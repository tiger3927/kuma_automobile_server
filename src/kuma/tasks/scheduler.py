"""worker 调度循环：每个任务加 Redis 锁 + 超时，禁止重叠（§12-8）。

**每个任务都留状态**（`Job.state`），由 `worker.py` 的 8102 探针暴露：
进程活着但任务卡死 / 一直被锁跳过 / 反复失败，systemd 是看不见的（T3 遗留项）。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import asdict, dataclass, field

from ..core.config import Settings
from ..core.logging import get_logger
from ..core.redis_client import acquire_lock, release_lock

log = get_logger("tasks.scheduler")


@dataclass
class JobState:
    """单个任务的可观测面。计数只增不清零，`last_*` 是 epoch 秒（服务器 UTC）。"""

    runs: int = 0
    failures: int = 0
    timeouts: int = 0
    skipped_locked: int = 0
    last_started: float | None = None
    last_finished: float | None = None
    last_duration_ms: int | None = None
    last_error: str | None = None


@dataclass
class Job:
    name: str
    fn: Callable[[], Awaitable[None]]
    interval_s: int
    lock_ttl: int = 300
    state: JobState = field(default_factory=JobState)

    def age_s(self) -> float | None:
        """距上次**成功结束**的秒数；从没成功过返回 None（探针按 stale 处理）。"""
        if self.state.last_finished is None:
            return None
        return max(0.0, time.time() - self.state.last_finished)

    def is_stale(self) -> bool:
        """超过 3 个周期 + 一把锁的 TTL 还没成功收尾 = 卡死或一直在丢。

        加 `lock_ttl` 是因为长任务本身就要跑很久；乘 3 是给"上一轮刚跑完"留余量，
        避免把正常的调度抖动报成故障（`outbox_publish` 5s 一轮，阈值 ≈ 25s）。
        """
        age = self.age_s()
        return age is None or age > self.interval_s * 3 + self.lock_ttl

    async def run_once(self) -> None:
        token = await acquire_lock(self.name, ttl=self.lock_ttl)
        if token is None:
            self.state.skipped_locked += 1
            log.info("job.skipped_locked", job=self.name)
            return
        self.state.runs += 1
        self.state.last_started = time.time()
        started = time.monotonic()
        try:
            await asyncio.wait_for(self.fn(), timeout=self.interval_s * 2)
            self.state.last_finished = time.time()
            self.state.last_duration_ms = int((time.monotonic() - started) * 1000)
            self.state.last_error = None
        except TimeoutError:
            self.state.timeouts += 1
            self.state.last_error = "timeout"
            log.error("job.timeout", job=self.name)
        except Exception as exc:
            self.state.failures += 1
            self.state.last_error = str(exc)
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

    def stats(self) -> dict:
        """给 8102 探针用：任一任务 stale 就整体 degraded（外部据此告警，不是让 systemd 重启）。"""
        jobs = []
        for j in self.jobs:
            stale = j.is_stale()
            jobs.append({
                "name": j.name, "interval_s": j.interval_s, "stale": stale,
                "age_s": None if j.age_s() is None else round(j.age_s() or 0.0, 1),
                **asdict(j.state),
            })
        stale_names = [j["name"] for j in jobs if j["stale"]]
        return {"status": "ok" if not stale_names else "degraded",
                "stale_jobs": stale_names, "jobs": jobs}

    async def _loop(self, job: Job) -> None:
        while not self._stop.is_set():
            await job.run_once()
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=job.interval_s)
            except TimeoutError:
                continue
