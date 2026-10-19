"""行程切分（M5）：按 `ts` 排序切，**不按到达顺序**（§8.8 乱序补报容忍）。

规则（v1）：
- 相邻两点间隔 > `gap_s`（默认 180s）→ 视为两段行程；
- 车辆离线（Redis TTL 过期）→ 关闭 open trip 并触发里程核算；
- odo 快照随 trip 存，`trip_mileage` 三口径由 `trip_service.recalc_mileage` 落库。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from ..core.db import tx
from ..core.logging import get_logger
from ..models.biz import Trip
from ..models.core import Vehicle
from ..models.tele import Telemetry
from ..repositories.trip_repo import TripRepo
from ..services.trip_service import TripService

log = get_logger("tasks.trip_splitter")

GAP_S = 180


async def close_idle_trips() -> None:
    """关闭"最后一报距今超过 gap_s"的 open trip。"""
    cutoff = datetime.now(UTC) - timedelta(seconds=GAP_S)
    async with tx() as session:
        rows = (await session.execute(
            select(Trip.id, Trip.vin)
            .where(Trip.status == "active")
            .limit(500)
        )).all()
        for trip_id, vin in rows:
            last_ts = (await session.execute(
                select(func.max(Telemetry.ts)).where(Telemetry.vin == vin)
            )).scalar_one_or_none()
            if last_ts is None or last_ts > cutoff:
                continue
            trip = await session.get(Trip, trip_id)
            if trip is None:
                continue
            trip.status = "completed"
            trip.end_ts = last_ts
            last_odo = (await session.execute(
                select(Telemetry.odo_km).where(Telemetry.vin == vin, Telemetry.ts <= last_ts)
                .order_by(Telemetry.ts.desc()).limit(1)
            )).scalar_one_or_none()
            if last_odo is not None:
                trip.end_odo_km = int(last_odo)
            await session.flush()
            await TripService().recalc_mileage(session, trip_id)
            log.info("trip.closed", trip_id=trip_id, vin=vin, end_ts=str(last_ts))


async def open_trip_for_vin(session, vehicle: Vehicle, *, ts: datetime,
                            odo_km: float | None) -> Trip:
    """gateway/worker 在检测到新车首次上报时开行程（幂等：已有 open trip 直接返回）。"""
    repo = TripRepo()
    existing = await repo.open_trip(session, vehicle.vin)
    if existing:
        return existing
    trip = Trip(
        vin=vehicle.vin, owner_type=vehicle.owner_type,
        owner_account_id=vehicle.owner_account_id, owner_org_id=vehicle.owner_org_id,
        region_id=vehicle.region_id, start_ts=ts, status="active",
        start_odo_km=int(odo_km) if odo_km is not None else None,
    )
    session.add(trip)
    await session.flush()
    return trip
