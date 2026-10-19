"""worker 调度循环：每个任务加 Redis 锁 + 超时，禁止重叠（§12-8）。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress

from ..core.config import Settings
from ..core.logging import get_logger
from ..core.redis_client import acquire_lock, release_lock

log = get_logger("tasks.scheduler")


class Job:
    def __init__(self, name: str, fn: Callable[[], Awaitable[None]], *, interval_s: int,
                 lock_ttl: int = 300) -> None:
        self.name = name
        self.fn = fn
        self.interval_s = interval_s
        self.lock_ttl = lock_ttl

    async def run_once(self) -> None:
        token = await acquire_lock(self.name, ttl=self.lock_ttl)
        if token is None:
            log.info("job.skipped_locked", job=self.name)
            return
        try:
            await asyncio.wait_for(self.fn(), timeout=self.interval_s * 2)
        except TimeoutError:
            log.error("job.timeout", job=self.name)
        except Exception as exc:
            log.error("job.failed", job=self.name, err=str(exc))
        finally:
            await release_lock(token)


class Scheduler:
    def __init__(self, settings: Settings, jobs: list[Job]) -> None:
        self.s = settings
        self.jobs = jobs
        self._stop = asyncio.Event()

    async def run(self) -> None:
        log.info("worker.started", jobs=[j.name for j in self.jobs])
        tasks = [asyncio.create_task(self._loop(j), name=f"job-{j.name}") for j in self.jobs]
        await self._stop.wait()
        for t in tasks:
            t.cancel()
        for t in tasks:
            with suppress(asyncio.CancelledError, Exception):   # 停机：吞掉取消与任务内异常
                await t

    def stop(self) -> None:
        self._stop.set()

    async def _loop(self, job: Job) -> None:
        while not self._stop.is_set():
            await job.run_once()
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=job.interval_s)
            except TimeoutError:
                continue
