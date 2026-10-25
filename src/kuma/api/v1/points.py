"""积分 API：余额与流水、可抵额度、返积分规则、积分包购买、负债报表。

**这里没有"扣积分"也没有"发积分"的端点**——消耗只发生在业务单里（换电 §16.9、车费 §7.3），
发放只发生在业务事实之后（换电完成 → worker 返积分；核销通过 → 买包入账）。
单独开一个"扣/发积分"接口就会出现"积分动了但业务没动"的孤儿账
（`Task-Server.md` §7 依赖铁律的同一立场）。

规则配置走双人授权（§9.5）：改汇率等于改所有后续单的价格口径，
必须两个人签字 + 全审计，且**不追溯历史单**（历史单的 `pricing` 里存了规则快照）。

积分包购买是**商品订单链**（§5.1），不是充值接口：
`下单 → 回填转账流水号 → 平台核销（双人）→ 入账`，
每一步一个端点，状态机在 `services/purchase_service.py`。
地区开关 `points_pack_purchase` 三国 seed 全是 `false`（§6.4 预付卡合规），
所以这些端点现在会明确返回 `points_pack_disabled`——**这是正确行为，不是 bug**。
"""

from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, Query, Request

from ...core.deps import Db, IdempotencyDep, PrincipalDep
from ...schemas.points import (
    AwardApproveIn,
    AwardRuleIn,
    OffsetApproveIn,
    OffsetCapacityIn,
    OffsetRuleIn,
    PackIn,
    PackRefundIn,
    VoucherIn,
    VoucherVerifyIn,
)
from ...services.points_service import PointsService
from ...services.purchase_service import PurchaseService

router = APIRouter(prefix="/points", tags=["points"])

points_svc = PointsService()
purchase_svc = PurchaseService()


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
    return await points_svc.ledger(session, principal, limit=limit, offset=offset)


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


# ══ 返积分规则（§7.2 获取比例；双人授权，与抵扣规则同一模式）══════════════════
@router.post("/award-rules", status_code=201)
async def propose_award_rule(body: AwardRuleIn, request: Request,
                             principal: PrincipalDep, session: Db) -> dict[str, Any]:
    """申报返积分规则。`ratio` 是"每 1 最小货币单位返多少积分"，与 `fixed_points` 二选一。"""
    return await points_svc.propose_award_rule(
        session, principal, rule_code=body.rule_code, source_type=body.source_type,
        region_id=body.region_id, currency=body.currency, type_code=body.type_code,
        ratio=body.ratio, fixed_points=body.fixed_points, caps=body.caps, version=body.version,
        effective_from=body.effective_from, effective_to=body.effective_to,
        validity_months=body.validity_months, co_approver_account_id=body.co_approver_account_id,
        ip=_ip(request))


@router.post("/award-rules/{rule_id}/approve")
async def approve_award_rule(rule_id: int, body: AwardApproveIn, request: Request,
                             principal: PrincipalDep, session: Db) -> dict[str, Any]:
    """审核返积分规则：通过后只对**后续**换电生效，不追溯已发的积分。"""
    return await points_svc.approve_award_rule(
        session, principal, rule_id=rule_id, approve=body.approve,
        co_approver_account_id=body.co_approver_account_id, ip=_ip(request))


# ══ 积分包（§5.1 商品骨架 / §7.2 购买 / §7.4 退款）═══════════════════════════
@router.get("/packs")
async def list_packs(principal: PrincipalDep, session: Db) -> dict[str, Any]:
    """可售积分包 + 地区开关/KYC/限额（买不了也要看得见原因）。"""
    return await purchase_svc.packs(session, principal)


@router.post("/packs", status_code=201)
async def create_pack(body: PackIn, request: Request, principal: PrincipalDep,
                      session: Db) -> dict[str, Any]:
    """配包（平台）：一次落 product + sku + points_pack——积分包是商品，不是充值档位。"""
    return await purchase_svc.create_pack(
        session, principal, pack_code=body.pack_code, name=body.name, points=body.points,
        price=body.price, currency=body.currency, validity_months=body.validity_months,
        region_id=body.region_id, ip=_ip(request))


@router.post("/packs/{pack_code}/retire")
async def retire_pack(pack_code: str, request: Request, principal: PrincipalDep,
                      session: Db) -> dict[str, Any]:
    """下架积分包（包 + sku + product 一起 retired）。"""
    return await purchase_svc.retire_pack(session, principal, pack_code=pack_code,
                                          ip=_ip(request))


