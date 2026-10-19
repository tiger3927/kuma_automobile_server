"""进程生命周期（关闭顺序按 Task-Server.md §12-4：停 pubsub → 刷缓冲 → 关 MQ → 关 DB 池）。"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .bus import close_bus, get_bus
from .config import Settings
from .db import dispose_engine, get_engine
from .logging import get_logger, setup_logging
from .redis_client import close as close_redis

log = get_logger("lifespan")


@asynccontextmanager
async def lifespan(
    app: FastAPI,
    settings: Settings,
    *,
    on_startup: Callable[[], Awaitable[None]] | None = None,
    on_shutdown: Callable[[], Awaitable[None]] | None = None,
) -> AsyncIterator[None]:
    setup_logging(settings.role, json_logs=settings.is_prod)
    get_engine(settings)
    if settings.role == "api":
        await get_bus().connect()          # 下行指令要经 AMQP，api 启动即连
    if on_startup:
        await on_startup()
    log.info("process.started", role=settings.role, env=settings.kuma_env)
    try:
        yield
    finally:
        if on_shutdown:
            await on_shutdown()            # 先刷缓冲 / 停订阅
        await close_bus()
        await close_redis()
        await dispose_engine()
        log.info("process.stopped", role=settings.role)
