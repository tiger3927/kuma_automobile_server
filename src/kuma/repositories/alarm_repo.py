"""告警写入与查询（(vin,ts,type) 唯一 → 重复上报天然幂等）。"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import Select, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from ..core.pagination import PageParams
from ..core.scope import Principal
from ..models.biz import Alarm, AlarmAck
from ..models.core import Vehicle
from .base import vehicle_scope


class AlarmRepo:
    async def raise_alarm(self, session, *, vin: str, ts: datetime, type_: str,
                          level: int = 2, payload: dict | None = None) -> bool:
        """ON CONFLICT DO NOTHING：返回是否新建。"""
        row = (await session.execute(
            pg_insert(Alarm).values(
                vin=vin, ts=ts, type=type_, level=level, payload=payload or {}, status="open",
            ).on_conflict_do_nothing(index_elements=["vin", "ts", "type"])
            .returning(Alarm.id)
        )).first()
        return row is not None

    def page(self, principal: Principal, *, vin: str | None = None,   # 见 vehicle_repo：方法别叫 list
             status: str | None = None, type_: str | None = None,
             page: PageParams | None = None) -> Select:
        stmt = select(Alarm).where(Alarm.vin.in_(
            select(Vehicle.vin).where(Vehicle.id.in_(vehicle_scope(principal)))
        ))
        if vin:
            stmt = stmt.where(Alarm.vin == vin)
        if status:
            stmt = stmt.where(Alarm.status == status)
        if type_:
            stmt = stmt.where(Alarm.type == type_)
        p = page or PageParams()
        return stmt.order_by(Alarm.ts.desc()).limit(p.limit).offset(p.offset)

    async def ack(self, session, *, alarm_id: int, account_id: int, note: str | None) -> Alarm | None:
        alarm = await session.get(Alarm, alarm_id)
        if alarm is None or alarm.status != "open":
            return None
        alarm.status = "ack"
        session.add(AlarmAck(alarm_id=alarm_id, ack_by=account_id, ack_at=datetime.now(UTC), note=note))
        await session.flush()
        return alarm

    async def open_counts_by_vin(self, session, principal: Principal) -> list[dict]:
        stmt: Select = (
            select(Alarm.vin, Alarm.type, text("count(*)").label("n"))
            .where(Alarm.status == "open", Alarm.vin.in_(
                select(Vehicle.vin).where(Vehicle.id.in_(vehicle_scope(principal)))))
            .group_by(Alarm.vin, Alarm.type)
            .order_by(text("n DESC")).limit(100)
        )
        return [dict(vin=r.vin, type=r.type, n=r.n) for r in (await session.execute(stmt)).all()]
