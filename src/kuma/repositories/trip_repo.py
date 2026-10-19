"""行程与里程核算结果。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Select, select

from ..core.pagination import PageParams
from ..core.scope import Principal
from ..models.biz import Trip, TripMileage
from .base import trip_scope_clause


class TripRepo:
    def page(self, principal: Principal, *, vin: str | None = None,   # 见 vehicle_repo：方法别叫 list
             status: str | None = None, start: datetime | None = None,
             end: datetime | None = None, page: PageParams | None = None) -> Select:
        stmt = select(Trip)
        clause = trip_scope_clause(principal)
        if clause is not None:
            stmt = stmt.where(clause)
        if vin:
            stmt = stmt.where(Trip.vin == vin)
        if status:
            stmt = stmt.where(Trip.status == status)
        if start:
            stmt = stmt.where(Trip.start_ts >= start)
        if end:
            stmt = stmt.where(Trip.start_ts < end)
        p = page or PageParams()
        return stmt.order_by(Trip.start_ts.desc()).limit(p.limit).offset(p.offset)

    async def get(self, session, trip_id: int) -> Trip | None:
        return await session.get(Trip, trip_id)

    async def open_trip(self, session, vin: str) -> Trip | None:
        return (await session.execute(
            select(Trip).where(Trip.vin == vin, Trip.status == "active").order_by(Trip.start_ts.desc())
        )).scalar_one_or_none()

    async def mileage(self, session, trip_id: int) -> list[TripMileage]:
        return (await session.execute(
            select(TripMileage).where(TripMileage.trip_id == trip_id)
        )).scalars().all()

    async def save_mileage(self, session, trip_id: int, method: str, distance_m: int,
                           *, confidence: float | None = None, flags: dict | None = None) -> TripMileage:
        """同一 trip 同口径重复写入直接覆盖（唯一约束 trip_id+method）。"""
        existing = (await session.execute(
            select(TripMileage).where(TripMileage.trip_id == trip_id, TripMileage.method == method)
        )).scalar_one_or_none()
        if existing:
            existing.distance_m = distance_m
            existing.confidence = confidence
            existing.flags = flags or {}
            await session.flush()
            return existing
        row = TripMileage(trip_id=trip_id, method=method, distance_m=distance_m,
                          confidence=confidence, flags=flags or {})
        session.add(row)
        await session.flush()
        return row
