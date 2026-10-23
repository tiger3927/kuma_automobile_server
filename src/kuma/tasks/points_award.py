"""换电返积分：消费 `swap_order.completed`（`Task-Plan.md` §7.2 换电行）。

**为什么必须有消费方**：换电完成时 `swap_service` 已经在同事务写 outbox
（`emit(aggregate_type="swap_order", event_type="completed")`），
但此前没有任何队列绑定 `swap_order.#` → 事件全部落到兜底队列 `kuma.events.unrouted`，
换完电不会返积分。

**业务规则不在这里**（`Task-Server.md` §7 依赖铁律：tasks 全部走 services）：
比例、上限、"抵扣部分不产生积分"、幂等全在 `PointsService.award_for_swap()`；
本模块只做三件事——收消息、调服务、把结果计数暴露给探针。

**兜底是 job 不是重试**：`core/bus.py` 的业务事件队列**没有 DLX**，
`nack(requeue=False)` 等于消息消失（只有 `kuma.cmd.down` 配了死信）。
所以这里除了消费方还有一条 `backfill` job：扫"已完成但没有返积分流水"的换电单补发。
幂等靠 `uq_ledger_source(source_type, source_ref, points_kind)`，
消费方与补发同时命中也只发一次。

**没有规则就不发**：`mkt.points_rule` 里没有 `source_type='swap'` 的 active 规则时，
每笔换电都会 `skipped=award_rule_missing` 并计数——这是有意的：
返积分比例是钱和营销口径，由平台配置（双人授权），代码不给默认值。
"""

from __future__ import annotations

from typing import Any

from ..core.bus import get_bus
from ..core.db import tx
from ..core.logging import get_logger
from ..services.points_service import AWARD_SOURCE_SWAP, PointsService

log = get_logger("tasks.points_award")

#: 业务事件队列（`core/bus.py:consume_events` 声明：TTL 24h、上限 2 万、超上限拒收）
Q_EVENTS_POINTS = "kuma.events.points"
#: 只订阅换电完成；`swap_order.rejected` 等不订阅（拒单没有可返的现金）
BINDINGS = ("swap_order.completed",)

_svc = PointsService()
_counters = {"handled": 0, "granted": 0, "replayed": 0, "skipped": 0, "failed": 0}


async def handle(evt: dict[str, Any]) -> None:
    """一条事件 → 一次返积分尝试。

    **业务性跳过不能抛异常**：抛了会被 `bus._wrap` 转成 `nack(requeue=False)`，
    消息直接消失（没有 DLX）。只有基础设施错误（DB 连不上等）才让它抛出去——
    那种情况由 `backfill` 补上。
    """
    payload = evt.get("payload") or {}
    swap_no = payload.get("swap_no")
    if not swap_no:
        # 事件里没有 swap_no：不回查 aggregate_id（那是 swap_order.id，
        # 与 swap_no 不是一回事），直接计数——payload 缺字段是发布方的问题，要看得见
        _counters["skipped"] += 1
        log.warning("points_award.event_without_swap_no", aggregate_id=evt.get("aggregate_id"),
                    event_type=evt.get("event_type"))
        return
    _counters["handled"] += 1
    try:
        async with tx() as session:
            out = await _svc.award_for_swap(session, swap_no=str(swap_no))
    except Exception as exc:
        _counters["failed"] += 1
        log.error("points_award.failed", swap_no=swap_no, err=str(exc))
        raise
    if out.get("granted"):
        if out.get("replayed"):
            _counters["replayed"] += 1
        else:
            _counters["granted"] += 1
        log.info("points_award.granted", swap_no=swap_no, points=out["granted"],
                 account_id=out["account_id"], rule=out["rule"], replayed=out["replayed"])
    else:
        _counters["skipped"] += 1
        log.info("points_award.skipped", reason=out.get("skipped"), swap_no=swap_no,
                 detail={k: v for k, v in out.items() if k not in ("skipped", "swap_no")})


async def start() -> None:
    """声明队列 + 绑定 + 起消费（绑定必须先于 consume，见 `bus.consume_events`）。"""
    await get_bus().consume_events(Q_EVENTS_POINTS, BINDINGS, handle, concurrency=20)


def counters() -> dict[str, int]:
    """给 `/healthz` 用：`granted=0 且 skipped>0` 说明规则没配或消费方在空转。"""
    return dict(_counters)


async def backfill(*, days: int = 7, limit: int = 200) -> dict[str, Any]:
    """补发：扫"已完成、有实付现金、但没有返积分流水"的换电单。

    这是消息丢失的兜底（nack 即丢、队列 TTL 24h），也是"消费方上线之前发生的换电"的追补口。
    """
    async with tx() as session:
        swap_nos = await _svc.repo.swaps_pending_award(session, source_type=AWARD_SOURCE_SWAP,
                                                       days=days, limit=limit)
    if not swap_nos:
        return {"pending": 0}
    granted = replayed = skipped = 0
    for no in swap_nos:
        async with tx() as session:
            out = await _svc.award_for_swap(session, swap_no=no)
        if out.get("granted"):
            granted += 1 if not out.get("replayed") else 0
            replayed += 1 if out.get("replayed") else 0
        else:
            skipped += 1
            log.info("points_award.backfill_skipped", swap_no=no, reason=out.get("skipped"))
    log.info("points_award.backfill", pending=len(swap_nos), granted=granted,
             replayed=replayed, skipped=skipped)
    return {"pending": len(swap_nos), "granted": granted, "replayed": replayed,
            "skipped": skipped}
