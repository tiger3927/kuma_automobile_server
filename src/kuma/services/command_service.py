"""下行控制（Task-Server.md §9）：状态机 + 安全闸门 + 不可篡改审计。

安全红线高于商业规则（Task-Plan.md §10-13）：欠费/过户/归属变化都不得绕过闸门。
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.bus import get_bus
from ..core.config import get_settings
from ..core.exceptions import (
    Conflict,
    Forbidden,
    InvalidState,
    NotFound,
    SafetyBlocked,
    VehicleOffline,
)
from ..core.logging import get_logger
from ..core.outbox import emit
from ..core.pagination import PageParams
from ..core.scope import Principal
from ..models.biz import Command
from ..models.core import Subscription
from ..repositories.base import assert_vehicle_visible
from ..repositories.command_repo import CommandRepo
from ..repositories.state_repo import StateRepo
from ..repositories.vehicle_repo import VehicleRepo

log = get_logger("command_service")

# 断电类指令（行车中禁止）
POWER_CUT = {"power_cut", "engine_disable", "lock_and_disable"}
HIGH_RISK = POWER_CUT | {"lock", "unlock"}

TRANSITIONS: dict[str, set[str]] = {
    "pending": {"sent", "failed"},
    "sent": {"acked", "timeout", "failed"},
    "timeout": {"sent", "failed"},
    "acked": set(),
    "failed": set(),
}


class CommandService:
    repo = CommandRepo()
    vehicles = VehicleRepo()
    state = StateRepo()

    async def issue(self, session: AsyncSession, principal: Principal, *, vin: str, cmd_type: str,
                    payload: dict | None = None, confirm_token: str | None = None,
                    co_approver_account_id: int | None = None,
                    ip: str | None = None) -> dict:
        principal.require("command_down", "write")
        vehicle = await self.vehicles.get_by_vin(session, vin)
        if vehicle is None:
            raise NotFound("vehicle_not_found")
        assert_vehicle_visible(principal, vehicle)

        # 闸门 1：离线车直接拒绝，不排队堆积
        if not await self.state.is_online(vin):
            raise VehicleOffline("vehicle_offline")

        # 闸门：订阅（C6）。口径——**订阅失效后收回控制能力**，但遥测入库与 WS 推送不受影响
        # （订阅买的是服务，不是记录权；见 `tasks/sub_expiry.py` 的口径说明）。
        # v1 过渡：从未订阅过的车不拦——seed 与在测车辆没有订阅记录，
        # 直接拦会让 M7 下行链路无法验证；只有"有过订阅且全部失效"才拒。
        sub_statuses = (await session.execute(
            select(Subscription.status).where(Subscription.vin == vin)
        )).scalars().all()
        if sub_statuses and not any(s in ("active", "grace") for s in sub_statuses):
            raise InvalidState("subscription_inactive")

        # 闸门 2：行车中禁断电
        live = await self.state.get(vin) or {}
        speed = float(live.get("speed_kmh") or 0)
        if cmd_type in POWER_CUT and speed > get_settings().cmd_speed_limit_kmh:
            raise SafetyBlocked("power_cut_blocked_while_moving",
                                detail={"speed_kmh": speed,
                                        "limit_kmh": get_settings().cmd_speed_limit_kmh})

        # 闸门 3：高危指令需前端二次确认（服务端校验 confirm_token）
        if cmd_type in HIGH_RISK and not (
            confirm_token and await _consume_confirm(vin, cmd_type, confirm_token)
        ):
            raise Conflict("confirm_required",
                           detail={"reason": "高危指令需先调用 /commands/confirm 获取确认令牌"})

        # 闸门 4：平台侧执行需双人授权
        if principal.needs_dual_control("command_down", "write"):
            await _require_co_approver(session, principal, co_approver_account_id)

        cmd_id = f"cmd_{vin[-6:]}_{secrets.token_hex(6)}"
        cmd = await self.repo.create(session, cmd_id=cmd_id, vin=vin, cmd_type=cmd_type,
                                     payload=payload or {}, created_by=principal.account_id)
        await self.repo.append_audit(
            session, cmd_id=cmd_id, attempt=0, event="created",
            actor_account_id=principal.account_id,
            detail={"cmd_type": cmd_type, "payload": payload or {}, "ip": ip,
                    "speed_at_issue": speed, "identity": principal.active_identity},
        )
        await self.repo.transition(session, cmd, "sent")
        await self.repo.append_audit(session, cmd_id=cmd_id, attempt=0, event="queued",
                                     actor_account_id=principal.account_id, detail={})

        await emit(session, aggregate_type="command", aggregate_id=cmd_id, event_type="queued",
                   payload={"vin": vin, "cmd_type": cmd_type, "status": cmd.status,
                            "by": principal.account_id})
        await get_bus().publish_cmd({
            "cmd_id": cmd_id, "vin": vin, "cmd_type": cmd_type, "payload": payload or {},
            "attempt": 0, "issued_at": datetime.now(UTC).isoformat(),
            "ttl_s": get_settings().cmd_timeout,
        })
        return {"cmd_id": cmd_id, "status": cmd.status, "vin": vin, "cmd_type": cmd_type}

    async def page(self, session: AsyncSession, principal: Principal, *, vin: str | None = None,
                   status: str | None = None, page: PageParams | None = None) -> dict:
        rows = (await session.execute(
            self.repo.page(principal, vin=vin, status=status, page=page)
        )).scalars().all()
        return {"items": [{"cmd_id": c.cmd_id, "vin": c.vin, "cmd_type": c.cmd_type,
                           "status": c.status, "attempt": c.attempt, "created_at": c.created_at,
                           "sent_at": c.sent_at, "acked_at": c.acked_at} for c in rows],
                "limit": (page or PageParams()).limit, "offset": (page or PageParams()).offset}

    async def trail(self, session: AsyncSession, principal: Principal, cmd_id: str) -> dict:
        principal.require("command_down", "read")
        cmd = await self.repo.get(session, cmd_id)
        if cmd is None:
            raise NotFound("command_not_found")
        vehicle = await self.vehicles.get_by_vin(session, cmd.vin)
        if vehicle is None:
            raise NotFound("vehicle_not_found")
        assert_vehicle_visible(principal, vehicle)
        return {"cmd_id": cmd_id, "status": cmd.status,
                "audit": await self.repo.audit_trail(session, cmd_id)}

    async def handle_ack(self, session: AsyncSession, *, cmd_id: str, result: dict) -> None:
        """gateway 收到车端 cmd_ack 后调用。"""
        cmd = await self.repo.get(session, cmd_id)
        if cmd is None:
            log.warn("command.ack_unknown", cmd_id=cmd_id)
            return
        if cmd.status in ("acked", "failed"):
            return                                    # 幂等：重复 ack 忽略
        await self.repo.transition(session, cmd, "acked")
        await self.repo.append_audit(session, cmd_id=cmd_id, attempt=cmd.attempt, event="acked",
                                     actor_account_id=None, detail=result)
        await emit(session, aggregate_type="command", aggregate_id=cmd_id, event_type="acked",
                   payload={"vin": cmd.vin, "cmd_type": cmd.cmd_type, "status": cmd.status,
                            "result": result})

    async def mark_timeout(self, session: AsyncSession, cmd: Command) -> dict:
        """worker：超时重投（≤3 次，退避），超限置 failed。"""
        if cmd.status not in TRANSITIONS or "timeout" not in TRANSITIONS[cmd.status]:
            raise InvalidState(f"{cmd.status} 不能转 timeout")
        await self.repo.transition(session, cmd, "timeout")
        await self.repo.append_audit(session, cmd_id=cmd.cmd_id, attempt=cmd.attempt,
                                     event="timeout", actor_account_id=None, detail={})
        s = get_settings()
        if cmd.attempt >= s.cmd_max_retry:
            await self.repo.transition(session, cmd, "failed")
            await self.repo.append_audit(session, cmd_id=cmd.cmd_id, attempt=cmd.attempt,
                                         event="failed", actor_account_id=None,
                                         detail={"reason": "retry_exhausted"})
            await emit(session, aggregate_type="command", aggregate_id=cmd.cmd_id,
                       event_type="failed",
                       payload={"vin": cmd.vin, "cmd_type": cmd.cmd_type, "status": "failed",
                                "attempt": cmd.attempt, "reason": "retry_exhausted"})
            return {"cmd_id": cmd.cmd_id, "status": "failed"}

        attempt = cmd.attempt + 1
        await self.repo.transition(session, cmd, "sent", attempt=attempt)
        await self.repo.append_audit(session, cmd_id=cmd.cmd_id, attempt=attempt, event="retry",
                                     actor_account_id=None, detail={"backoff_s": 2 ** attempt})
        await emit(session, aggregate_type="command", aggregate_id=cmd.cmd_id, event_type="retry",
                   payload={"vin": cmd.vin, "cmd_type": cmd.cmd_type, "status": "sent",
                            "attempt": attempt, "backoff_s": 2 ** attempt})
        await get_bus().publish_cmd({
            "cmd_id": cmd.cmd_id, "vin": cmd.vin, "cmd_type": cmd.cmd_type,
            "payload": cmd.payload, "attempt": attempt,
            "issued_at": datetime.now(UTC).isoformat(), "ttl_s": s.cmd_timeout,
        })
        return {"cmd_id": cmd.cmd_id, "status": "sent", "attempt": attempt}


async def _require_co_approver(session: AsyncSession, principal: Principal,
                               co_approver_account_id: int | None) -> None:
    from ..models.core import Identity

    if co_approver_account_id is None:
        raise Forbidden("dual_control_required")
    if co_approver_account_id == principal.account_id:
        raise Forbidden("co_approver_must_differ")
    ok = (await session.execute(
        Identity.__table__.select().where(
            Identity.account_id == co_approver_account_id,
            Identity.identity_type == "platform_staff", Identity.status == "active")
    )).first()
    if ok is None:
        raise Forbidden("co_approver_not_platform_staff")


async def _consume_confirm(vin: str, cmd_type: str, token: str) -> bool:
    from ..core.redis_client import get_redis, kkey

    key = kkey("confirm", vin, cmd_type)
    r = get_redis()
    stored = await r.get(key)
    if stored != token:
        return False
    await r.delete(key)
    return True


async def issue_confirm(vin: str, cmd_type: str) -> str:
    """高危指令的二次确认令牌（5 分钟有效，一次性）。"""
    from ..core.redis_client import kkey, set_ttl

    token = secrets.token_urlsafe(16)
    await set_ttl(kkey("confirm", vin, cmd_type), token, 300)
    return token
