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
from contextlib import suppress
from typing import Any
from urllib.parse import quote, unquote, urlparse

import aio_pika
import httpx
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
X_EVENTS_AE = "kuma.events.ae"          # alternate exchange：接住无路由事件
Q_EVENTS_UNROUTED = "kuma.events.unrouted"


class Bus:
    def __init__(self) -> None:
        self._conn: AbstractConnection | None = None
        self._ch: AbstractChannel | None = None
        self._events: AbstractExchange | None = None
        # 兜底是否真的生效（老 broker 上遗留的 kuma.events 不带 AE → False）。
        # 探针必须把它报出来，否则 `unrouted=0` 会被误读成"没有事件丢失"。
        self.events_ae = False

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

        # ⚠️ exchange 的 arguments **建好后不能改**，而声明冲突（406 PRECONDITION_FAILED）
        # 会**关掉整条 channel**——2026-10-21 实测：先带 AE 声明、接住异常再 passive 重声明，
        # 第二步必炸（channel 已死）。所以**必须先探测再决定怎么建**，且探测用**独立 channel**
        # （passive 查不存在的对象同样会关 channel，别污染主 channel）。
        # 探测本身**就是创建**：非 passive 声明在"不存在"时建出来、在"已存在且参数一致"时成功、
        # 只有"已存在但参数不同"才 406。所以一次探测能分出三种情况，比 passive 只看存在更准
        # （passive 读不到 arguments，会把已带 AE 的交换机误报成兜底没生效）。
        state = await self._probe_events_exchange()
        self.events_ae = state != "legacy"
        if state == "legacy":
            log.warning("bus.events_exchange_legacy",
                        hint="既有 kuma.events 不带 alternate-exchange，无路由事件仍会静默丢弃；"
                             f"删掉旧交换机（它通常没有绑定与消息）让本进程重建，"
                             f"即可拿到 {Q_EVENTS_UNROUTED} 兜底")
        # 到这里交换机一定存在（探测阶段已建好或确认在），主 channel 只取句柄。
        self._events = await self._ch.declare_exchange(X_EVENTS, ExchangeType.TOPIC, passive=True)
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

    async def _probe_events_exchange(self) -> str:
        """返回 `with_ae`（已带兜底，或本次建出来）/ `legacy`（存在但不带兜底）。

        必须用**独立 channel**：406 与 404 都会关掉所在 channel（2026-10-21 实测），
        拿主 channel 试会把后面所有声明一起带崩。
        """
        assert self._conn is not None, "connect() 未调用"
        probe = await self._conn.channel()
        try:
            await probe.declare_exchange(
                X_EVENTS, ExchangeType.TOPIC, durable=True, auto_delete=False,
                arguments={"alternate-exchange": X_EVENTS_AE},
            )
            return "with_ae"
        except Exception as exc:
            if "PRECONDITION_FAILED" in str(exc):
                return "legacy"
            log.warning("bus.events_probe_failed", err=str(exc))
            return "legacy"          # 探测不出来就按"没有兜底"报，宁可保守
        finally:
            with suppress(Exception):
                await probe.close()

    def is_connected(self) -> bool:
        conn = self._conn
        return conn is not None and not conn.is_closed

    async def queue_depth(self, queue: str) -> dict[str, int]:
        """队列深度与消费者数——**必须走 management HTTP API**。

        探针用它看 `kuma.events.unrouted`：深度 > 0 就说明事件发出去没人接（T7 未定频道），
        `consumers=0` 即"没有消费方"。查不到返回 -1，不抛。

        为什么不用 AMQP：`queue.declare-ok` 里的 message_count/consumer_count 在 RabbitMQ
        已废弃且不保证准确——2026-10-21 实测 management 报 `messages=1`，同一时刻
        passive declare 报 0。**探针报 0 比不报更糟**（会让人以为黑洞是空的）。
        """
        base, auth, vhost = self._mgmt()
        url = f"{base}/api/queues/{quote(vhost, safe='')}/{quote(queue, safe='')}"
        try:
            async with httpx.AsyncClient(timeout=3.0) as cli:
                r = await cli.get(url, auth=auth)
        except Exception as exc:
            log.warning("bus.queue_depth_failed", queue=queue, err=str(exc))
            return {"messages": -1, "consumers": -1}
        if r.status_code == 404:
            return {"messages": -1, "consumers": -1}
        if r.status_code != 200:
            log.warning("bus.queue_depth_http", queue=queue, status=r.status_code)
            return {"messages": -1, "consumers": -1}
        d = r.json()
        return {"messages": int(d.get("messages") or 0), "consumers": int(d.get("consumers") or 0)}

    @staticmethod
    def _mgmt() -> tuple[str, tuple[str, str], str]:
        """管理口地址与凭据：默认复用 `amqp_url` 的 host（端口 15672），
        可用 `KUMA_MQ_MGMT_URL` 覆盖。凭据只用于这次请求，不进日志。"""
        s = get_settings()
        u = urlparse(s.amqp_url)
        base = (s.mq_mgmt_url or f"http://{u.hostname}:15672").rstrip("/")
        vhost = unquote(u.path.lstrip("/")) or "/"
        return base, (u.username or "", u.password or ""), vhost

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
