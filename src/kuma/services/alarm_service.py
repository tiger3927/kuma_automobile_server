"""告警查询与确认。"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from ..core.audit import write_audit
from ..core.exceptions import Conflict, NotFound
from ..core.pagination import PageParams
from ..core.scope import Principal
from ..models.biz import Alarm
from ..repositories.alarm_repo import AlarmRepo


class AlarmService:
    repo = AlarmRepo()

    async def page(self, session: AsyncSession, principal: Principal, *, vin: str | None = None,
                   status: str | None = None, type_: str | None = None,
                   page: PageParams | None = None) -> dict:
        rows = (await session.execute(
            self.repo.page(principal, vin=vin, status=status, type_=type_, page=page)
        )).scalars().all()
        return {"items": [{"id": a.id, "vin": a.vin, "ts": a.ts, "type": a.type, "level": a.level,
                           "status": a.status, "payload": a.payload} for a in rows],
                "limit": (page or PageParams()).limit, "offset": (page or PageParams()).offset}

    async def ack(self, session: AsyncSession, principal: Principal, alarm_id: int,
                  note: str | None = None, ip: str | None = None) -> dict:
        alarm = await self.repo.ack(session, alarm_id=alarm_id, account_id=principal.account_id,
                                    note=note)
        if alarm is None:
            raise Conflict("alarm_not_open")
        await write_audit(session, actor=principal, action="alarm.ack",
                          resource_type="alarm", resource_id=alarm_id, after={"note": note}, ip=ip)
        return {"id": alarm.id, "status": alarm.status}

    async def hotspots(self, session: AsyncSession, principal: Principal) -> list[dict]:
        return await self.repo.open_counts_by_vin(session, principal)

    async def get(self, session: AsyncSession, alarm_id: int):
        alarm = await session.get(Alarm, alarm_id)
        if alarm is None:
            raise NotFound("alarm_not_found")
        return alarm
