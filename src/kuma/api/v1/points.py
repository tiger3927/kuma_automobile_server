"""积分 API：余额与流水（用户端）、可抵额度（下单前问一次）、抵扣规则（平台配置）。

**这里没有"扣积分"的端点**——消耗只发生在业务单里（换电 §16.9、车费 §7.3、
积分商品 §7.3），必须与业务同事务；单独开一个"扣积分"接口就会出现
"积分扣了但业务没成"的孤儿账（`Task-Server.md` §7 依赖铁律的同一立场）。

规则配置走双人授权（§9.5）：改汇率等于改所有后续单的价格口径，
必须两个人签字 + 全审计，且**不追溯历史单**（历史单的 `pricing` 里存了规则快照）。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request

from ...core.deps import Db, PrincipalDep
from ...schemas.points import OffsetApproveIn, OffsetCapacityIn, OffsetRuleIn
from ...services.points_service import PointsService

router = APIRouter(prefix="/points", tags=["points"])

points_svc = PointsService()


def _ip(request: Request) -> str | None:
    return request.client.host if request.client else None


# ══ 用户侧 ═════════════════════════════════════════════════════════════════
@router.get("/balance")
async def balance(principal: PrincipalDep, session: Db) -> dict[str, Any]:
    """余额分"购买/赠送"两栏 + 30 天内到期额（§9.1）。"""
    return await points_svc.balance(session, principal)


@router.get("/ledger")
async def ledger(principal: PrincipalDep, session: Db,
                 limit: int = Query(default=50, ge=1, le=200),
                 offset: int = Query(default=0, ge=0)) -> dict[str, Any]:
    return {"items": await points_svc.ledger(session, principal, limit=limit, offset=offset)}


@router.get("/offset-capacity")
async def offset_capacity(principal: PrincipalDep, session: Db,
                          q: OffsetCapacityIn = Depends()) -> dict[str, Any]:
    """这单最多能抵多少积分/多少钱（不扣任何东西，前端显示用）。"""
    return await points_svc.offset_capacity(session, principal, biz_line=q.biz_line,
                                            amount=q.amount, currency=q.currency,
                                            region_id=q.region_id)


# ══ 平台侧：抵扣规则（版本化 + 双人授权）════════════════════════════════════
@router.post("/offset-rules", status_code=201)
async def propose_offset_rule(body: OffsetRuleIn, request: Request,
                              principal: PrincipalDep, session: Db) -> dict[str, Any]:
    return await points_svc.propose_offset_rule(
        session, principal, rule_code=body.rule_code, biz_line=body.biz_line,
        currency=body.currency, points_per_unit=body.points_per_unit,
        max_offset_pct=body.max_offset_pct, version=body.version,
        effective_from=body.effective_from, region_id=body.region_id,
        unit_scale=body.unit_scale, min_amount=body.min_amount,
        effective_to=body.effective_to, co_approver_account_id=body.co_approver_account_id,
        ip=_ip(request))


@router.post("/offset-rules/{rule_id}/approve")
async def approve_offset_rule(rule_id: int, body: OffsetApproveIn, request: Request,
                              principal: PrincipalDep, session: Db) -> dict[str, Any]:
    return await points_svc.approve_offset_rule(
        session, principal, rule_id=rule_id, approve=body.approve,
        co_approver_account_id=body.co_approver_account_id, ip=_ip(request))