@router.post("/packs/{pack_code}/purchase", status_code=201)
async def buy_pack(pack_code: str, request: Request, principal: PrincipalDep, session: Db,
                   idem: IdempotencyDep = None) -> dict[str, Any]:
    """下单 + 开支付单（**不入账**）。必须带 `Idempotency-Key`：重复提交不能开出两张支付单。"""
    return await purchase_svc.buy(session, principal, pack_code=pack_code,
                                  idempotency_key=idem or "", ip=_ip(request))


@router.get("/purchases")
async def my_purchases(principal: PrincipalDep, session: Db,
                       limit: int = Query(default=50, ge=1, le=200),
                       offset: int = Query(default=0, ge=0)) -> dict[str, Any]:
    return await purchase_svc.my_purchases(session, principal, limit=limit, offset=offset)


@router.get("/pay-orders/{pay_no}")
async def pay_order_detail(pay_no: str, principal: PrincipalDep, session: Db) -> dict[str, Any]:
    """支付单 + 名下凭证（用户侧看核销进度；被驳回要能看见并重填）。"""
    return await purchase_svc.pay_order_view(session, principal, pay_no=pay_no)


@router.get("/pay-vouchers")
async def pay_voucher_queue(principal: PrincipalDep, session: Db,
                            status: str = Query(default="pending"),
                            region_id: str | None = Query(default=None),
                            limit: int = Query(default=50, ge=1, le=200),
                            offset: int = Query(default=0, ge=0)) -> dict[str, Any]:
    """待核销队列（§9.5 工作台，`payout_verify:read`）。带 `total`，可翻页。"""
    return await purchase_svc.verify_queue(session, principal, status=status,
                                           region_id=region_id, limit=limit, offset=offset)


@router.post("/pay-orders/{pay_no}/voucher", status_code=201)
async def upload_voucher(pay_no: str, body: VoucherIn, request: Request,
                         principal: PrincipalDep, session: Db) -> dict[str, Any]:
    """回填转账流水号。同一流水号第二次提交会被库拒（`uq_transfer_no`，§7.2 事故源）。"""
    return await purchase_svc.upload_voucher(
        session, principal, pay_no=pay_no, transfer_no=body.transfer_no, amount=body.amount,
        currency=body.currency, proof_url=body.proof_url, ip=_ip(request))


@router.get("/co-approvers")
async def co_approvers(principal: PrincipalDep, session: Db,
                       limit: int = Query(default=50, ge=1, le=200)) -> dict[str, Any]:
    """联审候选人（核销与退款都要双人，§9.5）。

    没有这个读端点，前端只能让人手填 `co_approver_account_id`——填错就是
    `co_approver_not_platform_staff`，核销台变成猜账号游戏。
    """
    return await purchase_svc.co_approvers(session, principal, limit=limit)


@router.post("/pay-vouchers/{voucher_id}/verify")
async def verify_voucher(voucher_id: int, body: VoucherVerifyIn, request: Request,
                         principal: PrincipalDep, session: Db) -> dict[str, Any]:
    """核销凭证（§9.5 收款核销工作台，双人）。通过 = 同事务完成收款、订单完成、积分入账。"""
    return await purchase_svc.verify_voucher(
        session, principal, voucher_id=voucher_id, approve=body.approve,
        co_approver_account_id=body.co_approver_account_id, ip=_ip(request))


@router.post("/purchases/{purchase_no}/refund")
async def refund_purchase(purchase_no: str, body: PackRefundIn, request: Request,
                          principal: PrincipalDep, session: Db) -> dict[str, Any]:
    """积分包退款：现金按原路退 + 整包扣回积分（§7.4 禁止交叉）。只允许整包未消耗。"""
    return await purchase_svc.refund_purchase(
        session, principal, purchase_no=purchase_no, reason=body.reason,
        co_approver_account_id=body.co_approver_account_id, ip=_ip(request))


# ══ 积分负债（§7.5：现金买来的未消耗积分是合同负债，不是收入）═════════════════
@router.get("/liability")
async def liability(principal: PrincipalDep, session: Db,
                    snapshot_date: date = Query(description="快照日（worker 日终写入）")
                    ) -> dict[str, Any]:
    """某日负债快照 + 估不出金额的账号数（`untraceable>0` 就是"账对不上"的信号）。"""
    return await purchase_svc.liability(session, principal, snapshot_date=snapshot_date)
