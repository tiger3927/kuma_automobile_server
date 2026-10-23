"""积分与收款的日终任务（`Task-Plan.md` §7.1 / §7.5 / §7.6 / §5.2）。

三个 job，都是"不做就会错"而不是"做了更好"：

1. **`expire`**（§7.1 + §7.6）：批次到期 → 清零 + 记负数流水 + 更新快照。
   不做的话，`points_account.purchased_balance` 会长期高于真实可用额，
   负债表（§7.5）跟着虚高，用户端显示的余额也含已经作废的积分。
   这是**规则本身**（期限写在批次上），不是对账差异——所以可以自动执行；
   §7.6 说的"不一致只告警不自动改"针对的是对账，不是这条。
2. **`liability_snapshot`**（§7.5）：每日写 `fin.points_liability`。
   现金买来的未消耗积分是**合同负债**，不是收入；这张表是财务口径的证据。
   追溯不到购买单的批次会让该账号 `cash_equiv` 为 NULL——**估不出来就留空并计数**，
   不编一个数（§7.6 同一立场）。
3. **`pay_timeout`**（§5.2）：超时未回填/未过核销的支付单置 `timeout`。
   不做的话 `uq_pay_active`（一单只允许一张进行中的支付单）会让用户**永远付不了第二次**。

任务本身只做事务边界与计数，规则在 services（`Task-Server.md` §7 依赖铁律）。
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from ..core.db import tx
from ..core.logging import get_logger
from ..services.points_service import PointsService
from ..services.purchase_service import PurchaseService

log = get_logger("tasks.points_lifecycle")

_points = PointsService()
_purchase = PurchaseService()


async def expire(*, limit: int = 500) -> dict[str, Any]:
    """过期批次清零（§7.1）。`limit` 是单轮上限，防止一次扫全表把事务拖成长事务。"""
    async with tx() as session:
        out = await _points.expire_batches(session, limit=limit)
    if out["batches"]:
        log.info("points.expire", batches=out["batches"], points=out["points"],
                 scanned=out["scanned"])
    return out


async def liability_snapshot(*, snapshot_date: date | None = None) -> dict[str, Any]:
    """写积分负债日快照（§7.5）。默认写**今天**；重复跑是覆盖同一天的快照（可重入）。"""
    d = snapshot_date or datetime.now(UTC).date()
    async with tx() as session:
        rows = await _points.repo.snapshot_liability(session, d)
        untraceable = await _points.repo.liability_untraceable(session, d)
    if untraceable:
        # 负债估不出来的账号数——这是"账对不上"的信号，必须看得见（§7.6）
        log.warning("points.liability_untraceable", snapshot_date=str(d), accounts=untraceable)
    log.info("points.liability_snapshot", snapshot_date=str(d), accounts=rows,
             untraceable=untraceable)
    return {"snapshot_date": d.isoformat(), "accounts": rows, "untraceable": untraceable}


async def pay_timeout(*, limit: int = 200) -> dict[str, Any]:
    """超时支付单关单（§5.2 `pay_timeout`）。"""
    async with tx() as session:
        out = await _purchase.close_expired_pay_orders(session, limit=limit)
    if out["closed"]:
        log.info("pay_order.timeout", closed=out["closed"], scanned=out["scanned"])
    return out
