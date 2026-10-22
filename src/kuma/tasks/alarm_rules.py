"""告警规则（M5）：超速、急刹、围栏越界、里程异常、静默到期。

**2026-10-22：阈值从硬编码常量迁到 `biz.alarm_rule`（迁移 0006）**，原因有两条：
- 限速是**地区差异**（TH/MY/ZA 各不相同），而 §12.1 明令禁止代码写死国家；
- 改一个阈值不该发一次版。

两条实现约束：
1. **常量保留作 fallback**——规则表没 seed 或被误删时，告警不能静默停摆；
   宁可按旧阈值多报，也不要"看起来一切正常"。
2. **粗筛用所有规则里最严的阈值，精筛按车辆 region**——
   这样加了地区覆盖行也不会漏报（SQL 只负责缩小候选集）。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select, text

from ..core.db import get_engine, tx
from ..core.logging import get_logger
from ..geo.fence import inside_operating_area
from ..models.biz import AlarmRule
from ..repositories.alarm_repo import AlarmRepo
from ..repositories.state_repo import StateRepo

log = get_logger("tasks.alarms")

# fallback：与迁移 0006 seed 的值一致。规则表缺失时用它们，不是"默认更严"。
FALLBACK: dict[str, dict] = {
    "overspeed": {"speed_kmh": 80.0, "level": 2, "dedupe_s": 600},
    "harsh_brake": {"drop_kmh": 25.0, "level": 2, "dedupe_s": 300},
    "fence_exit": {"level": 3, "dedupe_s": 900},
    "mileage_anomaly": {"diff_ratio": 0.15, "level": 3, "dedupe_s": 3600},
    "offline": {"offline_s": 600, "level": 2, "dedupe_s": 1800},
}


async def _rules(session, type_: str) -> dict[str, dict]:
    """取规则：key 是 region_code，`'*'` 表示全局默认。"""
    rows = (await session.execute(
        select(AlarmRule).where(AlarmRule.type == type_, AlarmRule.enabled.is_(True))
    )).scalars().all()
    out: dict[str, dict] = {}
    for r in rows:
        out[r.region_code or "*"] = {**FALLBACK.get(type_, {}), **(r.params or {}),
                                     "level": r.level, "dedupe_s": r.dedupe_window_s}
    return out or {"*": dict(FALLBACK.get(type_, {}))}


def _pick(rules: dict[str, dict], region: str | None, key: str):
    """地区覆盖优先，其次全局默认。`_rules()` 保证至少有一条 `'*'`，所以不会取空。"""
    rule = rules.get(region or "*") or rules.get("*") or {}
    return rule.get(key)


async def overspeed_and_brake() -> None:
    """从最近 5 分钟遥测里找超速与急刹（走 SQL，避免把点全拉到应用层）。"""
    since = datetime.now(UTC) - timedelta(minutes=5)
    async with tx() as session:
        speed_rules = await _rules(session, "overspeed")
        brake_rules = await _rules(session, "harsh_brake")

    # 粗筛阈值：所有地区规则里最严的那个，保证地区覆盖不会造成漏报
    speed_floor = min(float(_pick(speed_rules, r, "speed_kmh")) for r in speed_rules)
    drop_cap = min(float(_pick(brake_rules, r, "drop_kmh")) for r in brake_rules)

    engine = get_engine()
    async with engine.connect() as conn:
        overspeed = (await conn.execute(text("""
            SELECT t.vin, max(t.ts) AS ts, max(t.speed_kmh) AS speed, v.region_id
            FROM tele.telemetry t JOIN core.vehicle v ON v.vin = t.vin
            WHERE t.ts >= :since AND t.speed_kmh > :limit
            GROUP BY t.vin, v.region_id HAVING max(t.speed_kmh) > :limit
            LIMIT 500
        """), {"since": since, "limit": speed_floor})).mappings().all()

        harsh = (await conn.execute(text("""
            WITH w AS (
              SELECT t.vin, t.ts, t.speed_kmh, v.region_id,
                     lag(t.speed_kmh) OVER (PARTITION BY t.vin ORDER BY t.ts) AS prev_speed
              FROM tele.telemetry t JOIN core.vehicle v ON v.vin = t.vin
              WHERE t.ts >= :since
            )
            SELECT vin, ts, prev_speed, speed_kmh, region_id FROM w
            WHERE prev_speed - speed_kmh >= :drop ORDER BY ts DESC LIMIT 500
        """), {"since": since, "drop": drop_cap})).mappings().all()

    raised = 0
    async with tx() as session:
        repo = AlarmRepo()
        for r in overspeed:
            region = r["region_id"]
            limit_kmh = float(_pick(speed_rules, region, "speed_kmh"))
            if float(r["speed"]) <= limit_kmh:          # 精筛：按该地区自己的限速
                continue
            if await repo.raise_alarm(
                session, vin=r["vin"], ts=r["ts"], type_="overspeed",
                level=int(_pick(speed_rules, region, "level") or 2),
                payload={"speed_kmh": float(r["speed"]), "limit_kmh": limit_kmh,
                         "region": region},
                dedupe_window_s=int(_pick(speed_rules, region, "dedupe_s") or 600),
            ):
                raised += 1
        for r in harsh:
            region = r["region_id"]
            if await repo.raise_alarm(
                session, ts=r["ts"], vin=r["vin"], type_="harsh_brake",
                level=int(_pick(brake_rules, region, "level") or 2),
                payload={"from": float(r["prev_speed"]), "to": float(r["speed_kmh"]),
                         "region": region},
                dedupe_window_s=int(_pick(brake_rules, region, "dedupe_s") or 300),
            ):
                raised += 1
    # candidates 是 SQL 候选数，raised 是真正新建数（差额 = 被同类抑制窗口挡掉的）
    log.info("alarms.evaluated", overspeed_candidates=len(overspeed),
             harsh_candidates=len(harsh), raised=raised)


async def fence_exit() -> None:
    """车辆当前位置不在其运营商获准经营区域 → 告警（派单硬过滤的镜像检查）。"""
    from ..models.core import Vehicle
    from ..models.mobility import FleetContract

    async with tx() as session:
        rows = (await session.execute(
            select(FleetContract.operator_org_id, Vehicle.vin, Vehicle.id)
            .join(Vehicle, Vehicle.id == FleetContract.vehicle_id)
            .where(FleetContract.status == "active").limit(500)
        )).all()
        rules = await _rules(session, "fence_exit")

    state = StateRepo()
    hits = 0
    async with tx() as session:
        repo = AlarmRepo()
        for org_id, vin, _vid in rows:
            live = await state.get(vin)
            if not live or live.get("lon") is None:
                continue
            if not await inside_operating_area(live["lon"], live["lat"], org_id):
                if await repo.raise_alarm(
                    session, vin=vin, ts=_now(), type_="fence_exit",
                    level=int(_pick(rules, live.get("region"), "level") or 3),
                    payload={"org_id": org_id, "lon": live["lon"], "lat": live["lat"]},
                    dedupe_window_s=int(_pick(rules, live.get("region"), "dedupe_s") or 900),
                ):
                    hits += 1
    log.info("alarms.fence_exit", hits=hits)


async def mileage_anomaly() -> None:
    """odo 与 gps 差异超阈值 → `mileage_anomaly`（口径见 §11）。"""
    since = datetime.now(UTC) - timedelta(hours=6)
    async with tx() as session:
        rules = await _rules(session, "mileage_anomaly")
    engine = get_engine()
    async with engine.connect() as conn:
        rows = (await conn.execute(text("SELECT * FROM mileage_anomalies(:since)"),
                                   {"since": since})).mappings().all()
    raised = 0
    async with tx() as session:
        repo = AlarmRepo()
        for r in rows:
            if await repo.raise_alarm(
                session, vin=r["vin"], ts=_now(), type_="mileage_anomaly",
                level=int(_pick(rules, None, "level") or 3),
                payload={"trip_id": r["trip_id"], "odo_m": r["odo_m"], "gps_m": r["gps_m"],
                         "ratio": float(r["ratio"]) if r["ratio"] is not None else None},
                dedupe_window_s=int(_pick(rules, None, "dedupe_s") or 3600),
            ):
                raised += 1
    log.info("alarms.mileage_anomaly", candidates=len(rows), raised=raised)


async def snooze_expiry() -> None:
    """静默到期 → 回到 `open`。

    没有这一步，"静默"实际上等于永久屏蔽：告警会一直停在 silenced，
    抑制窗口（`raise_alarm` 的 NOT EXISTS）也会因此把后续同类告警全部吃掉。
    """
    async with tx() as session:
        rows = await AlarmRepo().expired_snoozes(session)
        for alarm in rows:
            alarm.status = "open"
            alarm.snooze_until = None
            alarm.updated_at = _now()
        if rows:
            await session.flush()
    if rows:
        log.info("alarms.snooze_expired", count=len(rows))


def _now() -> datetime:
    return datetime.now(UTC)
