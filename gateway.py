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

_GW: MqttGateway | None = None      # main() 里赋值；/stats 要读它的入站计数器


@app.get("/healthz")
async def healthz() -> dict:
    pg = await healthcheck()
    redis = await redis_health()
    mq = {"status": "ok"} if get_bus().is_connected() else {"status": "error"}
    ok = all(x["status"] == "ok" for x in (pg, redis, mq))
    return {"status": "ok" if ok else "degraded", "role": settings.role,
            "pg": pg, "redis": redis, "mq": mq}


@app.get("/stats")
async def stats() -> dict:
    """入站计数器（只绑 127.0.0.1 的健康端口）。

    M3 验收的"零丢包"必须看这里：`inserted` 会**合法地**小于客户端发送数
    （deadband 过滤静止近点、dedup 挡重复投递、补报窗口外的点丢弃），
    真正的丢是 `queue_dropped`（背压）与 `received` 对不上客户端确认数。
    """
    if _GW is None:
        return {"status": "starting"}
    s = _GW.inbound.stats
    return {"status": "ok", "mqtt_connected": _GW.is_connected(),
            "queue_depth": _GW.inbound.queue_depth(),
            "received": s.received, "inserted": s.inserted,
            "dup_dropped": s.dup_dropped, "stale_dropped": s.stale_dropped,
            "queue_dropped": s.queue_dropped, "decoded_error": s.decoded_error,
            # 单车速率闸门（§8.7）丢弃数。持续非 0 只有两种解释：
            # 有设备在狂发，或 KUMA_RATE_LIMIT_PER_MIN 设得比固件上报频率还低。
            "rate_limited": _GW.rate_limited}


async def main() -> None:
    setup_logging(settings.role, json_logs=settings.is_prod)
    await get_bus().connect()

    server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=settings.gateway_health_port, log_config=None,
        access_log=False,
    ))
    health_task = asyncio.create_task(server.serve(), name="gateway-health")

    global _GW
    gateway = MqttGateway(settings)   # 下行队列在 MQTT 就绪后由 run() 内部绑定
    _GW = gateway
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
