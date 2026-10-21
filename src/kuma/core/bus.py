"""RabbitMQ（AMQP）基座：下行指令队列 + 事件总线 + outbox 投递。

拓扑（Task-Server.md §4.3，启动时幂等声明）：
- `kuma.cmd.down`  持久队列，TTL 由指令超时决定，死信 → `kuma.cmd.dlx`
- `kuma.events`    topic 交换机，outbox 事件按 `aggregate_type.event_type` 路由
- `kuma.events.ae` → `kuma.events.unrouted`  **兜底**：topic 交换机没有任何绑定时消息会被
  broker 静默丢弃，而业务队列（T7）尚未定，所以给 `kuma.events` 挂 alternate exchange，
  无路由的事件落到 `kuma.events.unrouted` 队列——**不丢，且可被探针计数**（`/healthz` 报深度）。
  等 T7 定了消费频道，补上绑定即可，期间积压的事件还能回放。
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
from aio_pika.exceptions import ChannelPreconditionFailed

from .config import get_settings
from .logging import get_logger

log = get_logger("bus")

Q_CMD_DOWN = "kuma.cmd.down"
Q_CMD_DLX = "kuma.cmd.dlx"
X_EVENTS = "kuma.events"
X_EVENTS_AE = "kuma.events.ae"          # alternate exchange：接住无路由事件
Q_EVENTS_UNROUTED = "kuma.events.unrouted"


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

        # 兜底链先建：AE → unrouted 队列。上限是为了 4G 机器保命（§3）：
        # 事件本身不进 PG，积压到上限就拒收，宁可丢兜底也不能压垮 broker。
        ae = await self._ch.declare_exchange(X_EVENTS_AE, ExchangeType.FANOUT, durable=True)
        unrouted = await self._ch.declare_queue(
            Q_EVENTS_UNROUTED, durable=True,
            arguments={"x-message-ttl": 7 * 86_400_000,
                       "x-max-length": 50_000, "x-overflow": "reject-publish"},
        )
        await unrouted.bind(ae, routing_key="")

        try:
            self._events = await self._ch.declare_exchange(
                X_EVENTS, ExchangeType.TOPIC, durable=True, auto_delete=False,
                arguments={"alternate-exchange": X_EVENTS_AE},
            )
        except ChannelPreconditionFailed:
            # broker 上已有不带 AE 的同名交换机（早期版本建的）。arguments 不能改，
            # 只能删掉旧的让这里重建；降级声明保证进程不因运维滞后而崩（同 §consume 的教训）。
            log.warning("bus.events_exchange_legacy",
                        hint="既有 kuma.events 不带 alternate-exchange，无路由事件仍会静默丢弃；"
                             f"删掉旧交换机让本进程重建即可拿到 {Q_EVENTS_UNROUTED} 兜底")
            self._events = await self._ch.declare_exchange(
                X_EVENTS, ExchangeType.TOPIC, passive=True)
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

    async def queue_depth(self, queue: str) -> dict[str, int]:
        """只读查队列深度与消费者数（`passive=True` 不建、不改参数）。

        探针用它看 `kuma.events.unrouted`：**深度 > 0 就说明事件发出去没人接**，
        `consumers=0` 则说明频道还没接上（T7）。查不到队列返回 -1，不抛。
        """
        try:
            q = await self.channel.declare_queue(queue, passive=True)
        except Exception as exc:
            log.warning("bus.queue_depth_failed", queue=queue, err=str(exc))
            return {"messages": -1, "consumers": -1}
        # `AbstractQueue` 接口没声明这两个属性（mypy 报 attr-defined），但 aio_pika 的具体
        # Queue 类会从 broker 的 queue.declare-ok 里带回它们。用 getattr 同时兜住类型与版本差异，
        # 取不到就报 -1（**宁可说"不知道"也不要报 0**，否则探针会假装黑洞是空的）。
        messages = getattr(q, "message_count", None)
        consumers = getattr(q, "consumer_count", None)
        if messages is None or consumers is None:
            log.warning("bus.queue_depth_unsupported", queue=queue, queue_type=type(q).__name__)
            return {"messages": -1, "consumers": -1}
        return {"messages": int(messages), "consumers": int(consumers)}

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
                      *, concurrency: int = 50, passive: bool = False) -> AbstractQueue:
        """passive=True 只查不建。

        重复声明时 arguments 必须与既有队列**完全一致**：`connect()` 给 `kuma.cmd.down`
        带了 `x-message-ttl`，这里再裸声明就 `PRECONDITION_FAILED - inequivalent arg
        'x-message-ttl'`（2026-10-20 gateway 首次真连 MQ 时崩在这里，systemd 无限重启）。
        """
        q = await self.channel.declare_queue(queue, durable=True, passive=passive)
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
