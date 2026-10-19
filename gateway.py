"""MQTT 网关进程入口（**单实例**，防重复订阅，§12-7）。

启动顺序：连 AMQP → 起健康端口 → 跑 MQTT 订阅循环。
关闭顺序：停订阅 → 刷批量缓冲 → 关 AMQP → 关 DB（core.lifespan 之外手动管理）。
"""

from __future__ import annotations

import asyncio
import contextlib

import uvicorn
from fastapi import FastAPI

from src.kuma.core.bus import close_bus, get_bus
from src.kuma.core.config import get_settings
from src.kuma.core.db import dispose_engine, healthcheck
from src.kuma.core.logging import setup_logging
from src.kuma.core.redis_client import close as close_redis
from src.kuma.core.redis_client import healthcheck as redis_health
from src.kuma.mqtt.gateway import MqttGateway

settings = get_settings()
settings.role = "gateway"

app = FastAPI(title="kuma gateway")


@app.get("/healthz")
async def healthz() -> dict:
    pg = await healthcheck()
    redis = await redis_health()
    mq = {"status": "ok"} if get_bus().is_connected() else {"status": "error"}
    ok = all(x["status"] == "ok" for x in (pg, redis, mq))
    return {"status": "ok" if ok else "degraded", "role": settings.role,
            "pg": pg, "redis": redis, "mq": mq}


async def main() -> None:
    setup_logging(settings.role, json_logs=settings.is_prod)
    await get_bus().connect()

    server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=settings.gateway_health_port, log_config=None,
        access_log=False,
    ))
    health_task = asyncio.create_task(server.serve(), name="gateway-health")

    gateway = MqttGateway(settings)   # 下行队列在 MQTT 就绪后由 run() 内部绑定
    try:
        await gateway.run()
    finally:
        await gateway.shutdown()
        server.should_exit = True
        with contextlib.suppress(Exception):
            await health_task
        await close_bus()
        await close_redis()
        await dispose_engine()


if __name__ == "__main__":
    asyncio.run(main())
