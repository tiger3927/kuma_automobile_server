"""MQTT 网关（单实例，防重复订阅，§12-7）。

职责：订阅 → VIN 防伪校验 → codec 解码 → 入站管道 / 告警 / 指令回执。
换 EMQX 时只改这里的连接与 ACL，不改 codec 与 services（§3-3）。
"""

from __future__ import annotations

import asyncio

import aiomqtt

from ..core.config import Settings
from ..core.db import tx
from ..core.logging import get_logger
from ..core.redis_client import get_redis, set_ttl
from ..repositories.alarm_repo import AlarmRepo
from ..repositories.state_repo import StateRepo
from ..services.command_service import CommandService
from .codecs import decode
from .inbound import InboundPipeline
from .outbound import Outbound, loop_retry
from .topics import ALL_UP, topic_matches_vin, vin_from_topic

log = get_logger("mqtt.gateway")


class MqttGateway:
    def __init__(self, settings: Settings) -> None:
        self.s = settings
        self.inbound = InboundPipeline()
        self._mqtt: aiomqtt.Client | None = None
        self._outbound: Outbound | None = None
        self._tasks: list[asyncio.Task] = []

    async def run(self) -> None:
        await self.inbound.start()
        async with aiomqtt.Client(
            self.s.mqtt_url,
            username=self.s.mqtt_user,
            password=self.s.mqtt_password or None,
            identifier="kuma-gateway",
            keepalive=60,
            protocol=aiomqtt.ProtocolVersion.V5,
        ) as client:
            self._mqtt = client
            self._outbound = Outbound(client)
            await client.subscribe(ALL_UP, qos=1)
            log.info("gateway.subscribed", topic=ALL_UP)
            # MQTT 就绪后才接 AMQP 下行队列，避免指令发到没有 MQTT 连接的进程
            from ..core.bus import Q_CMD_DOWN, get_bus

            await get_bus().consume(Q_CMD_DOWN, self._outbound.handle)
            log.info("gateway.cmd_queue_bound", queue=Q_CMD_DOWN)
            self._tasks.append(asyncio.create_task(loop_retry(), name="cmd-retry-sweep"))
            async for message in client.messages:
                await self._route(str(message.topic), message.payload)

    async def shutdown(self) -> None:
        for t in self._tasks:
            t.cancel()
        await self.inbound.stop()          # 先刷缓冲，再断 MQTT

    async def _route(self, topic: str, payload: bytes) -> None:
        vin = vin_from_topic(topic)
        if vin is None:
            return
        # VIN 防伪：连接用户名（kuma_mq 网关账号）与 topic 不一致时，
        # 车端凭据由 RabbitMQ ACL 保证；这里再校验 topic 结构合法性并留痕。
        if not topic_matches_vin(topic, vin):
            log.warn("gateway.vin_mismatch", topic=topic)
            return

        decoded = decode(payload)
        if decoded.kind == "telemetry":
            self.inbound.offer(decoded)
            return
        if decoded.kind == "heartbeat":
            await StateRepo().update(vin, {"ts": decoded.ts, "lat": None, "lon": None,
                                           "speed_kmh": None, "soc": None, "odo_km": None})
            return
        if decoded.kind == "alarm" and decoded.alarm:
            async with tx() as session:
                created = await AlarmRepo().raise_alarm(
                    session, vin=vin, ts=decoded.ts or _now(), type_=decoded.alarm["type"],
                    level=decoded.alarm["level"], payload=decoded.alarm["payload"])
            if created:
                await _notify_alarm(vin, decoded.alarm)
            return
        if decoded.kind == "cmd_ack" and decoded.ack:
            async with tx() as session:
                await CommandService().handle_ack(session, cmd_id=decoded.ack["cmd_id"],
                                                  result=decoded.ack["result"])
            return
        if decoded.kind == "error":
            log.warn("gateway.decode_error", topic=topic, error=decoded.error)


async def _notify_alarm(vin: str, alarm: dict) -> None:
    from ..realtime.broadcaster import publish_vin

    await publish_vin(vin, "alarm", alarm)


async def rate_check(vin: str, limit_per_min: int = 60) -> bool:
    """单连接消息速率上限（§8.7）：超限计数并丢弃。"""
    key = f"kuma:rate:{vin}"
    r = get_redis()
    count = await r.incr(key)
    if count == 1:
        await set_ttl(key, "1", 60)
    return count <= limit_per_min


def _now():
    from datetime import UTC, datetime

    return datetime.now(UTC)
