"""FastAPI 应用工厂（api 进程）。

`/healthz` 是 M0/M1 验收项：pg / redis / mq 三项全绿才算健康。
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import ORJSONResponse

from ..core.bus import get_bus
from ..core.config import Settings, get_settings
from ..core.db import healthcheck as pg_health
from ..core.exceptions import register_handlers
from ..core.lifespan import lifespan
from ..core.middleware import RateLimit, RequestContext
from ..core.redis_client import healthcheck as redis_health
from ..realtime.manager import manager
from .v1 import api_router


def create_app(settings: Settings | None = None) -> FastAPI:
    s = settings or get_settings()

    async def _startup() -> None:
        await manager.start()

    async def _shutdown() -> None:
        await manager.stop()          # 先停 pubsub（§12-4）

    app = FastAPI(
        title="kuma 车联网服务", version="0.1.0",
        default_response_class=ORJSONResponse,
        docs_url="/docs" if not s.is_prod else None,
        lifespan=lambda a: lifespan(a, s, on_startup=_startup, on_shutdown=_shutdown),
    )
    app.add_middleware(RequestContext)
    app.add_middleware(RateLimit, rules={"/api/v1/public/": 120})
    register_handlers(app)

    app.include_router(api_router, prefix="/api/v1")

    @app.get("/healthz")
    async def healthz() -> dict:
        pg = await pg_health()
        redis = await redis_health()
        mq = {"status": "ok"} if get_bus().is_connected() else {"status": "error"}
        status = "ok" if all(x["status"] == "ok" for x in (pg, redis, mq)) else "degraded"
        return {"status": status, "role": s.role, "pg": pg, "redis": redis, "mq": mq}

    @app.get("/metrics")
    async def metrics() -> dict:
        from ..core.redis_client import get_redis

        return {"role": s.role, "ws_connections": len(manager._by_vin),
                "redis_ping": (await get_redis().ping())}

    return app
