"""outbox 投递（Task-Plan.md §10-1）：业务写 + outbox 同事务，worker 只投不生成。

失败重试带退避；投递成功才置 `published`。
"""

from __future__ import annotations

from sqlalchemy import select

from ..core.bus import get_bus
from ..core.db import tx
from ..core.logging import get_logger
from ..models.audit import Outbox

log = get_logger("tasks.outbox")

MAX_RETRY = 5


async def publish_pending(batch: int = 200) -> int:
    async with tx() as session:
        rows = (await session.execute(
            select(Outbox).where(Outbox.status == "pending").order_by(Outbox.id).limit(batch)
        )).scalars().all()
        if not rows:
            return 0
        bus = get_bus()
        for row in rows:
            try:
                await bus.publish_event(f"{row.aggregate_type}.{row.event_type}", {
                    "aggregate_type": row.aggregate_type, "aggregate_id": row.aggregate_id,
                    "event_type": row.event_type, "payload": row.payload,
                    "created_at": row.created_at.isoformat(),
                })
                row.status = "published"
                from datetime import UTC, datetime

                row.published_at = datetime.now(UTC)
            except Exception as exc:
                row.retry_count += 1
                if row.retry_count >= MAX_RETRY:
                    row.status = "failed"
                log.error("outbox.publish_failed", id=row.id, retry=row.retry_count,
                          err=str(exc))
        await session.flush()
        return len(rows)
