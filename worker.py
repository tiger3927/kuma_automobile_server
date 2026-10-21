"""worker 进程入口：定时任务 + outbox + 分区维护（§12-8：任务加锁禁止重叠）。

启动顺序与 gateway 一致：连 AMQP → 起健康端口（8102）→ 跑调度循环。
8102 是 T3 的遗留项：**没有它，任务卡死 / 一直被锁跳过 / 反复失败，外部一律看不见**，
systemd 只知道进程还活着。
"""

from __future__ import annotations

import asyncio
import contextlib

import uvicorn
from fastapi import FastAPI

from src.kuma.core.bus import Q_EVENTS_UNROUTED, close_bus, get_bus
from src.kuma.core.config import get_settings
from src.kuma.core.db import dispose_engine, healthcheck
from src.kuma.core.logging import setup_logging
from src.kuma.core.redis_client import close as close_redis
from src.kuma.core.redis_client import healthcheck as redis_health
from src.kuma.tasks import Job, Scheduler, alarm_rules, offline_check, outbox, retention

settings = get_settings()
settings.role = "worker"

app = FastAPI(title="kuma worker")

_SCHED: Scheduler | None = None       # main() 里赋值；探针要读每个任务的状态

JOBS: list[Job] = []


async def _trip_close() -> None:
    from src.kuma.tasks.trip_splitter import close_idle_trips

    await close_idle_trips()


async def _outbox_publish() -> None:
    n = await outbox.publish_pending()
    if n:
        print(f'{{"event":"outbox.published","count":{n}}}')


# 必须在上面的函数定义之后构建：JOBS 是模块级列表，导入时就求值，
# 把函数写在后面会 NameError（ruff F821 抓到过这个）
JOBS.extend([
    Job("offline_check", offline_check.run, interval_s=120, lock_ttl=110),
    Job("trip_close", _trip_close, interval_s=180, lock_ttl=170),
    Job("alarm_rules", alarm_rules.overspeed_and_brake, interval_s=300, lock_ttl=290),
    Job("fence_exit", alarm_rules.fence_exit, interval_s=600, lock_ttl=590),
    Job("mileage_anomaly", alarm_rules.mileage_anomaly, interval_s=1800, lock_ttl=1700),
    Job("outbox_publish", _outbox_publish, interval_s=5, lock_ttl=10),
    Job("retention", retention.ensure_partitions, interval_s=3600, lock_ttl=3500),
    Job("rep_refresh", retention.refresh_rep_views, interval_s=3600, lock_ttl=3500),
])


@app.get("/healthz")
async def healthz() -> dict:
    """依赖健康 + **任务新鲜度**。

    判读口径：
    - `stale_jobs` 非空 → `degraded`：某个任务超过 3 个周期没成功收尾（卡死、
      锁没释放、或一直抛错）。这**不是**让 systemd 重启的信号——重启只会把卡死
      变成"重启后继续卡死"，要人去看 `job.*` 日志。
    - `events_unrouted.messages > 0` → outbox 发出去的事件没人接（T7 未定频道），
      现在不会丢（落在兜底队列），但**必须看得见**；`consumers=0` 即"没有消费方"。
      这是已知状态，故不影响 `status`。
    """
    pg = await healthcheck()
    redis = await redis_health()
    bus = get_bus()
    mq = {"status": "ok"} if bus.is_connected() else {"status": "error"}
    sched = _SCHED.stats() if _SCHED is not None else {"status": "starting", "stale_jobs": []}
    unrouted = await bus.queue_depth(Q_EVENTS_UNROUTED) if bus.is_connected() else {"messages": -1, "consumers": -1}
    ok = all(x["status"] == "ok" for x in (pg, redis, mq)) and sched["status"] == "ok"
    return {"status": "ok" if ok else "degraded", "role": settings.role,
            "pg": pg, "redis": redis, "mq": mq,
            "stale_jobs": sched["stale_jobs"],
            "jobs_total": len(JOBS),
            "events_unrouted": unrouted}


@app.get("/jobs")
async def jobs() -> dict:
    """每个任务的 runs / failures / timeouts / skipped_locked / 上次耗时 / 最近错误。"""
    if _SCHED is None:
        return {"status": "starting"}
    return _SCHED.stats()


async def main() -> None:
    setup_logging(settings.role, json_logs=settings.is_prod)
    await get_bus().connect()          # outbox 要发事件

    server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=settings.worker_health_port, log_config=None,
        access_log=False,
    ))
    health_task = asyncio.create_task(server.serve(), name="worker-health")

    global _SCHED
    scheduler = Scheduler(settings, JOBS)
    _SCHED = scheduler
    try:
        await scheduler.run()
    finally:
        server.should_exit = True
        with contextlib.suppress(Exception):
            await health_task
        await close_bus()
        await close_redis()
        await dispose_engine()


if __name__ == "__main__":
    asyncio.run(main())
