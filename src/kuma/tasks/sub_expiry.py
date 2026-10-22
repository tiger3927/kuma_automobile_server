"""订阅到期与续费（C6，2026-10-22）。

**业务口径（本次决策定，写在这里以免以后被当成 bug）**：

1. 到期**不删数据、不停遥测入库**。订阅买的是"服务"（实时推送 + 下行控制 + 看板），
   不是"记录权"——遥测是合规留痕与结算依据，停掉它会让事后追责与对账断链。
2. 到期先进**宽限期**（`KUMA_SUB_GRACE_DAYS`，默认 7 天）：状态转 `grace`，
   期间下行控制被拒（见 `command_service`），遥测与 WS 推送照常。
3. 宽限期过完转 `expired`（终态，除非人工/自动续费复活）。
4. `auto_renew=true` 且套餐仍 `active` → 自动续一个 `duration_days`，
   并写审计（`action="subscription.renew"`）——自动扣费类动作必须可追溯。
5. 续费只按 `subscription_plan.duration_days`，**不做真实支付**：
   真实支付属 M8（`commerce`/`fin` 目前连 ORM 都没有），这里只保证"到期有状态流转"。

状态取值域：`active | grace | expired | cancelled`（`core.subscription.status` 无 CHECK）。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from ..core.audit import write_audit
from ..core.config import get_settings
from ..core.db import tx
from ..core.logging import get_logger
from ..models.core import Subscription, SubscriptionPlan

log = get_logger("tasks.sub_expiry")


async def run() -> None:
    now = datetime.now(UTC)
    grace = timedelta(days=get_settings().sub_grace_days)
    renewed = to_grace = expired = 0

    async with tx() as session:
        due = (await session.execute(
            select(Subscription).where(Subscription.status.in_(("active", "grace")),
                                       Subscription.end_at.is_not(None),
                                       Subscription.end_at < now)
            .limit(500)
        )).scalars().all()

        for sub in due:
            if sub.status == "grace":
                sub.status = "expired"
                expired += 1
                continue
            plan = await session.get(SubscriptionPlan, sub.plan_id)
            if sub.auto_renew and plan is not None and plan.status == "active":
                old_end = sub.end_at
                sub.end_at = (old_end or now) + timedelta(days=plan.duration_days)
                sub.status = "active"
                renewed += 1
                # before 必须在改之前取，否则审计里前后值一样、看不出续了多久
                await write_audit(session, actor=None, action="subscription.renew",
                                  resource_type="subscription", resource_id=sub.id,
                                  before={"end_at": old_end, "plan_id": plan.id},
                                  after={"end_at": sub.end_at, "plan_id": plan.id,
                                         "trigger": "auto"})
                continue
            sub.status = "grace"
            to_grace += 1

    log.info("sub_expiry.done", scanned=len(due), renewed=renewed,
             to_grace=to_grace, expired=expired)
    if grace.days <= 0:
        log.warn("sub_expiry.grace_disabled", hint="KUMA_SUB_GRACE_DAYS<=0，到期即 expired")
