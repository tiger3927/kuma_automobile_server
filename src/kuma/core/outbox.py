"""outbox 写入（Task-Plan.md §10-1）：**业务写与事件必须同事务**。

2026-10-22 之前的状态是"只有投递方，没有生产方"：`tasks/outbox.py` 每 60s 扫一遍
`audit.outbox`，但全项目没有一处 `Outbox(...)` 写入——所以 `outbox_publish` 探针全绿、
任务表里也在跑，实际是空转，`kuma.events` 上没有任何消息，业务事件全在原地消失。

这一版把口径钉死：

1. **谁写**：services 层在业务写入的同一个 session 里调 `emit()`；repository 不写事件
   （repo 不知道业务语义，也不该决定"这件事要不要广播"）。
2. **写什么**：`aggregate_type` + `event_type` 拼成 topic routing key（`alarm.acked`），
   payload 只放**订阅方要用的最小字段**（vin、状态、金额），不放整行快照——
   事件是通知不是数据同步，订阅方要细节自己回查。
3. **写完就 flush**：与 `core/audit.py` 同一个教训——不 flush 的话序列化/约束错误会发生在
   FastAPI teardown 的 `commit()` 阶段，那时 200 已经发给客户端，整个事务回滚，
   业务写和事件一起静默丢失。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from ..models.audit import Outbox


async def emit(session: AsyncSession, *, aggregate_type: str,
               aggregate_id: int | str, event_type: str, payload: dict[str, Any]) -> None:
    """`aggregate_id` 允许 int 或 str：迁移 015 把列放宽成 text，
    因为 `biz.command` 的主键是 `cmd_id`（text），不是整数。"""
    session.add(Outbox(aggregate_type=aggregate_type, aggregate_id=str(aggregate_id),
                       event_type=event_type, payload=payload))
    await session.flush()
