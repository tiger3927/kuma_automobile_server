"""worker 进程入口：定时任务 + outbox + 分区维护（§12-8：任务加锁禁止重叠）。"""

from __future__ import annotations

import asyncio

from src.kuma.core.bus import close_bus, get_bus
from src.kuma.core.config import get_settings
from src.kuma.core.db import dispose_engine
from src.kuma.core.logging import setup_logging
from src.kuma.core.redis_client import close as close_redis
from src.kuma.tasks import Job, Scheduler, alarm_rules, offline_check, outbox, retention

settings = get_settings()
settings.role = "worker"

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


async def main() -> None:
    setup_logging(settings.role, json_logs=settings.is_prod)
    await get_bus().connect()          # outbox 要发事件
    scheduler = Scheduler(settings, JOBS)
    try:
        await scheduler.run()
    finally:
        await close_bus()
        await close_redis()
        await dispose_engine()


if __name__ == "__main__":
    asyncio.run(main())
