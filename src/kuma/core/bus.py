"""RabbitMQ（AMQP）基座：下行指令队列 + 事件总线 + outbox 投递。

拓扑（Task-Server.md §4.3，启动时幂等声明）：
- `kuma.cmd.down`  持久队列，TTL 由指令超时决定，死信 → `kuma.cmd.dlx`
- `kuma.events`    topic 交换机，outbox 事件按 `aggregate_type.event_type` 路由
Redis 不做可靠队列（§4.1），只缓存/状态/pubsub。
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

import aio_pika
from aio_pika import DeliveryMode, ExchangeType, Message
from aio_pika.abc import (
    AbstractChannel,
    AbstractConnection,
    AbstractExchange,
    AbstractIncomingMessage,
    AbstractQueue,
)

from .config import get_settings
from .logging import get_logger

log = get_logger("bus")

Q_CMD_DOWN = "kuma.cmd.down"
Q_CMD_DLX = "kuma.cmd.dlx"
X_EVENTS = "kuma.events"


class Bus:
    def __init__(self) -> None:
        self._conn: AbstractConnection | None = None
        self._ch: AbstractChannel | None = None
        self._events: AbstractExchange | None = None

    async def connect(self) -> None:
        if self._conn is not None:
            return
        s = get_settings()
        self._conn = await aio_pika.connect_robust(
            s.amqp_url, heartbeat=60, detect_connections=True,
            connection_name=f"kuma-{s.role}",
        )
        self._ch = await self._conn.channel()
        await self._ch.set_qos(prefetch_count=200)

        self._events = await self._ch.declare_exchange(
            X_EVENTS, ExchangeType.TOPIC, durable=True, auto_delete=False
        )
        dlx = await self._ch.declare_exchange("kuma.cmd.dlx.x", ExchangeType.FANOUT, durable=True)
        dlq = await self._ch.declare_queue(Q_CMD_DLX, durable=True,
                                           arguments={"x-message-ttl": 86_400_000})
        await dlq.bind(dlx, routing_key="")
        await self._ch.declare_queue(
            Q_CMD_DOWN,
            durable=True,
            arguments={
                "x-dead-letter-exchange": "kuma.cmd.dlx.x",
                "x-message-ttl": get_settings().cmd_timeout * 1000,
            },
        )
        log.info("bus.connected")

    def is_connected(self) -> bool:
        conn = self._conn
        return conn is not None and not conn.is_closed

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
        self._conn = None
        self._ch = None
        self._events = None

    @property
    def channel(self) -> AbstractChannel:
        assert self._ch is not None, "Bus.connect() 未调用"
        return self._ch

    async def publish_cmd(self, payload: dict[str, Any]) -> None:
        """下行指令入队（gateway 消费后转 MQTT）。"""
        body = json.dumps(payload, ensure_ascii=False, default=str).encode()
        await self.channel.default_exchange.publish(
            Message(body, delivery_mode=DeliveryMode.PERSISTENT,
                    message_id=str(payload.get("cmd_id")),
                    content_type="application/json"),
            routing_key=Q_CMD_DOWN,
        )

    async def publish_event(self, routing_key: str, payload: dict[str, Any]) -> None:
        assert self._events is not None
        body = json.dumps(payload, ensure_ascii=False, default=str).encode()
        await self._events.publish(
            Message(body, delivery_mode=DeliveryMode.PERSISTENT, content_type="application/json"),
            routing_key=routing_key,
        )

    async def consume(self, queue: str, handler: Callable[[dict[str, Any]], Awaitable[None]],
                      *, concurrency: int = 50) -> AbstractQueue:
        q = await self.channel.declare_queue(queue, durable=True)
        await q.consume(self._wrap(handler), no_ack=False)
        await self.channel.set_qos(prefetch_count=concurrency)
        return q

    @staticmethod
    def _wrap(handler: Callable[[dict[str, Any]], Awaitable[None]]):
        async def _inner(msg: AbstractIncomingMessage) -> None:   # 收消息用 IncomingMessage，才有 ack/nack
            try:
                payload = json.loads(msg.body.decode())
                await handler(payload)
                await msg.ack()
            except Exception as exc:
                log.error("bus.handler_failed", err=str(exc))
                await msg.nack(requeue=False)  # 进死信，不无限重投
        return _inner


_bus: Bus | None = None


def get_bus() -> Bus:
    global _bus
    if _bus is None:
        _bus = Bus()
    return _bus


async def close_bus() -> None:
    global _bus
    if _bus is not None:
        await _bus.close()
    _bus = None
