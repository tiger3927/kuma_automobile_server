"""入站管道：有界队列 → 批量 COPY（500 条 / 1s）→ 状态与广播。

背压是 4G 内存机器上的保命项（§12-6）：队列满时丢"低优先级"（心跳/重复点）并计数告警。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from ..core.config import get_settings
from ..core.logging import get_logger
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
        s = get_settings()
        batch: list[TelemetryPoint] = []
        while True:
            closing = self._stop.is_set()
            timeout = 0.0 if closing else s.tele_flush_interval
            try:
                p = await asyncio.wait_for(self._queue.get(), timeout=timeout)
                batch.append(p)
                if len(batch) < s.tele_batch_size and not closing:
                    continue
            except TimeoutError:
                pass
            if batch:
                await self._flush(batch)
                batch = []
            if closing and self._queue.empty():
                return

    async def _flush(self, batch: list[TelemetryPoint]) -> None:
        try:
            stats = await self._svc.ingest(batch)
            self.stats.inserted += stats.inserted
            self.stats.dup_dropped += stats.duplicated
            if stats.rejected_stale:
                self.stats.stale_dropped += stats.rejected_stale
        except Exception as exc:
            log.error("inbound.flush_failed", count=len(batch), err=str(exc))

    async def drain(self) -> None:
        """测试/关闭用：把队列清空。"""
        while not self._queue.empty():
            batch: list[TelemetryPoint] = []
            while not self._queue.empty() and len(batch) < get_settings().tele_batch_size:
                batch.append(self._queue.get_nowait())
            if batch:
                await self._flush(batch)
