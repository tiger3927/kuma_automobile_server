"""商城 API（配件电商）：购物车 / 下单 / 回填转账 / 平台核销 / 到店提货 + 商家货架与库存。

**目录是公开的**（`/public/products`），品牌方官网与未登录用户都要能看货：
把"看货"锁在登录之后，等于让官网帮你筛掉所有还没注册的人。

**钱与货的口径**（与 `services/shop_service.py` 的模块 docstring 一致）：
下单只锁库存、开支付单；收款确认发生在平台**双人核销**通过之后；到店提货才完成订单。
所以这里的写端点刻意分开——`/orders`（用户）、`/orders/{no}/verify`（平台）、
`/pickup`（经销商）——三种角色三种动作，不做一个"什么都干"的端点。
"""

from __future__ import annotations

import secrets
from typing import Any

from fastapi import APIRouter, Query, Request

from ...core.deps import Db, IdempotencyDep, PrincipalDep
from ...schemas.points import VoucherIn, VoucherVerifyIn
from ...schemas.shop import (
    CancelIn,
    CartItemIn,
    CheckoutIn,
    PickupConfirmIn,
    ProductIn,
    SkuIn,
    StockQtyIn,
    StockUnitsIn,
)
from ...services.shop_service import ShopService

router = APIRouter(prefix="/shop", tags=["shop"])

svc = ShopService()


def _ip(request: Request) -> str | None:
    return request.client.host if request.client else None


# ══ 购物车 ═════════════════════════════════════════════════════════════════
@router.get("/cart")
async def cart(principal: PrincipalDep, session: Db) -> dict[str, Any]:
    return await svc.cart(session, principal)


@router.post("/cart/items")
async def set_cart_item(body: CartItemIn, principal: PrincipalDep, session: Db
                        ) -> dict[str, Any]:
    """加购 / 改数量（`qty=0` 等于移除）。加购就校验可售量，别让用户填完地址才知道没货。"""
    return await svc.set_cart_item(session, principal, sku_code=body.sku_code, qty=body.qty)


@router.delete("/cart")
async def clear_cart(principal: PrincipalDep, session: Db) -> dict[str, Any]:
    return await svc.clear_cart(session, principal)


# ══ 订单（用户侧）══════════════════════════════════════════════════════════
@router.post("/orders", status_code=201)
async def checkout(body: CheckoutIn, request: Request, principal: PrincipalDep, session: Db,
                   idem: IdempotencyDep = None) -> dict[str, Any]:
    """下单 + 开支付单（**不收钱、不交付**）。

    没带 `Idempotency-Key` 时**当场生成一个**：线上真实事故是前端漏带 header 后
    `idempotency_key=''` 让第二笔订单撞 `UNIQUE`（或更糟：被当成重放返回第一单）。
    生成随机 key 后每笔订单独立，代价只是失去重试去重——比错单好。
    """
    key = idem or f"autogen-{secrets.token_hex(8)}"
    return await svc.checkout(
        session, principal, dealer_org_id=body.dealer_org_id,
        items=[{"sku_code": i.sku_code, "qty": i.qty} for i in body.items]
        if body.items is not None else None,
        vin=body.vin, note=body.note, idempotency_key=key, ip=_ip(request))


@router.get("/orders")
async def my_orders(principal: PrincipalDep, session: Db,
                    status: str | None = Query(default=None, max_length=20),
                    limit: int = Query(default=20, ge=1, le=100),
                    offset: int = Query(default=0, ge=0)) -> dict[str, Any]:
    return await svc.my_orders(session, principal, status=status, limit=limit, offset=offset)


@router.get("/orders/{order_no}")
async def order_detail(order_no: str, principal: PrincipalDep, session: Db) -> dict[str, Any]:
    """订单详情：行、SN、履约单、凭证、支付单（一张页面看全，不再来回查四个端点）。"""
    return await svc.order_detail(session, principal, order_no=order_no)


@router.post("/orders/{order_no}/voucher", status_code=201)
async def upload_voucher(order_no: str, body: VoucherIn, request: Request,
                         principal: PrincipalDep, session: Db) -> dict[str, Any]:
    """回填转账流水号。同一流水号跨订单复用会被 `uq_transfer_no` 拦（409）。"""
    return await svc.upload_voucher(
        session, principal, order_no=order_no, transfer_no=body.transfer_no,
        amount=body.amount, currency=body.currency, proof_url=body.proof_url,
        ip=_ip(request))


@router.post("/orders/{order_no}/cancel")
async def cancel_order(order_no: str, request: Request, principal: PrincipalDep, session: Db,
                       body: CancelIn | None = None) -> dict[str, Any]:
    """取消未付款订单：库存/SN 立刻放回（不取消就一直占着货）。"""
    return await svc.cancel(session, principal, order_no=order_no,
                            reason=(body.reason if body else None), ip=_ip(request))


