"""遥测入库管道：补报窗口 → deadband → COPY 落库 → Redis 状态 → 实时广播。

顺序按 Task-Server.md §12-9：**deadband 在解码后、落库前**，被过滤的点仍更新 Redis 最新状态。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from ..core.config import get_settings
from ..core.logging import get_logger
from ..realtime.broadcaster import publish_vin
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
        if kept:
            await self._broadcast(kept)
        if stale:
            log.warn("telemetry.stale_dropped", count=stale)
        return stats

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

    async def _broadcast(self, points: list[TelemetryPoint]) -> None:
        latest: dict[str, TelemetryPoint] = {}
        for p in points:
            latest[p.vin] = p
        for vin, p in latest.items():
            await publish_vin(vin, "telemetry", {
                "ts": p.ts, "lat": p.lat, "lon": p.lon, "speed_kmh": p.speed_kmh,
                "soc": p.soc, "odo_km": p.odo_km,
            })

    async def track(self, vin: str, start: datetime, end: datetime, *, limit: int = 5000) -> list[dict]:
        return await self.repo.track(vin, start, end, limit=limit)

    async def live(self, vins: list[str]) -> dict[str, dict]:
        return await self.state.many(vins)
