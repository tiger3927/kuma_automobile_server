"""下行指令与不可篡改审计（`biz.command_audit` 对 kuma_app 无 UPDATE/DELETE）。"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import Select, select

from ..core.pagination import PageParams
from ..core.scope import Principal
from ..models.biz import Command, CommandAudit
from ..models.core import Vehicle
from .base import vehicle_scope

TERMINAL = {"acked", "failed"}


class CommandRepo:
    async def create(self, session, *, cmd_id: str, vin: str, cmd_type: str,
                     payload: dict, created_by: int) -> Command:
        cmd = Command(cmd_id=cmd_id, vin=vin, cmd_type=cmd_type, payload=payload,
                      status="pending", attempt=0, created_by=created_by)
        session.add(cmd)
        await session.flush()
        return cmd

    async def get(self, session, cmd_id: str) -> Command | None:
        return (await session.execute(select(Command).where(Command.cmd_id == cmd_id))
                ).scalar_one_or_none()

    async def append_audit(self, session, *, cmd_id: str, attempt: int, event: str,
                           actor_account_id: int | None, detail: dict) -> None:
        session.add(CommandAudit(cmd_id=cmd_id, attempt=attempt, event=event,
                                 actor_account_id=actor_account_id, detail=detail))
        await session.flush()

    async def transition(self, session, cmd: Command, status: str, *,
                         attempt: int | None = None) -> Command:
        cmd.status = status
        if attempt is not None:
            cmd.attempt = attempt
        now = datetime.now(UTC)
        if status == "sent":
            cmd.sent_at = now
        elif status == "acked":
            cmd.acked_at = now
        await session.flush()
        return cmd

    def page(self, principal: Principal, *, vin: str | None = None,   # 见 vehicle_repo：方法别叫 list
             status: str | None = None, page: PageParams | None = None) -> Select:
        stmt = select(Command).where(Command.vin.in_(
            select(Vehicle.vin).where(Vehicle.id.in_(vehicle_scope(principal)))
        ))
        if vin:
            stmt = stmt.where(Command.vin == vin)
        if status:
            stmt = stmt.where(Command.status == status)
        p = page or PageParams()
        return stmt.order_by(Command.created_at.desc()).limit(p.limit).offset(p.offset)

    async def audit_trail(self, session, cmd_id: str) -> list[dict]:
        rows = (await session.execute(
            select(CommandAudit).where(CommandAudit.cmd_id == cmd_id).order_by(CommandAudit.id)
        )).scalars().all()
        return [{"attempt": a.attempt, "event": a.event, "actor": a.actor_account_id,
                 "at": a.created_at.isoformat(), "detail": a.detail} for a in rows]
