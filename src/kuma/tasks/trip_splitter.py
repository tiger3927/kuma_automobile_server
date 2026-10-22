"""行程切分（M5）：按 `ts` 排序切，**不按到达顺序**（§8.8 乱序补报容忍）。

规则（v1）：
- 相邻两点间隔 > `gap_s`（默认 180s）→ 视为两段行程；
- 车辆离线（Redis TTL 过期）→ 关闭 open trip 并触发里程核算；
- odo 快照随 trip 存，`trip_mileage` 三口径由 `trip_service.recalc_mileage` 落库。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from ..core.config import get_settings
from ..core.db import tx
from ..core.logging import get_logger
from ..core.outbox import emit
from ..models.biz import Trip
from ..models.core import Vehicle
from ..models.tele import Telemetry
from ..repositories.trip_repo import TripRepo
from ..services.trip_service import TripService

log = get_logger("tasks.trip_splitter")

GAP_S = 180      # 兜底值；实际取 `KUMA_TRIP_GAP_S`（口径见 docs/mileage-spec.md）


async def close_idle_trips() -> None:
    """关闭"最后一报距今超过 gap_s"的 open trip。"""
    cutoff = datetime.now(UTC) - timedelta(seconds=get_settings().trip_gap_s or GAP_S)
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
            # 车端时钟异常（补报点的 ts 早于行程开始）会让 end_ts < start_ts，
            # 于是 `recalc_mileage` 里的 `check_time_window` 直接抛 "start 晚于 end"，
            # 行程卡在 active、trip_close 每 180s 重复失败（2026-10-22 端到端实测抓到）。
            # 这里兜住并留痕，而不是让它静默卡死。
            end_ts = last_ts if last_ts >= trip.start_ts else trip.start_ts
            if end_ts != last_ts:
                log.warn("trip.clock_anomaly", trip_id=trip_id, vin=vin,
                         start_ts=str(trip.start_ts), last_report=str(last_ts))
            trip.status = "completed"
            trip.end_ts = end_ts
            last_odo = (await session.execute(
                select(Telemetry.odo_km).where(Telemetry.vin == vin, Telemetry.ts <= last_ts)
                .order_by(Telemetry.ts.desc()).limit(1)
            )).scalar_one_or_none()
            if last_odo is not None:
                trip.end_odo_km = int(last_odo)
            await session.flush()
            mileage = await TripService().recalc_mileage(session, trip_id)
            await emit(session, aggregate_type="trip", aggregate_id=trip_id,
                       event_type="closed",
                       payload={"vin": vin, "status": "completed",
                                "start_ts": trip.start_ts, "end_ts": trip.end_ts,
                                "start_odo_km": trip.start_odo_km,
                                "end_odo_km": trip.end_odo_km,
                                "accounts": [a for a in (trip.owner_account_id,) if a]})
            log.info("trip.closed", trip_id=trip_id, vin=vin, end_ts=str(last_ts),
                     mileage=mileage)


async def open_trip_for_vin(session, vehicle: Vehicle, *, ts: datetime,
                            odo_km: float | None) -> tuple[Trip, bool]:
    """开一段行程，返回 `(trip, created)`。幂等：已有 active trip 就返回它 + False。

    **这是全仓唯一写 `biz.trip` 的地方**（C0，2026-10-22 接进 `TelemetryService.ingest`）。
    在此之前它零调用方，于是 `GET /trips`、`/gov/trips`、`recalc` 读到的全是 seed 数据，
    `trip_close` 任务每 180s 扫一遍永远扫不到 `status='active'`——
    而探针显示它"新鲜、零失败"，绿灯是假的。

    `owner_*` 存的是**行程开始时刻的快照**：过户不追溯改变已有行程的归属（口径见
    `docs/mileage-spec.md` §3）。
    """
    repo = TripRepo()
    existing = await repo.open_trip(session, vehicle.vin)
    if existing:
        return existing, False
    trip = Trip(
        vin=vehicle.vin, owner_type=vehicle.owner_type,
        owner_account_id=vehicle.owner_account_id, owner_org_id=vehicle.owner_org_id,
        region_id=vehicle.region_id, start_ts=ts, status="active",
        start_odo_km=int(odo_km) if odo_km is not None else None,
    )
    session.add(trip)
    await session.flush()
    await emit(session, aggregate_type="trip", aggregate_id=trip.id, event_type="opened",
               payload={"vin": vehicle.vin, "status": "active", "start_ts": trip.start_ts,
                        "start_odo_km": trip.start_odo_km,
                        "accounts": [a for a in (vehicle.owner_account_id,) if a]})
    return trip, True
