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
from ..repositories.state_repo import StateRepo
from ..services.alarm_service import raise_alarm
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
        # 单车速率闸门的计数（/stats 暴露）。0 意味着没有车被限流；
        # 持续非 0 说明有设备在狂发，或 `KUMA_RATE_LIMIT_PER_MIN` 设得比固件上报频率还低。
        self.rate_limited = 0

    async def run(self) -> None:
        await self.inbound.start()
        async with aiomqtt.Client(
            self.s.mqtt_host,
            port=self.s.mqtt_port,
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

            # passive=True：队列已由 Bus.connect() 带 x-message-ttl 声明过，
            # 再裸声明会 PRECONDITION_FAILED（见 core/bus.py:consume）
            await get_bus().consume(Q_CMD_DOWN, self._outbound.handle, passive=True)
            log.info("gateway.cmd_queue_bound", queue=Q_CMD_DOWN)
            self._tasks.append(asyncio.create_task(loop_retry(), name="cmd-retry-sweep"))
            async for message in client.messages:
                await self._route(str(message.topic), message.payload)

    def is_connected(self) -> bool:
        """MQTT 订阅连接是否在（/stats 用；AMQP 下行连接由 core.bus 自己报）。"""
        return self._mqtt is not None

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

        # 单车速率闸门（§8.7）。2026-10-22 之前 `rate_check()` 已写好但**从未被调用**，
        # 这条闸门实际不存在：一台车狂发只受全局队列背压约束，别人的车跟着遭殃。
        # 放在 decode 之前——被限流的消息不该再消耗 codec CPU。
        if not await rate_check(vin, self.s.rate_limit_per_min):
            self.rate_limited += 1
            if self.rate_limited % 50 == 1:      # 限流留痕本身也要限频，否则日志会被刷爆
                log.warn("gateway.rate_limited", vin=vin, total=self.rate_limited,
                         limit_per_min=self.s.rate_limit_per_min)
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
                created = await raise_alarm(
                    session, vin=vin, ts=decoded.ts or _now(), type_=decoded.alarm["type"],
                    level=decoded.alarm["level"], payload=decoded.alarm["payload"])
            if created:
                # 两条路径各有用途，不是重复推送：
                # ① 这里直推 Redis（毫秒级）——设备自己报的告警（碰撞、断电）必须立刻弹窗；
                # ② outbox 的 `alarm.created`（worker 每 5s 投）——带 alarm id，给其它消费者
                #    与前端列表刷新用。事件名不同（`alarm` vs `alarm.created`），前端不会混。
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
