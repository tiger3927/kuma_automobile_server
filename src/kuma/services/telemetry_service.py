"""遥测入库管道：补报窗口 → deadband → COPY 落库 → Redis 状态。

顺序按 Task-Server.md §12-9：**deadband 在解码后、落库前**，被过滤的点仍更新 Redis 最新状态。

**实时广播不在这里**——它在 `mqtt/inbound.py` 出队时就发了（§12-9：最新状态不依赖落库）。
放在这里会让 WS 延迟被批处理窗口卡住（§12-10）。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from ..core.config import get_settings
from ..core.logging import get_logger
from ..repositories.state_repo import StateRepo
from ..repositories.telemetry_repo import FlushStats, TelemetryPoint, TelemetryRepo

log = get_logger("telemetry_service")


class TelemetryService:
    repo = TelemetryRepo()
    state = StateRepo()

    async def ingest(self, points: list[TelemetryPoint]) -> FlushStats:
        if not points:
            return FlushStats(0, 0, 0)
        s = get_settings()
        now = datetime.now(UTC)
        horizon = now - timedelta(hours=s.backfill_max_hours)

        fresh = [p for p in points if p.ts >= horizon]
        stale = len(points) - len(fresh)

        kept = [p for p in fresh if not self._deadbanded(p)]
        stats = await self.repo.flush(kept)
        stats.rejected_stale = stale

        for p in fresh:                      # 被 deadband 过滤的点也要刷新状态
            await self.state.update(p.vin, {
                "ts": p.ts, "lat": p.lat, "lon": p.lon, "speed_kmh": p.speed_kmh,
                "soc": p.soc, "odo_km": p.odo_km, "quality": p.quality,
            })
        if stale:
            log.warn("telemetry.stale_dropped", count=stale)

        await self._open_trips(kept)
        return stats

    async def _open_trips(self, points: list[TelemetryPoint]) -> None:
        """C0：行程生产者（口径见 `docs/mileage-spec.md`）。

        每个 vin 只取本批**最早**的有效点——行程起点应当是最早那一报；
        更晚才到的更早补报由 `recalc_mileage` 的幂等重算兜住。

        放在落库之后：行程必须建立在已持久化的遥测上，否则 `trip_close`
        可能关掉一段"库里根本没有点"的行程。
        """
        if not points:
            return
        from sqlalchemy import select

        from ..core.db import tx
        from ..models.core import Vehicle
        from ..tasks.trip_splitter import open_trip_for_vin

        earliest: dict[str, TelemetryPoint] = {}
        for p in points:
            cur = earliest.get(p.vin)
            if cur is None or p.ts < cur.ts:
                earliest[p.vin] = p

        opened = 0
        async with tx() as session:
            vehicles = (await session.execute(
                select(Vehicle).where(Vehicle.vin.in_(list(earliest)))
            )).scalars().all()
            by_vin = {v.vin: v for v in vehicles}
            for vin, p in earliest.items():
                vehicle = by_vin.get(vin)
                if vehicle is None:
                    continue                       # 未注册车辆（gateway 的 ACL 已挡，这里只是兜底）
                _, created = await open_trip_for_vin(session, vehicle, ts=p.ts, odo_km=p.odo_km)
                opened += int(created)
        if opened:
            log.info("trip.opened", count=opened, batch_vins=len(earliest))

    def _deadbanded(self, p: TelemetryPoint) -> bool:
        """静止且位移小于阈值 → 不落原始点（三轮车可减 60–80% 写入，§3-4）。

        同步判断依赖上一次状态缓存（`_last` 由 gateway 在解码时填充，避免每点多一次 Redis 往返）。
        """
        last = p.last
        if not last or p.lat is None or p.lon is None:
            return False
        s = get_settings()
        if (p.speed_kmh or 0) > s.deadband_max_speed:
            return False
        prev_lat, prev_lon = last.get("lat"), last.get("lon")
        if prev_lat is None or prev_lon is None:
            return False
        dist_m = ((p.lat - prev_lat) ** 2 + (p.lon - prev_lon) ** 2) ** 0.5 * 111_000
        return dist_m < s.deadband_min_dist_m

    async def track(self, vin: str, start: datetime, end: datetime, *, limit: int = 5000) -> list[dict]:
        return await self.repo.track(vin, start, end, limit=limit)

    async def live(self, vins: list[str]) -> dict[str, dict]:
        return await self.state.many(vins)
