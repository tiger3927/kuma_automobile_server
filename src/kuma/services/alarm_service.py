"""告警查询、确认与状态流转（M5 验收：可查、可确认、可静默、可关闭）。

2026-10-22 补的三件事：
1. **状态机**（此前只有 `open → ack`，没有静默/关闭/重开）；
2. **归属校验**——旧 `ack()` 只 `session.get(Alarm, id)`，**任何登录用户都能确认别人的告警**；
   现在所有读写都先过 `assert_vehicle_visible`；
3. **同类抑制**在 `AlarmRepo.raise_alarm` 里（窗口来自 `biz.alarm_rule`）。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.audit import write_audit
from ..core.config import get_settings
from ..core.exceptions import Conflict, NotFound, ValidationFailed
from ..core.outbox import emit
from ..core.pagination import PageParams
from ..core.scope import Principal
from ..models.biz import Alarm, AlarmAck
from ..models.core import Vehicle
from ..repositories.alarm_repo import AlarmRepo
from ..repositories.base import assert_vehicle_visible, count_of


async def raise_alarm(session: AsyncSession, *, vin: str, ts: datetime, type_: str,
                      level: int = 2, payload: dict | None = None,
                      dedupe_window_s: int = 600) -> int | None:
    """建告警的**唯一入口**：抑制判定 + outbox 事件必须同事务（C1）。

    之前 5 个调用点（gateway 直推、超速、急刹、围栏越界、里程异常、离线）各自直接调 repo，
    事件要么没有、要么在事务外发（发出去了但业务回滚 = 幽灵事件）。
    """
    alarm_id = await AlarmRepo().raise_alarm(
        session, vin=vin, ts=ts, type_=type_, level=level, payload=payload,
        dedupe_window_s=dedupe_window_s)
    if alarm_id is not None:
        await emit(session, aggregate_type="alarm", aggregate_id=alarm_id, event_type="created",
                   payload={"vin": vin, "type": type_, "level": level, "ts": ts})
    return alarm_id

# 状态机（取值域写在这里，DB 的 status 列没有 CHECK——加状态只改这一处）。
ALLOWED: dict[str, set[str]] = {
    "open": {"ack", "silenced", "closed", "auto_closed"},
    "ack": {"silenced", "closed"},
    "silenced": {"open", "ack", "closed"},
    "closed": {"open"},            # reopen
    "auto_closed": {"open"},       # reopen
}


class AlarmService:
    repo = AlarmRepo()

    # ── 读 ──────────────────────────────────────────────────────
    async def page(self, session: AsyncSession, principal: Principal, *, vin: str | None = None,
                   status: str | None = None, type_: str | None = None,
                   page: PageParams | None = None) -> dict:
        p = page or PageParams()
        rows = (await session.execute(
            self.repo.page(principal, vin=vin, status=status, type_=type_, page=p)
        )).scalars().all()
        total = await count_of(session, self.repo.filters(principal, vin=vin, status=status,
                                                          type_=type_))
        return {"items": [{"id": a.id, "vin": a.vin, "ts": a.ts, "type": a.type, "level": a.level,
                           "status": a.status, "snooze_until": a.snooze_until,
                           "payload": a.payload} for a in rows],
                "limit": p.limit, "offset": p.offset, "total": total}

    async def detail(self, session: AsyncSession, principal: Principal, alarm_id: int) -> dict:
        alarm = await self._visible(session, principal, alarm_id)
        acks = (await session.execute(
            select(AlarmAck).where(AlarmAck.alarm_id == alarm_id).order_by(AlarmAck.ack_at)
        )).scalars().all()
        return {"id": alarm.id, "vin": alarm.vin, "ts": alarm.ts, "type": alarm.type,
                "level": alarm.level, "status": alarm.status, "payload": alarm.payload,
                "snooze_until": alarm.snooze_until, "created_at": alarm.created_at,
                "updated_at": alarm.updated_at,
                "acks": [{"ack_by": a.ack_by, "ack_at": a.ack_at, "note": a.note} for a in acks]}

    async def hotspots(self, session: AsyncSession, principal: Principal) -> list[dict]:
        return await self.repo.open_counts_by_vin(session, principal)

    # ── 写 ──────────────────────────────────────────────────────
    async def ack(self, session: AsyncSession, principal: Principal, alarm_id: int,
                  note: str | None = None, ip: str | None = None) -> dict:
        await self._visible(session, principal, alarm_id)
        alarm = await self.repo.ack(session, alarm_id=alarm_id, account_id=principal.account_id,
                                    note=note)
        if alarm is None:
            raise Conflict("alarm_not_open")
        await write_audit(session, actor=principal, action="alarm.ack",
                          resource_type="alarm", resource_id=alarm_id, after={"note": note}, ip=ip)
        await emit(session, aggregate_type="alarm", aggregate_id=alarm_id, event_type="acked",
                   payload={"vin": alarm.vin, "type": alarm.type, "status": alarm.status,
                            "by": principal.account_id})
        return {"id": alarm.id, "status": alarm.status}

    async def transition(self, session: AsyncSession, principal: Principal, alarm_id: int,
                         to_status: str, *, note: str | None = None,
                         snooze_s: int | None = None, ip: str | None = None) -> dict:
        """silence / close / reopen 的统一入口（状态机见 `ALLOWED`）。"""
        if to_status not in ALLOWED:
            raise ValidationFailed(f"未知状态 {to_status}")
        alarm = await self._visible(session, principal, alarm_id)
        from_status = alarm.status
        if to_status not in ALLOWED.get(from_status, set()):
            raise Conflict(f"不允许 {from_status} → {to_status}", code="alarm_bad_transition")
        if to_status == "open" and not note:
            raise ValidationFailed("reopen 必须给 reason（写进审计）")

        snooze_until = None
        if to_status == "silenced":
            seconds = snooze_s or get_settings().alarm_silence_default_s
            if not 60 <= seconds <= 30 * 86_400:
                raise ValidationFailed("静默时长必须在 60 秒 ~ 30 天之间")
            snooze_until = datetime.now(UTC) + timedelta(seconds=seconds)

        updated = await self.repo.set_status(session, alarm_id=alarm_id, to_status=to_status,
                                             snooze_until=snooze_until)
        if updated is None:
            raise NotFound("alarm_not_found")
        await write_audit(session, actor=principal, action=f"alarm.{to_status}",
                          resource_type="alarm", resource_id=alarm_id,
                          before={"status": from_status},
                          after={"status": to_status, "note": note,
                                 "snooze_until": snooze_until}, ip=ip)
        await emit(session, aggregate_type="alarm", aggregate_id=alarm_id,
                   event_type=to_status,
                   payload={"vin": alarm.vin, "type": alarm.type, "from": from_status,
                            "to": to_status, "by": principal.account_id,
                            "snooze_until": snooze_until})
        return {"id": updated.id, "status": updated.status, "snooze_until": updated.snooze_until}

    # ── 内部 ────────────────────────────────────────────────────
    async def _visible(self, session: AsyncSession, principal: Principal,
                       alarm_id: int) -> Alarm:
        """告警必须属于该 principal 可见的车辆（旧实现漏了这一步）。"""
        alarm = await session.get(Alarm, alarm_id)
        if alarm is None:
            raise NotFound("alarm_not_found")
        # ⚠️ Vehicle 的主键是 `id`，`vin` 只是 unique 列——`session.get(Vehicle, vin)`
        # 会把 17 字符的 VIN 当整数主键传给驱动，直接 500（2026-10-22 实测）。
        vehicle = (await session.execute(
            select(Vehicle).where(Vehicle.vin == alarm.vin)
        )).scalar_one_or_none()
        if vehicle is None:
            raise NotFound("alarm_not_found")
        assert_vehicle_visible(principal, vehicle)
        return alarm
