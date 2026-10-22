"""事件总线消费者（C1，2026-10-22 决策）。

`kuma.events` 此前两端都空：没有生产方（`Outbox` 没人写），也没有消费方
（所有事件落到 `kuma.events.unrouted` 兜底队列，`consumers=0`）。补消费者不能凭想象造：
通知渠道、积分入账、报表聚合都要等 M8+ 的业务表（C10）落地才有意义，
现在写出来就是空转的假管道。

v1 唯一有真实用途的消费者是 **WS 桥**：api 进程持有 WebSocket 连接，业务事件
（告警确认、指令状态、行程结束、订阅到期、凭据轮换）必须能推给前端，
否则前端只能靠轮询 REST。链路是：

    services → audit.outbox（同事务）→ worker `outbox_publish` → `kuma.events`
        → 本消费者（api 进程）→ Redis pubsub `kuma:rt:*` → WS 扇出

为什么不直连 WS：api 是多 worker 进程（§12-7），AMQP 消息只会被一个消费者拿走，
而 WS 连接分散在各进程；Redis pubsub 天然广播，每个 api 进程都能扇出自己手里的连接。

路由口径（v1）：payload 带 `vin` 就发给订阅该车的连接；带 `accounts`（账号 id 列表）
就额外发给这些用户的个人频道。两者都没有的事件（如围栏）v1 不推——
管理端还没有全局事件订阅，先计数留痕，别默默丢掉。
"""

from __future__ import annotations

from typing import Any

from ..core.bus import get_bus
from ..core.logging import get_logger
from .broadcaster import publish_user, publish_vin

log = get_logger("events_bridge")

Q_EVENTS_WS = "kuma.events.ws"
# 只订阅"有人在等"的频道；新增绑定必须同时有对应的生产方，否则等于没加。
BINDINGS: tuple[str, ...] = (
    "alarm.#", "command.#", "trip.#", "subscription.#",
    "vehicle.#",          # vehicle.status / vehicle.ownership / vehicle.credential.*
)

delivered = 0
no_target = 0


async def handle(evt: dict[str, Any]) -> None:
    global delivered, no_target
    aggregate_type = evt.get("aggregate_type")
    event_type = evt.get("event_type")
    if not aggregate_type or not event_type:
        log.warn("events_bridge.malformed", keys=sorted(evt)[:6])
        return
    name = f"{aggregate_type}.{event_type}"
    payload = evt.get("payload") or {}

    vin = payload.get("vin")
    if vin:
        await publish_vin(str(vin), name, payload)
    for account_id in payload.get("accounts") or []:
        await publish_user(int(account_id), name, payload)
    if not vin and not payload.get("accounts"):
        no_target += 1
        log.info("events_bridge.no_target", event=name, aggregate_id=evt.get("aggregate_id"))
        return
    delivered += 1


async def start() -> None:
    """api 进程启动时调用（Bus 已在 lifespan 里 connect 过）。"""
    await get_bus().consume_events(Q_EVENTS_WS, BINDINGS, handle, concurrency=32)


def counters() -> dict[str, int]:
    return {"events_delivered": delivered, "events_no_target": no_target}
