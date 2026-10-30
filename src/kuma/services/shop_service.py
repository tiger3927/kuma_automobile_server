"""商城（配件电商）服务：目录 / 购物车 / 下单锁库存 / 回填转账 / 双人核销 / 到店提货。

## 与积分包链路的关系（不是两套东西）

积分包已经把"下单 → 二维码收款 → 回填流水号 → 平台双人核销 → 交付"这条链跑通，
商城**复用同一条**：`PurchaseRepo` 的订单/支付单/凭证方法与 `fin.payment_voucher` 的唯一约束
（`uq_transfer_no`：同一笔转账不能给两个订单核销）。所以后台只要一个核销工作台，
不是"积分包一个、商城一个"。

## 库存双轨的扣减时机（这一条决定会不会超卖）

- **数量库存（`qty`）**：下单即扣（`lock_qty` 条件 UPDATE），取消/超时归还。
  为什么不等到付款？等付款再扣，就会出现"两个人同时下单、付款时只剩一件"——
  用户已经转了钱却拿不到货，比"下单占货、半小时不付自动释放"难处理得多。
- **SN 库存（`serial`）**：下单即 `reserved`（`FOR UPDATE SKIP LOCKED` 挑件），
  核销通过变 `sold`，取消/超时回 `in_stock`。

两者都在**下单那一刻**锁住，这是本域唯一的口径；释放只有两个来源：用户取消、
worker 扫超时（`close_expired`），两者都走同一个 `_release`，不允许第三条路径。

## 钱归谁（v1 的真实缺口，别装作没有）

配件是**经销商**的货（`order.dealer_org_id`），但收款渠道是平台静态码
（`fin.payment_channel.account_entity`），即 v1 是"平台代收 → 应结给经销商"。
`fin.settlement_*` 表已建但**结算作业没实现**（M14），所以：

- 订单的 `collect_mode` 如实记 `platform_online`（不是假装钱直接进经销商口袋）；
- `dealer_org_id` 一定写上，将来分账只靠这一个锚点；
- 对账口径：平台流水里这些是**代收负债**，不是平台收入。做报表时必须按
  `collect_mode='platform_online' AND order_type IN ('part','service')` 剔出来。
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.audit import write_audit
from ..core.exceptions import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from ..core.outbox import emit
from ..core.scope import Principal
from ..repositories.purchase_repo import PurchaseRepo
from ..repositories.shop_repo import ShopRepo
from .dual_control import require_co_approver
from .purchase_service import OPEN_PAY_STATUSES, PAY_TTL, PROVIDER_QR_TRANSFER

#: 商城订单类型（`vehicle`/`used_vehicle` 走买车链 M9，`points_*` 走积分包）
SHOP_ORDER_TYPES = ("part", "service")
#: 单笔订单最多行数（购物车上限，防止一次结 500 个 SKU 把库存锁半天）
MAX_LINES = 20


class ShopService:
    def __init__(self) -> None:
        self.repo = ShopRepo()
        self.prepo = PurchaseRepo()

    # ══ 目录（公开可看，无需登录）════════════════════════════════════════════
    async def catalog(self, session: AsyncSession, *, region_id: str | None = None,
                      product_type: str | None = None, category: str | None = None,
                      q: str | None = None, limit: int = 20, offset: int = 0
                      ) -> dict[str, Any]:
        if product_type is not None and product_type not in (*SHOP_ORDER_TYPES, "coupon"):
            raise ValidationFailed(f"product_type 必须在 {list(SHOP_ORDER_TYPES)} 或 coupon",
                                   code="bad_product_type")
        items, total = await self.repo.products_page(
            session, region_id=region_id, product_type=product_type, category=category,
            q=q, limit=limit, offset=offset)
        return {"items": items, "total": total, "limit": limit, "offset": offset}

    async def product(self, session: AsyncSession, *, product_id: int) -> dict[str, Any]:
        """商品详情 + 每个 SKU 的可售量与"哪家店有货"（没有卖方信息，用户没法下单）。"""
        p = await self.repo.product_detail(session, product_id=product_id)
        if p is None or p["status"] != "active":
            raise NotFound("商品不存在或已下架", code="product_not_found")
        skus = await self.repo.skus_of_product(session, product_id)
        out = []
        for s in skus:
            if s["status"] != "active":
                continue
            s["dealers"] = await self.repo.dealers_with_stock(session, sku_id=int(s["id"]))
            out.append(s)
        p["skus"] = out
        return p

    # ══ 购物车 ═══════════════════════════════════════════════════════════════
    async def cart(self, session: AsyncSession, principal: Principal) -> dict[str, Any]:
        """购物车。**不合并跨币种**：混币种的车直接标 `currency_conflict`，
        让前端提示用户分开结算，而不是在这里偷偷挑一个币种算总价。"""
        principal.require("order", "write")
        rows = await self.repo.cart_rows(session, principal.account_id)
        currencies = {r["currency"] for r in rows}
        total = sum(int(r["price"]) * int(r["qty"]) for r in rows)
        return {"items": rows, "count": len(rows), "total_minor": total,
                "currency": currencies.pop() if len(currencies) == 1 else None,
                "currency_conflict": len(currencies) > 1}

    async def set_cart_item(self, session: AsyncSession, principal: Principal, *, sku_code: str,
                            qty: int) -> dict[str, Any]:
        principal.require("order", "write")
        sku = await self.repo.sku_by_code(session, sku_code)
        if sku is None or sku["status"] != "active":
            raise NotFound("商品不存在或已下架", code="sku_not_found")
        if qty > 0:
            # 加购时就校验可售量：等到结算才说"没货"，用户已经把地址填完了
            avail = await self._available(session, sku_id=int(sku["id"]))
            if qty > avail:
                raise Conflict(f"库存不足（现有 {avail} 件）", code="stock_not_enough")
        await self.repo.cart_upsert(session, account_id=principal.account_id,
                                    sku_id=int(sku["id"]), qty=qty)
        return await self.cart(session, principal)

    async def clear_cart(self, session: AsyncSession, principal: Principal) -> dict[str, Any]:
        principal.require("order", "write")
        n = await self.repo.cart_clear(session, principal.account_id)
        return {"removed": n}

    # ══ 下单 ═════════════════════════════════════════════════════════════════
    async def checkout(self, session: AsyncSession, principal: Principal, *,
                       dealer_org_id: int, items: list[dict[str, Any]] | None,
                       vin: str | None, note: str | None,
                       idempotency_key: str, ip: str | None = None) -> dict[str, Any]:
        """下单 + 开支付单（同一事务）。**这一步不收钱、也不交付**：
        付款只发生在平台核销通过之后（与积分包同一口径，§5.2 收款线）。
        """
        principal.require("order", "write")
        if principal.status != "active":
            raise InvalidState("账号状态不允许下单", code="account_not_active")

        replay = await self.prepo.order_by_idempotency(session, idempotency_key)
        if replay is not None:
            pay = await self.prepo.pay_order_for_order(session, int(replay["id"]))
            return {"order": replay, "pay_order": pay, "replayed": True,
                    "items": await self.repo.order_items(session, int(replay["id"]))}

        dealer = await self.repo.org_by_id(session, dealer_org_id)
        if dealer is None or dealer["org_type"] != "dealer" or dealer["status"] != "active":
            raise ValidationFailed("卖方不是有效的经销商", code="dealer_invalid")

        lines = await self._resolve_lines(session, principal, items=items,
                                          dealer_region=str(dealer["region_id"] or ""))
        if not lines:
            raise ValidationFailed("没有可结算的商品", code="cart_empty")
        if len(lines) > MAX_LINES:
            raise ValidationFailed(f"单笔订单最多 {MAX_LINES} 行", code="too_many_lines")

        currency = lines[0]["currency"]
        if any(line["currency"] != currency for line in lines):
            raise ValidationFailed("一单只能结算同一币种", code="currency_conflict")
        amount = sum(int(line["price"]) * int(line["qty"]) for line in lines)
        order_type = ("service" if all(line["product_type"] == "service" for line in lines)
                      else "part")

        channel = await self.prepo.channel_for(
            session, provider_type=PROVIDER_QR_TRANSFER,
            region_id=str(dealer["region_id"]), currency=currency)
        if channel is None:
            raise InvalidState(
                f"{dealer['region_id']}/{currency} 没有可用的转账收款渠道配置",
                code="pay_channel_missing")

        order = await self.prepo.insert_order(
            session, order_no=self._no("SO"), account_id=principal.account_id,
            order_type=order_type, region_id=str(dealer["region_id"] or "TH"),
            currency=currency, amount_cash=amount, amount_points=0,
            collect_mode=str(channel["collect_mode"]), status="created",
            idempotency_key=idempotency_key, dealer_org_id=dealer_org_id)
        expires = datetime.now(UTC) + PAY_TTL

        # 库存锁定与订单行同事务：任一行不足就整单失败（不允许"扣了一半"的订单）
        for line in lines:
            item_id = await self.prepo.insert_order_item(
                session, order_id=int(order["id"]), sku_id=int(line["sku_id"]),
                product_type=str(line["product_type"]), qty=int(line["qty"]),
                unit_price=int(line["price"]), points_cost=0, vin=vin)
            if line["inventory_kind"] == "serial":
                units = await self.repo.reserve_units(
                    session, sku_id=int(line["sku_id"]), dealer_org_id=dealer_org_id,
                    qty=int(line["qty"]), order_id=int(order["id"]), order_item_id=item_id,
                    reserved_until=expires)
                if len(units) < int(line["qty"]):
                    raise Conflict(f"{line['sku_code']} 库存不足（在库 {len(units)} 件）",
                                   code="stock_not_enough")
                line["units"] = [u["sn"] for u in units]
            else:
                left = await self.repo.lock_qty(
                    session, sku_id=int(line["sku_id"]), dealer_org_id=dealer_org_id,
                    qty=int(line["qty"]))
                if left is None:
                    raise Conflict(f"{line['sku_code']} 库存不足", code="stock_not_enough")

        pay = await self.prepo.insert_pay_order(
            session, pay_no=self._no("PAY"), order_id=int(order["id"]),
            provider=PROVIDER_QR_TRANSFER, channel_id=int(channel["id"]),
            amount=amount, currency=currency, status="qr_issued", expires_at=expires)

        # 结算完就清购物车（"立即购买"不动购物车）
        if items is None:
            await self.repo.cart_clear(session, principal.account_id)

        await write_audit(session, actor=principal, action="shop.order.create",
                          resource_type="order", resource_id=order["id"],
                          after={"order_no": order["order_no"], "dealer_org_id": dealer_org_id,
                                 "amount": amount, "currency": currency,
                                 "lines": [{"sku": line["sku_code"], "qty": line["qty"]}
                                           for line in lines],
                                 "pay_no": pay["pay_no"], "channel": channel["channel_code"]},
                          ip=ip)
        await emit(session, aggregate_type="commerce_order", aggregate_id=order["id"],
                   event_type="created",
                   payload={"order_no": order["order_no"],
                            "account_id": principal.account_id,
                            "dealer_org_id": dealer_org_id, "amount": amount,
                            "currency": currency,
                            "lines": [{"sku_code": line["sku_code"], "qty": line["qty"]}
                                      for line in lines]})
        return {
            "order": order, "pay_order": pay, "replayed": False,
            "items": await self.repo.order_items(session, int(order["id"])),
            "pay_instructions": {
                "channel_code": channel["channel_code"],
                "provider_type": channel["provider_type"],
                "account_entity": channel["account_entity"],
                "channel_id": int(channel["id"]),
                "amount": amount, "currency": currency,
                "expires_at": expires,
                "note": "转账后回填流水号，平台核销通过后到店提货"},
        }

    # ══ 我的订单 ═════════════════════════════════════════════════════════════
    async def my_orders(self, session: AsyncSession, principal: Principal, *,
                        status: str | None = None, limit: int = 20, offset: int = 0
                        ) -> dict[str, Any]:
        principal.require("order", "read")
        items, total = await self.repo.orders_page(session, account_id=principal.account_id,
                                                   status=status, limit=limit, offset=offset)
        return {"items": items, "total": total, "limit": limit, "offset": offset}

    async def order_detail(self, session: AsyncSession, principal: Principal, *,
                           order_no: str) -> dict[str, Any]:
        principal.require("order", "read")
        order = await self.repo.order_by_no(session, order_no)
        if order is None:
            raise NotFound("订单不存在", code="order_not_found")
        self._assert_readable(principal, order)
        order["items"] = await self.repo.order_items(session, int(order["id"]))
        order["units"] = await self.repo.order_units(session, int(order["id"]))
        order["fulfillments"] = await self.repo.fulfillments_of_order(session, int(order["id"]))
        order["vouchers"] = await self.repo.order_vouchers(session, int(order["id"]))
        pay = await self.prepo.pay_order_for_order(session, int(order["id"]))
        order["pay_order"] = pay
        order["pay_instructions"] = await self._pay_instructions(session, order, pay)
        return order

    async def upload_voucher(self, session: AsyncSession, principal: Principal, *,
                             order_no: str, transfer_no: str, amount: int | None = None,
                             currency: str | None = None, proof_url: str | None = None,
                             ip: str | None = None) -> dict[str, Any]:
        """回填转账流水号（`qr_issued → voucher_pending`）。与积分包共用 `fin.payment_voucher`。

        **入参是订单号不是支付单号**：用户手里只有订单（他在"我的订单"里点的），
        让他先查 pay_no 再回填纯属让前端多一次往返。
        """
        principal.require("payment", "write")
        order = await self.repo.order_by_no(session, order_no)
        if order is None:
            raise NotFound("订单不存在", code="order_not_found")
        if int(order["account_id"]) != principal.account_id:
            raise Forbidden("只能给自己的订单回填凭证", code="not_your_order")
        if order["status"] != "created":
            raise InvalidState(f"订单当前状态 {order['status']} 不能回填凭证",
                               code="order_state")
        pay = await self.prepo.pay_order_for_order(session, int(order["id"]))
        if pay is None:
            raise NotFound("该订单没有支付单", code="pay_order_not_found")
        if pay["status"] not in OPEN_PAY_STATUSES:
            raise InvalidState(f"支付单当前状态 {pay['status']} 不能回填凭证", code="pay_state")
        if amount is not None and int(amount) != int(pay["amount"]):
            raise ValidationFailed("回填金额与应付金额不一致", code="voucher_amount_mismatch")
        if currency is not None and currency != pay["currency"]:
            raise ValidationFailed("回填币种与应付币种不一致", code="voucher_currency_mismatch")

        try:
            async with session.begin_nested():
                voucher = await self.prepo.insert_voucher(
                    session, pay_order_id=int(pay["id"]), transfer_no=transfer_no,
                    amount=amount, currency=currency, proof_url=proof_url,
                    uploaded_by=principal.account_id)
        except IntegrityError as e:
            # `uq_transfer_no`：同一笔转账给多个订单核销 = §7.2 点名的 v1 最大事故源。
            # 用 SAVEPOINT 而不是 `session.rollback()`：外层（API 的 get_session）还开着事务，
            # 整条连接回滚会让它变 closed（换电域踩过这个坑，见 swap_service 的注释）。
            raise Conflict("该转账流水号已被用于其它订单（不能重复核销）",
                           code="transfer_no_reused") from e
        await self.prepo.set_pay_status(session, pay_id=int(pay["id"]), status="voucher_pending")
        await write_audit(session, actor=principal, action="shop.voucher.upload",
                          resource_type="payment_voucher", resource_id=voucher["id"],
                          after={"order_no": order_no, "pay_no": pay["pay_no"],
                                 "transfer_no": transfer_no, "amount": amount}, ip=ip)
        return voucher

    async def cancel(self, session: AsyncSession, principal: Principal, *, order_no: str,
                     reason: str | None = None, ip: str | None = None) -> dict[str, Any]:
        """用户取消未付款的订单。**只放行 `created`**：已付款要退出走售后（退款是另一条链）。"""
        principal.require("order", "write")
        order = await self.repo.order_by_no(session, order_no)
        if order is None:
            raise NotFound("订单不存在", code="order_not_found")
        if int(order["account_id"]) != principal.account_id and not principal.is_platform:
            raise Forbidden("不是你的订单", code="not_your_order")
        locked = await self.repo.lock_order(session, int(order["id"]))
        if locked is None:
            raise NotFound("订单不存在", code="order_not_found")
        if locked["status"] != "created":
            raise InvalidState(f"订单已是 {locked['status']}，不能取消", code="order_state")
        await self._release(session, locked)
        pay = await self.prepo.pay_order_for_order(session, int(locked["id"]))
        if pay is not None and pay["status"] in OPEN_PAY_STATUSES:
            await self.prepo.set_pay_status(session, pay_id=int(pay["id"]), status="closed")
        cancelled = await self.repo.mark_cancelled(
            session, int(locked["id"]), reason or "用户取消")
        await write_audit(session, actor=principal, action="shop.order.cancel",
                          resource_type="order", resource_id=locked["id"],
                          after={"order_no": order_no, "reason": reason or "用户取消"}, ip=ip)
        await emit(session, aggregate_type="commerce_order", aggregate_id=locked["id"],
                   event_type="cancelled",
                   payload={"order_no": order_no, "reason": reason or "用户取消"})
        return cancelled or locked

    # ══ 核销（平台，双人）与提货（经销商）═════════════════════════════════════
    async def verify_order(self, session: AsyncSession, principal: Principal, *,
                           order_no: str, approve: bool,
                           co_approver_account_id: int | None = None,
                           ip: str | None = None) -> dict[str, Any]:
        """核销转账凭证 = 确认钱到账。通过后：订单 `paid`、SN 转 `sold`、生成提货码。

        `payout_verify:write` 在 `DUAL_CONTROL` 里 → 必须两个人签字（§9.5），
        与积分包核销是**同一道闸门**（同一个动作、同一张凭证表）。
        """
        principal.require("payout_verify", "write")
        await require_co_approver(session, principal, co_approver_account_id)

        order = await self.repo.order_by_no(session, order_no)
        if order is None:
            raise NotFound("订单不存在", code="order_not_found")
        if order["order_type"] not in SHOP_ORDER_TYPES:
            raise InvalidState("这个端点只处理配件/服务订单", code="not_shop_order")
        locked = await self.repo.lock_order(session, int(order["id"]))
        if locked is None:
            raise NotFound("订单不存在", code="order_not_found")
        if locked["status"] != "created":
            raise InvalidState(f"订单已是 {locked['status']}", code="order_state")

        vouchers = await self.repo.order_vouchers(session, int(locked["id"]))
        pending = next((v for v in vouchers if v["status"] == "pending"), None)
        if pending is None:
            raise InvalidState("该订单没有待核销的转账凭证", code="no_pending_voucher")

        if not approve:
            await self.prepo.set_voucher_status(session, voucher_id=int(pending["id"]),
                                                status="rejected",
                                                verifier=principal.account_id)
            await self.prepo.set_pay_status(session, pay_id=int(pending["pay_order_id"]),
                                            status="voucher_pending")
            await write_audit(session, actor=principal, action="shop.voucher.reject",
                              resource_type="payment_voucher", resource_id=pending["id"],
                              after={"order_no": order_no, "status": "rejected"}, ip=ip)
            return {"order_no": order_no, "voucher_status": "rejected"}

        await self.prepo.set_voucher_status(session, voucher_id=int(pending["id"]),
                                            status="verified", verifier=principal.account_id)
        pay = await self.prepo.set_pay_status(
            session, pay_id=int(pending["pay_order_id"]), status="paid",
            out_trade_no=str(pending["transfer_no"]), mark_paid=True)
        paid = await self.repo.mark_paid(session, int(locked["id"]))
        sns = await self.repo.sell_units_of_order(session, int(locked["id"]))

        pickup_code = secrets.token_hex(4).upper()
        await self.prepo.insert_fulfillment(
            session, order_id=int(locked["id"]), type_="pickup", status="pending",
            confirmed_by=None, pickup_code=pickup_code,
            dealer_org_id=locked["dealer_org_id"], delivered_now=False)

        await write_audit(session, actor=principal, action="shop.voucher.verify",
                          resource_type="payment_voucher", resource_id=pending["id"],
                          after={"order_no": order_no, "pay_no": pending["pay_no"],
                                 "transfer_no": pending["transfer_no"],
                                 "serial_sold": sns, "pickup_code": pickup_code,
                                 "co_approver_account_id": co_approver_account_id}, ip=ip)
        await emit(session, aggregate_type="commerce_order", aggregate_id=locked["id"],
                   event_type="paid",
                   payload={"order_no": order_no, "amount": int(locked["amount_cash"]),
                            "currency": locked["currency"],
                            "dealer_org_id": locked["dealer_org_id"],
                            "pickup_code": pickup_code})
        return {"order": paid, "pay_order": pay, "voucher_status": "verified",
                "serial_sold": sns, "pickup_code": pickup_code}

    async def confirm_pickup(self, session: AsyncSession, principal: Principal, *,
                             pickup_code: str, ip: str | None = None) -> dict[str, Any]:
        """到店提货核销（经销商扫用户出示的提货码）。资金在核销那一步已经确认。"""
        principal.require("order", "write")
        f = await self.repo.fulfillment_by_pickup(session, pickup_code)
        if f is None:
            raise NotFound("提货码无效", code="pickup_code_not_found")
        if not (principal.is_platform
                or int(f["dealer_org_id"] or 0) in principal.org_ids_for_line("dealer")
                or int(f["dealer_org_id"] or 0) in principal.org_ids()):
            raise Forbidden("只能核销自己店的提货码", code="not_your_dealer")
        if f["status"] == "delivered":
            raise InvalidState("该提货码已使用", code="pickup_already_done")
        if f["order_status"] != "paid":
            raise InvalidState(f"订单还没确认收款（{f['order_status']}）", code="order_state")

        await self.repo.confirm_fulfillment(session, fulfillment_id=int(f["id"]),
                                            account_id=principal.account_id)
        done = await self.repo.mark_completed(session, int(f["order_id"]))
        await write_audit(session, actor=principal, action="shop.pickup.confirm",
                          resource_type="fulfillment", resource_id=f["id"],
                          after={"pickup_code": pickup_code, "order_no": f["order_no"]}, ip=ip)
        await emit(session, aggregate_type="commerce_order", aggregate_id=f["order_id"],
                   event_type="completed",
                   payload={"order_no": f["order_no"], "pickup_code": pickup_code})
        return done or {}

    # ══ 商家侧：商品 / SKU / 库存 / 订单 ══════════════════════════════════════
    async def create_product(self, session: AsyncSession, principal: Principal, *,
                             name: str, product_type: str, category: str | None,
                             currency: str, region_id: str | None, msrp: int | None,
                             manufacturer_org_id: int | None, ip: str | None = None
                             ) -> dict[str, Any]:
        principal.require("product", "write")
        org_id = self._seller_org(principal, manufacturer_org_id)
        p = await self.repo.insert_product(
            session, name=name, product_type=product_type, category=category,
            currency=currency, region_id=region_id, msrp=msrp,
            manufacturer_org_id=org_id, status="active")
        await write_audit(session, actor=principal, action="shop.product.create",
                          resource_type="product", resource_id=p["id"],
                          after={"name": name, "type": product_type, "org_id": org_id}, ip=ip)
        return p

    async def create_sku(self, session: AsyncSession, principal: Principal, *, product_id: int,
                         code: str, price: int, currency: str | None, inventory_kind: str,
                         attrs: dict[str, Any], ip: str | None = None) -> dict[str, Any]:
        """建可售单位。`inventory_kind` 建完就定了**不能改**：
        把已有 SN 台账的 SKU 改成数量库存，等于把逐件资产变成一堆数字，不可逆。
        """
        principal.require("product", "write")
        p = await self.repo.product_detail(session, product_id=product_id)
        if p is None:
            raise NotFound("商品不存在", code="product_not_found")
        sku = await self.repo.insert_sku(
            session, product_id=product_id, code=code, attrs=attrs, price=price,
            currency=currency or str(p["currency"]), inventory_kind=inventory_kind,
            status="active")
        await write_audit(session, actor=principal, action="shop.sku.create",
                          resource_type="sku", resource_id=sku["id"],
                          after={"code": code, "price": price, "kind": inventory_kind}, ip=ip)
        return sku

    async def add_stock_qty(self, session: AsyncSession, principal: Principal, *,
                            sku_id: int, dealer_org_id: int | None, qty_delta: int,
                            ip: str | None = None) -> dict[str, Any]:
        principal.require("product", "write")
        sku = await self.repo.sku_by_id(session, sku_id)
        if sku is None:
            raise NotFound("SKU 不存在", code="sku_not_found")
        if sku["inventory_kind"] != "qty":
            raise InvalidState("这个 SKU 是 SN 台账，请用 SN 入库", code="not_qty_sku")
        org = self._seller_org(principal, dealer_org_id)
        qty = await self.repo.add_stock_qty(session, sku_id=sku_id, dealer_org_id=org,
                                            delta=int(qty_delta))
        await write_audit(session, actor=principal, action="shop.stock.adjust",
                          resource_type="stock", resource_id=sku_id,
                          after={"dealer_org_id": org, "delta": qty_delta, "qty": qty}, ip=ip)
        return {"sku_id": sku_id, "dealer_org_id": org, "qty": qty}

    async def add_stock_units(self, session: AsyncSession, principal: Principal, *,
                              sku_id: int, dealer_org_id: int | None, sns: list[str],
                              note: str | None = None, ip: str | None = None
                              ) -> dict[str, Any]:
        """SN 入库。SN 归一化（去空格 + 大写）在这里做，重复的静默跳过。"""
        principal.require("product", "write")
        sku = await self.repo.sku_by_id(session, sku_id)
        if sku is None:
            raise NotFound("SKU 不存在", code="sku_not_found")
        if sku["inventory_kind"] != "serial":
            raise InvalidState("这个 SKU 是数量库存，请用数量入库", code="not_serial_sku")
        org = self._seller_org(principal, dealer_org_id)
        cleaned = []
        for sn in sns:
            v = "".join(str(sn).split()).upper()
            if not v:
                continue
            if len(v) > 64:
                raise ValidationFailed(f"SN 过长：{v[:16]}…", code="sn_too_long")
            cleaned.append(v)
        inserted = await self.repo.insert_stock_units(session, sku_id=sku_id,
                                                      dealer_org_id=org, sns=cleaned,
                                                      note=note)
        await write_audit(session, actor=principal, action="shop.stock.units",
                          resource_type="stock_unit", resource_id=sku_id,
                          after={"dealer_org_id": org, "inserted": inserted,
                                 "skipped": len(cleaned) - len(inserted)}, ip=ip)
        return {"sku_id": sku_id, "dealer_org_id": org, "inserted": inserted,
                "skipped": len(cleaned) - len(inserted), "requested": len(cleaned)}

    async def stock(self, session: AsyncSession, principal: Principal, *,
                    dealer_org_id: int | None = None, limit: int = 50, offset: int = 0
                    ) -> dict[str, Any]:
        principal.require("product", "read")
        org = None if principal.is_platform or principal.is_regulator \
            else self._seller_org(principal, dealer_org_id)
        items, total = await self.repo.stock_page(session, dealer_org_id=org or dealer_org_id,
                                                  limit=limit, offset=offset)
        return {"items": items, "total": total, "limit": limit, "offset": offset}

    async def units(self, session: AsyncSession, principal: Principal, *, sku_id: int | None = None,
                    status: str | None = None, limit: int = 50, offset: int = 0
                    ) -> dict[str, Any]:
        principal.require("product", "read")
        org = None if principal.is_platform or principal.is_regulator \
            else self._seller_org(principal, None)
        items, total = await self.repo.units_page(session, sku_id=sku_id, dealer_org_id=org,
                                                  status=status, limit=limit, offset=offset)
        return {"items": items, "total": total, "limit": limit, "offset": offset}

    async def merchant_orders(self, session: AsyncSession, principal: Principal, *,
                              status: str | None = None, limit: int = 20, offset: int = 0
                              ) -> dict[str, Any]:
        """商家看自己的单（平台/监管看全部）。**没有 org 的账号看到空列表**，
        而不是报错——经销商员工可能挂了多个组织，一个都没配也不该整页 500。"""
        principal.require("order", "read")
        if principal.is_platform or principal.is_regulator:
            orgs: list[int] | None = None
        else:
            orgs = principal.org_ids_for_line("dealer") or principal.org_ids()
            if not orgs:
                return {"items": [], "total": 0, "limit": limit, "offset": offset}
        if orgs is None:
            items, total = await self.repo.orders_page(session, status=status, limit=limit,
                                                       offset=offset)
        elif len(orgs) == 1:
            items, total = await self.repo.orders_page(session, dealer_org_id=orgs[0],
                                                       status=status, limit=limit, offset=offset)
        else:
            # 多组织：逐店取再合并（v1 的经销商员工通常只有一个店，不为此加 SQL 复杂度）
            items, total = [], 0
            for org in orgs:
                rows, n = await self.repo.orders_page(session, dealer_org_id=org, status=status,
                                                      limit=limit, offset=offset)
                items.extend(rows)
                total += n
        return {"items": items, "total": total, "limit": limit, "offset": offset}

    async def summary(self, session: AsyncSession, principal: Principal, *,
                      dealer_org_id: int | None = None) -> dict[str, Any]:
        """商家看板。**非平台身份必须落到自己店上**（见 `sales_summary` 的口径注）：

        - 平台/监管：`dealer_org_id` 可指定某店，不给就是全平台；
        - 经销商员工：默认自有门店集合，显式指定别家 → `org_out_of_scope`；
        - 其他身份（consumer/owner/driver）：**全 0**。`order:read` 是共享动作，
          给他们全平台口径等于把全站 GMV 漏出去。
        """
        principal.require("order", "read")
        if principal.is_platform or principal.is_regulator:
            orgs: list[int] | None = [dealer_org_id] if dealer_org_id else None
        else:
            mine = sorted(set(principal.org_ids_for_line("dealer") or principal.org_ids()))
            if not mine:
                return {"unpaid_orders": 0, "paid_orders": 0, "completed_orders": 0,
                        "revenue_minor": 0, "pending_minor": 0}
            if dealer_org_id and dealer_org_id not in mine:
                raise Forbidden("这不是你名下的门店", code="org_out_of_scope")
            orgs = [dealer_org_id] if dealer_org_id else mine
        return await self.repo.sales_summary(session, dealer_org_ids=orgs)

    # ══ worker ═══════════════════════════════════════════════════════════════
    async def close_expired(self, session: AsyncSession, *, limit: int = 200
                            ) -> dict[str, Any]:
        """超时未付款 → 取消订单并放回库存。

        两条都要做：只关单不放库存，货会被一张死单永久占住；
        只放库存不关单，用户回来还能对着一个 0 件库存的单付款。
        """
        released = await self.repo.release_expired_units(session, limit=limit)
        rows = await self.repo.expired_unpaid(session, limit=limit)
        closed = 0
        for r in rows:
            locked = await self.repo.lock_order(session, int(r["id"]))
            if locked is None or locked["status"] != "created":
                continue
            await self._release(session, locked)
            pay = await self.prepo.pay_order_for_order(session, int(locked["id"]))
            if pay is not None and pay["status"] in OPEN_PAY_STATUSES:
                await self.prepo.set_pay_status(session, pay_id=int(pay["id"]), status="timeout")
            await self.repo.mark_cancelled(session, int(locked["id"]), "支付超时")
            await emit(session, aggregate_type="commerce_order", aggregate_id=locked["id"],
                       event_type="cancelled",
                       payload={"order_no": locked["order_no"], "reason": "支付超时"})
            closed += 1
        return {"scanned": len(rows), "cancelled": closed, "units_released": released}

    # ══ 内部 ═════════════════════════════════════════════════════════════════
    async def _pay_instructions(self, session: AsyncSession, order: dict[str, Any],
                                pay: dict[str, Any] | None) -> dict[str, Any] | None:
        """订单详情里的"钱转到哪"：**只有 `created` 才给**（付完再显示账户等于诱导重复转账）。

        用下单时实际选中的渠道（`pay.channel_id`），不按当前配置重算——
        结算配置改了之后，用户仍要按他下单时看到的那条付款，否则对账找不到这笔钱。

        没有这个回显时，`pay_instructions` 只活在下单响应里：用户一刷新订单页就不知道该转给谁。
        """
        if pay is None or order["status"] != "created" or pay.get("channel_id") is None:
            return None
        ch = await self.prepo.channel_by_id(session, int(pay["channel_id"]))
        if ch is None:
            return None
        return {
            "channel_code": ch["channel_code"],
            "provider_type": ch["provider_type"],
            "account_entity": ch["account_entity"],
            "channel_id": int(ch["id"]),
            "amount": int(pay["amount"]),
            "currency": str(pay["currency"]),
            "expires_at": pay["expires_at"],
            "note": "转账后回填流水号，平台核销通过后到店提货",
        }

    async def _release(self, session: AsyncSession, order: dict[str, Any]) -> None:
        """归还库存（取消/超时的唯一出口）：SN 回在库，数量库存按原数量加回。"""
        await self.repo.release_units_of_order(session, int(order["id"]))
        items = await self.repo.order_items(session, int(order["id"]))
        for it in items:
            if it["inventory_kind"] == "qty":
                await self.repo.add_stock_qty(session, sku_id=int(it["sku_id"]),
                                              dealer_org_id=int(order["dealer_org_id"]),
                                              delta=int(it["qty"]))

    async def _available(self, session: AsyncSession, *, sku_id: int) -> int:
        """这个 SKU 的全网可售量。**只用于加购时的软校验**，真正的硬校验在下单：
        数量库存走条件 UPDATE、SN 走 `SKIP LOCKED` 挑件——这里算得再准也可能被别人抢走。"""
        sku = await self.repo.sku_by_id(session, sku_id)
        if sku is None:
            return 0
        if sku["inventory_kind"] == "serial":
            _items, total = await self.repo.units_page(session, sku_id=sku_id,
                                                       status="in_stock", limit=1, offset=0)
            return int(total)
        dealers = await self.repo.dealers_with_stock(session, sku_id=sku_id, limit=100)
        return sum(int(d["qty"]) for d in dealers)

    async def _resolve_lines(self, session: AsyncSession, principal: Principal, *,
                             items: list[dict[str, Any]] | None, dealer_region: str
                             ) -> list[dict[str, Any]]:
        """把"购物车/立即购买"解析成订单行，并把价格、币种、库存模型一次定死。

        **价格以库里为准**，绝不接受前端传来的价格：前端只传 `sku_code` 与 `qty`。
        """
        raw: list[dict[str, Any]] = []
        if items is not None:
            raw = [{"sku_code": i["sku_code"], "qty": int(i.get("qty", 1))} for i in items]
        else:
            cart = await self.repo.cart_rows(session, principal.account_id)
            raw = [{"sku_code": c["sku_code"], "qty": int(c["qty"])} for c in cart]
        if not raw:
            return []

        merged: dict[str, int] = {}
        for r in raw:
            merged[r["sku_code"]] = merged.get(r["sku_code"], 0) + r["qty"]

        lines: list[dict[str, Any]] = []
        for code, qty in merged.items():
            if qty <= 0:
                continue
            sku = await self.repo.sku_by_code(session, code)
            if sku is None or sku["status"] != "active":
                raise NotFound(f"商品 {code} 不存在或已下架", code="sku_not_found")
            if sku["region_id"] is not None and sku["region_id"] != dealer_region:
                raise ValidationFailed(f"商品 {code} 不在该经销商所在地区销售",
                                       code="sku_region_mismatch")
            lines.append({"sku_id": int(sku["id"]), "sku_code": code, "qty": qty,
                          "price": int(sku["price"]), "currency": str(sku["currency"]),
                          "product_type": str(sku["product_type"]),
                          "inventory_kind": str(sku["inventory_kind"])})
        return lines

    @staticmethod
    def _seller_org(principal: Principal, org_id: int | None) -> int:
        """卖方组织：平台必须显式指定，商家只能用自己的（与换电域 `_resolve_org` 同思路）。"""
        dealers = principal.org_ids_for_line("dealer") or principal.org_ids()
        if principal.is_platform:
            if org_id is None:
                raise ValidationFailed("平台必须显式指定 dealer_org_id",
                                       code="dealer_org_required")
            return int(org_id)
        if org_id is not None:
            if int(org_id) not in dealers:
                raise Forbidden("只能操作自己组织的数据", code="org_out_of_scope")
            return int(org_id)
        if not dealers:
            raise Forbidden("账号没有挂任何经销商组织", code="org_missing")
        return int(dealers[0])

    @staticmethod
    def _assert_readable(principal: Principal, order: dict[str, Any]) -> None:
        if principal.is_platform or principal.is_regulator:
            return
        if int(order["account_id"]) == principal.account_id:
            return
        if int(order["dealer_org_id"] or 0) in principal.org_ids_for_line("dealer"):
            return
        if int(order["dealer_org_id"] or 0) in principal.org_ids():
            return
        raise Forbidden("订单不在你的可见范围", code="order_out_of_scope")

    @staticmethod
    def _no(prefix: str) -> str:
        """单号：前缀 + 时间 + 6 位随机。**不做自增序列**——多实例下没有共享序列，
        而单号只需要可读 + 高概率唯一（`UNIQUE` 约束兜底）。"""
        return f"{prefix}{datetime.now(UTC):%y%m%d%H%M%S}{uuid4().hex[:6].upper()}"
