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
from ..mqtt import auth as mqtt_auth
from ..realtime import events_bridge
from ..realtime.manager import manager
from .v1 import api_router


def create_app(settings: Settings | None = None) -> FastAPI:
    s = settings or get_settings()

    async def _startup() -> None:
        await manager.start()
        # C1：业务事件（outbox → kuma.events）→ Redis pubsub → WS。
        # 必须在 bus 已连上之后（lifespan 里 connect 先于 on_startup）。
        await events_bridge.start()

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

    if s.mq_auth_token:
        # RabbitMQ `rabbitmq_auth_backend_http` 的回调入口（T1，见 mqtt/auth.py）。
        # 路径里带随机段：8100 被 nginx 全量反代到公网，不带 token 的调用只会拿到 404，
        # 这个端点不会被人当"口令探针"枚举。token 只落在 root-only 的 rabbitmq.conf 与 runtime.env。
        # 故意不进 RateLimit 规则：topic 权限每次 publish 都要判一次（1000 台 @5s ≈ 200 次/秒）。
        app.include_router(mqtt_auth.router, prefix=f"/internal/mqtt-auth/{s.mq_auth_token}")

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
                "redis_ping": (await get_redis().ping()),
                # C1：事件桥的投递计数（0 且 outbox 有行 = 桥没接上）
                **events_bridge.counters(),
                # C9：连接闸门拒绝数（持续增长 = 设备侧退避没生效或在被攻击）
                "mqtt_connect_denied": mqtt_auth.connect_denied}

    return app