# ══ 平台：核销（双人）与经销商：提货 ════════════════════════════════════════
@router.post("/orders/{order_no}/verify")
async def verify_order(order_no: str, body: VoucherVerifyIn, request: Request,
                       principal: PrincipalDep, session: Db) -> dict[str, Any]:
    """核销转账凭证（`payout_verify:write` + 双人复核）。通过后生成提货码、SN 转 sold。"""
    return await svc.verify_order(
        session, principal, order_no=order_no, approve=body.approve,
        co_approver_account_id=body.co_approver_account_id, ip=_ip(request))


@router.post("/pickup")
async def confirm_pickup(body: PickupConfirmIn, request: Request, principal: PrincipalDep,
                         session: Db) -> dict[str, Any]:
    """到店提货核销（经销商扫用户出示的提货码）→ 订单 completed。"""
    return await svc.confirm_pickup(session, principal, pickup_code=body.pickup_code,
                                    ip=_ip(request))


# ══ 商家侧：货架、SKU、库存 ═════════════════════════════════════════════════
@router.post("/products", status_code=201)
async def create_product(body: ProductIn, request: Request, principal: PrincipalDep, session: Db
                         ) -> dict[str, Any]:
    return await svc.create_product(
        session, principal, name=body.name, product_type=body.product_type,
        category=body.category, currency=body.currency, region_id=body.region_id,
        msrp=body.msrp, manufacturer_org_id=body.manufacturer_org_id,
        image_url=body.image_url, attrs=body.attrs, ip=_ip(request))


@router.post("/skus", status_code=201)
async def create_sku(body: SkuIn, request: Request, principal: PrincipalDep, session: Db
                     ) -> dict[str, Any]:
    """建可售单位。`inventory_kind` 建完不可改（数量库存与 SN 台账不能互相转换）。"""
    return await svc.create_sku(
        session, principal, product_id=body.product_id, code=body.code, price=body.price,
        currency=body.currency, inventory_kind=body.inventory_kind, attrs=body.attrs,
        ip=_ip(request))


@router.post("/stock/qty")
async def add_stock_qty(body: StockQtyIn, request: Request, principal: PrincipalDep, session: Db
                        ) -> dict[str, Any]:
    """数量库存增减（入库 +N / 报损 -N），返回变动后的余量。"""
    return await svc.add_stock_qty(
        session, principal, sku_id=body.sku_id, dealer_org_id=body.dealer_org_id,
        qty_delta=body.qty_delta, ip=_ip(request))


@router.post("/stock/units", status_code=201)
async def add_stock_units(body: StockUnitsIn, request: Request, principal: PrincipalDep,
                          session: Db) -> dict[str, Any]:
    """SN 批量入库（重复 SN 静默跳过，返回 inserted / skipped）。"""
    return await svc.add_stock_units(
        session, principal, sku_id=body.sku_id, dealer_org_id=body.dealer_org_id,
        sns=body.sns, note=body.note, ip=_ip(request))


@router.get("/stock")
async def stock(principal: PrincipalDep, session: Db,
                dealer_org_id: int | None = Query(default=None, ge=1),
                limit: int = Query(default=50, ge=1, le=200),
                offset: int = Query(default=0, ge=0)) -> dict[str, Any]:
    """数量库存列表（`qty` 类 SKU）。"""
    return await svc.stock(session, principal, dealer_org_id=dealer_org_id,
                           limit=limit, offset=offset)


@router.get("/stock/units")
async def units(principal: PrincipalDep, session: Db,
                sku_id: int | None = Query(default=None, ge=1),
                status: str | None = Query(default=None, max_length=20),
                limit: int = Query(default=50, ge=1, le=200),
                offset: int = Query(default=0, ge=0)) -> dict[str, Any]:
    """SN 台账（`serial` 类 SKU 的逐件状态：在库 / 已预留 / 已售）。"""
    return await svc.units(session, principal, sku_id=sku_id, status=status,
                           limit=limit, offset=offset)


@router.get("/merchant/orders")
async def merchant_orders(principal: PrincipalDep, session: Db,
                          status: str | None = Query(default=None, max_length=20),
                          limit: int = Query(default=20, ge=1, le=100),
                          offset: int = Query(default=0, ge=0)) -> dict[str, Any]:
    """商家订单列表（平台/监管看全部；经销只看自己店）。"""
    return await svc.merchant_orders(session, principal, status=status, limit=limit,
                                     offset=offset)


@router.get("/merchant/summary")
async def merchant_summary(principal: PrincipalDep, session: Db,
                           dealer_org_id: int | None = Query(default=None, ge=1)
                           ) -> dict[str, Any]:
    """商家看板：待收款/待提货/已收金额。金额只算 paid+completed（created 不算营收）。"""
    return await svc.summary(session, principal, dealer_org_id=dealer_org_id)
