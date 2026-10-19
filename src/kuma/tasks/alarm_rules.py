"""告警规则（M5）：超速、急刹、围栏越界、里程异常。

规则阈值 v1 写死常量，M6+ 移到 `core.region.features` / 运营商配置（代码不写死国家）。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from ..core.db import get_engine, tx
from ..core.logging import get_logger
from ..geo.fence import inside_operating_area
from ..repositories.alarm_repo import AlarmRepo
from ..repositories.state_repo import StateRepo

log = get_logger("tasks.alarms")

SPEED_LIMIT_KMH = 80.0
HARSH_BRAKE_DROP = 25.0      # 相邻两点速度下降超过此值（km/h）


async def overspeed_and_brake() -> None:
    """从最近 5 分钟遥测里找超速与急刹（走 SQL，避免把点全拉到应用层）。"""
    since = datetime.now(UTC) - timedelta(minutes=5)
    engine = get_engine()
    async with engine.connect() as conn:
        overspeed = (await conn.execute(text("""
            SELECT vin, max(ts) AS ts, max(speed_kmh) AS speed
            FROM tele.telemetry
            WHERE ts >= :since AND speed_kmh > :limit
            GROUP BY vin HAVING max(speed_kmh) > :limit
            LIMIT 500
        """), {"since": since, "limit": SPEED_LIMIT_KMH})).mappings().all()

        harsh = (await conn.execute(text("""
            WITH w AS (
              SELECT vin, ts, speed_kmh,
                     lag(speed_kmh) OVER (PARTITION BY vin ORDER BY ts) AS prev_speed
              FROM tele.telemetry WHERE ts >= :since
            )
            SELECT vin, ts, prev_speed, speed_kmh FROM w
            WHERE prev_speed - speed_kmh >= :drop ORDER BY ts DESC LIMIT 500
        """), {"since": since, "drop": HARSH_BRAKE_DROP})).mappings().all()

    async with tx() as session:
        repo = AlarmRepo()
        for r in overspeed:
            await repo.raise_alarm(session, vin=r["vin"], ts=r["ts"], type_="overspeed", level=2,
                                   payload={"speed_kmh": float(r["speed"]),
                                            "limit_kmh": SPEED_LIMIT_KMH})
        for r in harsh:
            await repo.raise_alarm(session, ts=r["ts"], vin=r["vin"], type_="harsh_brake", level=2,
                                   payload={"from": float(r["prev_speed"]),
                                            "to": float(r["speed_kmh"])})
    log.info("alarms.evaluated", overspeed=len(overspeed), harsh_brake=len(harsh))


async def fence_exit() -> None:
    """车辆当前位置不在其运营商获准经营区域 → 告警（派单硬过滤的镜像检查）。"""
    from sqlalchemy import select

    from ..models.core import Vehicle
    from ..models.mobility import FleetContract

    async with tx() as session:
        rows = (await session.execute(
            select(FleetContract.operator_org_id, Vehicle.vin, Vehicle.id)
            .join(Vehicle, Vehicle.id == FleetContract.vehicle_id)
            .where(FleetContract.status == "active").limit(500)
        )).all()

    state = StateRepo()
    hits = 0
    async with tx() as session:
        for org_id, vin, _vid in rows:
            live = await state.get(vin)
            if not live or live.get("lon") is None:
                continue
            if not await inside_operating_area(live["lon"], live["lat"], org_id):
                hits += 1
                await AlarmRepo().raise_alarm(
                    session, vin=vin, ts=_now(), type_="fence_exit", level=3,
                    payload={"org_id": org_id, "lon": live["lon"], "lat": live["lat"]})
    log.info("alarms.fence_exit", hits=hits)


async def mileage_anomaly() -> None:
    """odo 与 gps 差异 >15% → `mileage_anomaly`（口径见 §11）。"""
    since = datetime.now(UTC) - timedelta(hours=6)
    engine = get_engine()
    async with engine.connect() as conn:
        rows = (await conn.execute(text("SELECT * FROM mileage_anomalies(:since)"),
                                   {"since": since})).mappings().all()
    async with tx() as session:
        for r in rows:
            await AlarmRepo().raise_alarm(
                session, vin=r["vin"], ts=_now(), type_="mileage_anomaly", level=3,
                payload={"trip_id": r["trip_id"], "odo_m": r["odo_m"], "gps_m": r["gps_m"],
                         "ratio": float(r["ratio"]) if r["ratio"] is not None else None})
    log.info("alarms.mileage_anomaly", hits=len(rows))


def _now() -> datetime:
    return datetime.now(UTC)
