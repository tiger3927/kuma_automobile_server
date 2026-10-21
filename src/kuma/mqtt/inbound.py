"""入站管道：有界队列 → **点一到就推 WS** + 固定周期窗口批量 COPY（500 条 / 1s）。

背压是 4G 内存机器上的保命项（§12-6）：队列满时丢"低优先级"（心跳/重复点）并计数告警。

**实时推送与落库解耦**（§12-9）：WS 广播在点出队时立刻发，不等 COPY——"最新状态"不依赖落库，
被 deadband 过滤（不落库）的点同样要推给前端。落库只决定历史轨迹，不决定实时性。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from ..core.config import get_settings
from ..core.logging import get_logger
from ..realtime.broadcaster import publish_vin
from ..repositories.telemetry_repo import TelemetryPoint
from ..services.telemetry_service import TelemetryService
from .codecs import Decoded
from .dedup import DedupWindow

log = get_logger("mqtt.inbound")


@dataclass(slots=True)
class InboundStats:
    received: int = 0
    decoded_error: int = 0
    dup_dropped: int = 0
    stale_dropped: int = 0
    queue_dropped: int = 0
    inserted: int = 0
    by_vin_error: dict[str, int] = field(default_factory=dict)


class InboundPipeline:
    def __init__(self) -> None:
        s = get_settings()
        self._queue: asyncio.Queue[TelemetryPoint] = asyncio.Queue(maxsize=s.tele_queue_max)
        self._dedup = DedupWindow()
        self._svc = TelemetryService()
        self.stats = InboundStats()
        self._flusher: asyncio.Task | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        self._stop.clear()
        self._flusher = asyncio.create_task(self._flush_loop(), name="tele-flush")

    async def stop(self) -> None:
        """优雅关闭：先刷完缓冲再退出（§12-4）。"""
        self._stop.set()
        if self._flusher:
            await self._flusher
            self._flusher = None

    def offer(self, decoded: Decoded) -> None:
        self.stats.received += 1
        if decoded.kind == "error":
            self.stats.decoded_error += 1
            log.warn("inbound.decode_error", error=decoded.error)
            return
        if not decoded.points:
            return
        fresh, stale = self._dedup.stale(decoded.points)
        self.stats.stale_dropped += stale
        kept, dup = self._dedup.filter(fresh)
        self.stats.dup_dropped += dup
        for p in kept:
            try:
                self._queue.put_nowait(p)
            except asyncio.QueueFull:
                self.stats.queue_dropped += 1        # 背压：丢弃并计数，不阻塞 MQTT 读取

    async def _flush_loop(self) -> None:
        """固定周期窗口：任何点最多等 `tele_flush_interval` 就落库；**出队即推 WS，不等落库**。

        旧写法每拿到一条就重新 `wait_for(1.0)`，把窗口变成了"两条消息之间的空闲计时器"：
        低速率下每条都要空等一整个 interval（实测 WS 延迟恒定 ≈1.04s），高速率下 `wait_for`
        永不超时、要攒满 `tele_batch_size` 才落库（200 msg/s = 2.5s）——见 §12-10。
        现在用 deadline 收批：窗口是墙钟周期，与消息间隔无关。
        """
        s = get_settings()
        loop = asyncio.get_running_loop()
        batch: list[TelemetryPoint] = []
        while True:
            deadline = loop.time() + s.tele_flush_interval
            while len(batch) < s.tele_batch_size:
                if self._stop.is_set():
                    try:                                # 关闭中：不再等窗口，把队列排空
                        p = self._queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                else:
                    remaining = deadline - loop.time()
                    if remaining <= 0:
                        break
                    try:
                        p = await asyncio.wait_for(self._queue.get(), timeout=remaining)
                    except TimeoutError:
                        break
                batch.append(p)
                await self._publish(p)
            if batch:
                await self._flush(batch)
                batch = []
            elif self._stop.is_set() and self._queue.empty():
                return

    async def _publish(self, p: TelemetryPoint) -> None:
        """点一出队就广播给 WS 订阅者（与落库无关，§12-9）。

        被 deadband 过滤、不落库的点**也推**：前端要的是"这辆车还活着、在这"，
        不是"这行有没有写进 tele.telemetry"。补报太旧的点和落库口径一致，不推。
        """
        s = get_settings()
        if p.ts < datetime.now(UTC) - timedelta(hours=s.backfill_max_hours):
            return
        try:
            await publish_vin(p.vin, "telemetry", {
                "ts": p.ts, "lat": p.lat, "lon": p.lon, "speed_kmh": p.speed_kmh,
                "soc": p.soc, "odo_km": p.odo_km,
            })
        except Exception as exc:                        # noqa: BLE001 - 推送失败不能带停落库
            log.warn("inbound.publish_failed", vin=p.vin, err=str(exc))

    async def _flush(self, batch: list[TelemetryPoint]) -> None:
        try:
            stats = await self._svc.ingest(batch)
            self.stats.inserted += stats.inserted
            self.stats.dup_dropped += stats.duplicated
            if stats.rejected_stale:
                self.stats.stale_dropped += stats.rejected_stale
        except Exception as exc:
            log.error("inbound.flush_failed", count=len(batch), err=str(exc))

    def queue_depth(self) -> int:
        """当前待批量落库的点数（/stats 用，判断背压）。"""
        return self._queue.qsize()

    async def drain(self) -> None:
        """测试/关闭用：把队列清空。"""
        while not self._queue.empty():
            batch: list[TelemetryPoint] = []
            while not self._queue.empty() and len(batch) < get_settings().tele_batch_size:
                batch.append(self._queue.get_nowait())
            if batch:
                await self._flush(batch)
