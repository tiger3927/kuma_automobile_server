"""积分包购买链路（`Task-Plan.md` §5.1 / §5.2 / §5.3 / §7.2 / §7.4 / §7.5）。

**这不是"充值接口"，是一条商品订单 + 收款核销链**（§5.1 自洽要点 2）：

    配包（平台：建 product + sku + points_pack）
      → 下单（用户：`commerce.order` `created` + `fin.pay_order` `qr_issued`）
      → 回填转账流水号（用户：`fin.payment_voucher` `pending`）
      → 平台核销（双人授权，`payout_verify:write` ∈ DUAL_CONTROL）
      → 同事务：pay_order `paid` → order `completed` → `points_purchase`
                → 积分入账（`purchased` 批次，按包期限过期）→ 履约 `digital delivered`
      → 负债进 `fin.points_liability` 日快照（§7.5 合同负债）

四道闸门全部来自 §7.2 / §6.4，**取值只在 `core.region.features`**
（§5.1"地区配置单一来源，禁止代码写死国家判断"）：

- **地区开关 `points_pack_purchase`**（`core.region.features`）：
  **三国 seed 全是 `false`**（§6.4：积分包具单用途预付卡性质，可能需备案）。
  打开它是业务与合规的决定，**代码不替业务打开**。
- **KYC 门槛 `kyc_level_for_points`**（同上，seed=2）：当地基础证件齐全才允许买积分。
- **限额 `points_pack_limits`**（同上）：单笔/单日/单月（金额是最小货币单位）；
  `review_above` 以上**直接拒**——人工复核工作台未实现，宁可不做也不能没复核就收大额。
- **同一支付账户给多账号购买**：靠库级 `uq_transfer_no`，同一流水号第二次核销必被拒。

**退款只允许"整包未消耗"**（§7.4 按原路 + 禁止交叉）：已花掉一部分还退现金，
等于把已消费的金额拿回来；部分退款要按批次剩余分摊并处理跨包混用的余额，v1 不做——
宁可拒并留人工口子。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.audit import write_audit
from ..core.exceptions import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from ..core.outbox import emit
from ..core.scope import Principal
from ..repositories.points_repo import PointsRepo
from ..repositories.purchase_repo import PurchaseRepo
from .dual_control import require_co_approver
from .points_service import PointsService, add_months

#: 静态二维码 + 回填流水号（v1 的真实收款方式，§5.3）；provider 取值域见 `pay_order` CHECK
PROVIDER_QR_TRANSFER = "qr_transfer"
#: 支付单有效期（§5.2 `pay_timeout` → 自动关单）。太短会被"银行转账到账要几分钟"坑到，
#: 太长会让僵尸支付单长期占着 `uq_pay_active`，用户重付不了。
PAY_TTL = timedelta(minutes=30)
#: 入账来源值（幂等键 `uq_ledger_source(source_type, source_ref, points_kind)` 的一部分）
PACK_SOURCE = "points_pack"
PACK_REFUND_SOURCE = "points_pack_refund"

#: 可付状态（§5.2 收款线）；`paid/closed/timeout` 之后不能再核销
OPEN_PAY_STATUSES = ("created", "qr_issued", "voucher_pending", "verifying")

_LIABILITY_SQL = text("""
    SELECT snapshot_date, account_id, booked_points, currency, cash_equiv, created_at
      FROM fin.points_liability WHERE snapshot_date = :d ORDER BY account_id
""")


class PurchaseService:
    repo = PurchaseRepo()
    points_repo = PointsRepo()
    points = PointsService()

    # ══ 用户侧：看包、下单、回填凭证 ═══════════════════════════════════════════
    async def packs(self, session: AsyncSession, principal: Principal) -> dict[str, Any]:
        """可买的包 + **为什么现在不能买**（前端要显示原因，不能只给空列表）。"""
        principal.require("points", "read")
        ctx = await self._account_context(session, principal)
        feats: dict[str, Any] = ctx["features"]
        return {
            "region_id": ctx["region_id"], "currency": ctx["currency"],
            "purchase_enabled": bool(feats.get("points_pack_purchase", False)),
            "kyc_required": int(feats.get("kyc_level_for_points", 0)),
            "kyc_level": int(ctx.get("kyc_level") or 0),   # 库里为准，与闸门同一来源
            "limits": feats.get("points_pack_limits") or {},
            "items": await self.repo.packs(session, str(ctx["region_id"])),
        }

    async def buy(self, session: AsyncSession, principal: Principal, *, pack_code: str,
                  idempotency_key: str, ip: str | None = None) -> dict[str, Any]:
        """下单 + 开支付单（同一事务）。**这一步不收钱、也不入账**——
        入账只发生在平台核销通过之后（§5.2 收款线：钱没确认到账就发积分，就是白送）。
        """
        principal.require("order", "write")
        if principal.status != "active":
            raise InvalidState("账号状态不允许购买", code="account_not_active")

        replay = await self.repo.order_by_idempotency(session, idempotency_key)
        if replay is not None:
            # 弱网重试：原样返回已建的单与支付单，不重复建单（`idempotency_key` UNIQUE 是防线）
            pay = await self.repo.pay_order_for_order(session, int(replay["id"]))
            return {"order": replay, "pay_order": pay, "replayed": True}

        ctx = await self._account_context(session, principal)
        feats: dict[str, Any] = ctx["features"]
        if not feats.get("points_pack_purchase", False):
            raise InvalidState(
                f"{ctx['region_id']} 未开放积分包购买（§6.4：具单用途预付卡性质，"
                "需当地备案/合规确认后才打开）", code="points_pack_disabled")
        # **用库里的 kyc_level，不用 token 里的**：`Principal.kyc_level` 来自 JWT，
        # 令牌签发后证件状态可能已变（补件/降级），拿它当闸门等于用旧快照放行。
        need_kyc = int(feats.get("kyc_level_for_points", 0))
        have_kyc = int(ctx.get("kyc_level") or 0)
        if have_kyc < need_kyc:
            raise Forbidden(f"购买积分需 KYC 等级 ≥ {need_kyc}（当前 {have_kyc}）",
                            code="kyc_required")

        pack = await self.repo.pack_by_code(session, pack_code)
        if pack is None or pack["status"] != "active":
            raise NotFound("积分包不存在或已下架", code="pack_not_found")
        if pack["region_id"] is not None and pack["region_id"] != ctx["region_id"]:
            raise ValidationFailed("该积分包不在你的地区销售", code="pack_region_mismatch")
        if pack["currency"] != ctx["currency"]:
            raise ValidationFailed("积分包币种与地区币种不一致", code="pack_currency_mismatch")
        if pack["sku_id"] is None:
            # 0009 之前的老包没有 sku：`order_item.sku_id` 是 NOT NULL FK，
            # 与其在 INSERT 时撞 FK，不如明确告诉运维"这个包没被商品化"
            raise InvalidState("该包未关联 sku，无法下单（需平台重新配置）",
                               code="pack_not_provisioned")

        await self._check_limits(session, feats, principal.account_id, int(pack["price"]))

        channel = await self.repo.channel_for(session, provider_type=PROVIDER_QR_TRANSFER,
                                              region_id=str(ctx["region_id"]),
                                              currency=str(ctx["currency"]))
        if channel is None:
            raise InvalidState(f"{ctx['region_id']}/{ctx['currency']} 没有可用的转账收款渠道配置",
                               code="pay_channel_missing")

        order = await self.repo.insert_order(
            session, order_no=self._no("PP"), account_id=principal.account_id,
            order_type="points_pack", region_id=str(ctx["region_id"]),
            currency=str(ctx["currency"]), amount_cash=int(pack["price"]),
            amount_points=int(pack["points"]), collect_mode="platform_online", status="created",
            idempotency_key=idempotency_key)
        await self.repo.insert_order_item(session, order_id=int(order["id"]),
                                         sku_id=int(pack["sku_id"]), product_type="points_pack",
                                         qty=1, unit_price=int(pack["price"]), points_cost=0)
        pay = await self.repo.insert_pay_order(
            session, pay_no=self._no("PAY"), order_id=int(order["id"]),
            provider=PROVIDER_QR_TRANSFER, channel_id=int(channel["id"]),
            amount=int(pack["price"]), currency=str(ctx["currency"]), status="qr_issued",
            expires_at=datetime.now(UTC) + PAY_TTL)
        await write_audit(session, actor=principal, action="points_pack.order.create",
                          resource_type="order", resource_id=order["id"],
                          after={"order_no": order["order_no"], "pack_code": pack_code,
                                 "amount": pack["price"], "currency": ctx["currency"],
                                 "pay_no": pay["pay_no"], "channel": channel["channel_code"]},
                          ip=ip)
        return {"order": order, "pay_order": pay, "replayed": False,
                # 收款指引只给库里真有的字段。`payment_channel` 没有存账号/二维码的列
                # （实测只有 `fee_rule` + `config_version`），二维码按房内惯例走
                # `core.media`（owner_type='payment_channel'、owner_id=渠道 id）由运营上传，
                # 前端取 media 即可——**不在这里编造一个 config 字段**。
                "pay_instructions": {
                    "channel_code": channel["channel_code"],
                    "provider_type": channel["provider_type"],
                    "account_entity": channel["account_entity"],
                    "channel_id": int(channel["id"]),
                    "config_version": channel.get("config_version"),
                    "amount": int(pay["amount"]), "currency": pay["currency"],
                    "expires_at": pay["expires_at"],
                    "note": "转账后回填流水号，平台核销通过后积分即时入账"}}

    async def upload_voucher(self, session: AsyncSession, principal: Principal, *, pay_no: str,
                             transfer_no: str, amount: int | None = None,
                             currency: str | None = None, proof_url: str | None = None,
                             ip: str | None = None) -> dict[str, Any]:
        """回填转账流水号 + 凭证（§5.2 `qr_issued → voucher_pending`）。"""
        principal.require("payment", "write")
        pay = await self.repo.pay_order_by_no(session, pay_no)
        if pay is None:
            raise NotFound("支付单不存在", code="pay_order_not_found")
        if int(pay["account_id"]) != principal.account_id:
            raise Forbidden("只能给自己的订单回填凭证", code="not_your_pay_order")
        # `pay_order_by_no` 里支付单自己的状态键是 `status`（订单的才叫 `order_status`）；
        # `voucher_by_id` 那边才把它 alias 成 `pay_status`——两处键名不同，写错就是 KeyError。
        if pay["status"] not in OPEN_PAY_STATUSES:
            raise InvalidState(f"支付单当前状态 {pay['status']} 不能回填凭证", code="pay_state")
        if pay["order_status"] != "created":
            raise InvalidState(f"订单当前状态 {pay['order_status']} 不能回填凭证",
                               code="order_state")
        if amount is not None and int(amount) != int(pay["amount"]):
            raise ValidationFailed("回填金额与应付金额不一致", code="voucher_amount_mismatch")
        if currency is not None and currency != pay["currency"]:
            raise ValidationFailed("回填币种与应付币种不一致", code="voucher_currency_mismatch")

        try:
            voucher = await self.repo.insert_voucher(
                session, pay_order_id=int(pay["id"]), transfer_no=transfer_no,
                amount=amount, currency=currency, proof_url=proof_url,
                uploaded_by=principal.account_id)
        except IntegrityError as e:
            # `uq_transfer_no`：同一笔转账给多个订单核销 = §7.2 点名的 v1 最大事故源，
            # 也正是"同一支付账户给多账号购买"的拦截点。
            # **必须先回滚**：事务已被 PG 标记为失败，不 rollback 后续任何写入都会连带炸。
            await session.rollback()
            raise Conflict("该转账流水号已被用于其它订单（不能重复核销）",
                           code="transfer_no_reused") from e
        await self.repo.set_pay_status(session, pay_id=int(pay["id"]), status="voucher_pending")
        await write_audit(session, actor=principal, action="points_pack.voucher.upload",
                          resource_type="payment_voucher", resource_id=voucher["id"],
                          after={"pay_no": pay_no, "transfer_no": transfer_no, "amount": amount},
                          ip=ip)
        return voucher

    # ══ 平台侧：核销 / 退款 / 配包 / 负债报表 ═══════════════════════════════════
    async def verify_voucher(self, session: AsyncSession, principal: Principal, *, voucher_id: int,
                             approve: bool, co_approver_account_id: int | None = None,
                             ip: str | None = None) -> dict[str, Any]:
        """核销转账凭证。通过 = **同事务**完成收款、订单完成、积分入账、履约（§7.2 即时入账）。

        `payout_verify:write` 在 `DUAL_CONTROL` 里 → 必须两个人签字（§9.5 收款核销工作台）。
        """
        principal.require("payout_verify", "write")
        await require_co_approver(session, principal, co_approver_account_id)

        v = await self.repo.voucher_by_id(session, voucher_id)
        if v is None:
            raise NotFound("凭证不存在", code="voucher_not_found")
        if v["status"] != "pending":
            raise InvalidState(f"凭证已处理（{v['status']}）", code="voucher_processed")
        if v["pay_status"] not in OPEN_PAY_STATUSES:
            raise InvalidState(f"支付单已是 {v['pay_status']}", code="pay_state")
        if v["order_status"] != "created":
            raise InvalidState(f"订单已是 {v['order_status']}", code="order_state")

        if not approve:
            # 驳回：凭证置 rejected（该流水号让出 `uq_transfer_no`，用户可换一笔重填），
            # 支付单退回 voucher_pending 让用户再填；不关单（关单交给超时 job，口径只有一处）
            await self.repo.set_voucher_status(session, voucher_id=voucher_id, status="rejected",
                                               verifier=principal.account_id)
            await self.repo.set_pay_status(session, pay_id=int(v["pay_order_id"]),
                                           status="voucher_pending")
            await write_audit(session, actor=principal, action="points_pack.voucher.reject",
                              resource_type="payment_voucher", resource_id=voucher_id,
                              after={"status": "rejected"}, ip=ip)
            return {"voucher_id": voucher_id, "status": "rejected"}

        pack = await self._pack_for_order(session, int(v["order_id"]))
        if pack is None:
            raise InvalidState("订单对应的积分包不存在", code="pack_not_found")

        await self.repo.set_voucher_status(session, voucher_id=voucher_id, status="verified",
                                           verifier=principal.account_id)
        pay = await self.repo.set_pay_status(session, pay_id=int(v["pay_order_id"]), status="paid",
                                             out_trade_no=str(v["transfer_no"]), mark_paid=True)
        # 虚拟线状态机（§5.2）：`paid → provisioning → completed`。
        # 数字商品没有"等第三方开通"的等待期，同事务直接 completed——
        # 中间态只在需要异步开通时才有意义，写个没人看的状态只是噪音。
        order = await self.repo.set_order_status(session, int(v["order_id"]), "completed")
        purchase = await self.repo.insert_purchase(
            session, purchase_no=self._no("PPU"), order_id=int(v["order_id"]),
            account_id=int(v["account_id"]), pack_id=int(pack["id"]), points=int(pack["points"]),
            amount=int(pack["price"]), currency=str(pack["currency"]))
        expires_at = add_months(datetime.now(UTC), int(pack["validity_months"]))
        credit = await self.points.grant(
            session, account_id=int(v["account_id"]), points=int(pack["points"]),
            points_kind="purchased", source_type=PACK_SOURCE, source_ref=purchase["purchase_no"],
            expires_at=expires_at, memo=f"积分包 {pack['pack_code']}")
        await self.repo.insert_fulfillment(session, order_id=int(v["order_id"]), type_="digital",
                                           status="delivered", confirmed_by=principal.account_id)
        await emit(session, aggregate_type="points_purchase", aggregate_id=purchase["id"],
                   event_type="completed",
                   payload={"purchase_no": purchase["purchase_no"],
                            "account_id": int(v["account_id"]), "pack_code": pack["pack_code"],
                            "points": int(pack["points"]), "amount": int(pack["price"]),
                            "currency": pack["currency"], "expires_at": expires_at.isoformat()})
        await write_audit(session, actor=principal, action="points_pack.voucher.verify",
                          resource_type="payment_voucher", resource_id=voucher_id,
                          after={"status": "verified", "pay_no": v["pay_no"],
                                 "purchase_no": purchase["purchase_no"],
                                 "points": int(pack["points"]),
                                 "co_approver_account_id": co_approver_account_id}, ip=ip)
        return {"voucher_id": voucher_id, "status": "verified", "pay_order": pay, "order": order,
                "purchase": purchase, "credit": credit}

    async def refund_purchase(self, session: AsyncSession, principal: Principal, *,
                              purchase_no: str, reason: str | None = None,
                              co_approver_account_id: int | None = None,
                              ip: str | None = None) -> dict[str, Any]:
        """积分包退款：现金按原路退 + 积分整包扣回（§7.4 按原路、**禁止交叉**）。

        `fin.refund.mode='cash'` + `points_reversal_ref` 同时写：
        只退钱不扣积分 = 免费消费；只扣积分退现金券 = 积分变现通道。两条都被 CHECK 挡住。
        """
        principal.require("payout_verify", "write")
        await require_co_approver(session, principal, co_approver_account_id)

        purchase = await self.repo.purchase_by_no(session, purchase_no)
        if purchase is None:
            raise NotFound("购买记录不存在", code="purchase_not_found")
        if purchase["status"] != "completed":
            raise InvalidState(f"购买已是 {purchase['status']}", code="purchase_state")
        if purchase["pay_order_id"] is None:
            raise InvalidState("购买没有关联支付单，无法按原路退", code="pay_order_missing")
        if purchase["account_id"] is None:
            raise InvalidState("购买记录缺少 account_id（0009 之前的数据），走人工处理",
                               code="purchase_account_missing")

        batch = await self.repo.purchase_batch(session, purchase_no)
        if batch is None:
            raise InvalidState("找不到该购买对应的积分批次", code="pack_batch_missing")
        total, remaining = int(batch["total"]), int(batch["remaining"])
        if remaining != total:
            # 已消耗（或已过期被清零）都不给自动退：部分退要按批次剩余分摊并处理跨包混用，
            # v1 不做——宁可拒并留人工口子，也不能算错一分负债
            raise InvalidState(f"该包已变动 {total - remaining} 积分，不支持自动退款（走人工）",
                               code="points_pack_partially_spent")

        account_id = int(purchase["account_id"])
        # 锁顺序仍是 账户 → 批次（与消耗、过期路径一致，反序就死锁）
        acc = await self.points_repo.lock_account(session, account_id)
        if not await self.repo.zero_batch(session, batch_id=int(batch["id"]),
                                          expect_remaining=total):
            raise Conflict("批次余额已变化（刚被消耗或过期），退款中止重试", code="batch_changed")

        points = total
        purchased = int(acc["purchased_balance"]) - points
        await self.points_repo.insert_ledger(
            session, account_id=account_id, points_kind="purchased", delta=-points,
            balance_after=purchased, source_type=PACK_REFUND_SOURCE, source_ref=purchase_no,
            memo="积分包退款扣回（§7.4 按原路）")
        await self.points_repo.set_balances(session, account_id=account_id,
                                            earned=int(acc["earned_balance"]),
                                            purchased=purchased)
        refund = await self.repo.insert_refund(
            session, refund_no=self._no("RF"), pay_order_id=int(purchase["pay_order_id"]),
            amount=int(purchase["amount"]), currency=str(purchase["currency"]), mode="cash",
            reason=reason, status="processed", processed_by=principal.account_id,
            points_reversal_ref=purchase_no)
        await self.repo.set_purchase_status(session, purchase_no=purchase_no, status="refunded")
        await self.repo.set_order_status(session, int(purchase["order_id"]), "refunded")
        await emit(session, aggregate_type="points_purchase", aggregate_id=int(purchase["id"]),
                   event_type="refunded",
                   payload={"purchase_no": purchase_no, "account_id": account_id,
                            "points": points, "amount": int(purchase["amount"]),
                            "currency": purchase["currency"], "refund_no": refund["refund_no"]})
        await write_audit(session, actor=principal, action="points_pack.refund",
                          resource_type="points_purchase", resource_id=int(purchase["id"]),
                          before={"points": points, "amount": purchase["amount"]},
                          after={"status": "refunded", "refund_no": refund["refund_no"],
                                 "co_approver_account_id": co_approver_account_id}, ip=ip)
        return {"refund": refund, "purchase_no": purchase_no, "points_reversed": points}

    async def my_purchases(self, session: AsyncSession, principal: Principal, *, limit: int = 50,
                           offset: int = 0) -> dict[str, Any]:
        principal.require("points", "read")
        return {"items": await self.repo.purchases_of_account(session, principal.account_id,
                                                              limit=limit, offset=offset)}

    async def liability(self, session: AsyncSession, principal: Principal, *,
                        snapshot_date) -> dict[str, Any]:
        """积分负债报表（§7.5：现金买来的未消耗积分是负债，必须与 ledger 重算一致）。"""
        principal.require("points_liability", "read")
        rows = (await session.execute(_LIABILITY_SQL, {"d": snapshot_date})).mappings().all()
        return {"snapshot_date": snapshot_date.isoformat(),
                "accounts": len(rows), "items": [dict(r) for r in rows],
                "untraceable": await self.points_repo.liability_untraceable(session,
                                                                            snapshot_date)}

    async def create_pack(self, session: AsyncSession, principal: Principal, *, pack_code: str,
                          points: int, price: int, currency: str, validity_months: int,
                          region_id: str | None, name: str,
                          ip: str | None = None) -> dict[str, Any]:
        """配包 = 一次落 `product` + `sku` + `points_pack`（§5.1：积分包是商品）。"""
        principal.require("points", "config")
        if points <= 0 or price <= 0:
            raise ValidationFailed("points 与 price 必须为正", code="pack_amount_invalid")
        if validity_months <= 0:
            raise ValidationFailed("validity_months 必须为正（§7.2 默认 24 个月）",
                                   code="pack_validity_invalid")
        if await self.repo.pack_by_code(session, pack_code) is not None:
            raise Conflict("pack_code 已存在", code="pack_exists")
        pack = await self.repo.create_pack(session, pack_code=pack_code, points=points,
                                           price=price, currency=currency,
                                           validity_months=validity_months, region_id=region_id,
                                           name=name)
        await write_audit(session, actor=principal, action="points_pack.create",
                          resource_type="points_pack", resource_id=pack["id"],
                          after={"pack_code": pack_code, "points": points, "price": price,
                                 "currency": currency, "sku_id": pack["sku_id"]}, ip=ip)
        return pack

    async def retire_pack(self, session: AsyncSession, principal: Principal, *, pack_code: str,
                          ip: str | None = None) -> dict[str, Any]:
        principal.require("points", "config")
        pack = await self.repo.retire_pack(session, pack_code)
        if pack is None:
            raise NotFound("积分包不存在或已下架", code="pack_not_found")
        await write_audit(session, actor=principal, action="points_pack.retire",
                          resource_type="points_pack", resource_id=pack["id"],
                          after={"status": "retired"}, ip=ip)
        return pack

    # ══ worker job：超时关单（§5.2 `pay_timeout`）══════════════════════════════
    async def close_expired_pay_orders(self, session: AsyncSession, *, limit: int = 200
                                       ) -> dict[str, Any]:
        """超时未回填/未过核销 → 支付单 `timeout`。

        不关的后果不是"难看"而是**用户付不了第二次**：
        `uq_pay_active` 只允许一单一张进行中的支付单，僵尸单会一直占着这个名额。
        订单本身留给下一版决定（`created` 单可重开支付单，不必强行 cancelled）。
        """
        rows = await self.repo.expired_open_pay_orders(session, limit=limit)
        closed = 0
        for r in rows:
            if await self.repo.set_pay_status(session, pay_id=int(r["id"]),
                                              status="timeout") is not None:
                closed += 1
        return {"scanned": len(rows), "closed": closed}

    # ══ 内部 ═════════════════════════════════════════════════════════════════
    async def _account_context(self, session: AsyncSession,
                               principal: Principal) -> dict[str, Any]:
        """账号地区 + 地区配置（开关 / KYC / 限额的唯一来源）。"""
        ctx = await self.repo.account_region(session, principal.account_id)
        if ctx is None or ctx.get("region_id") is None:
            raise ValidationFailed("账号没有地区，无法确定币种与合规口径", code="region_missing")
        if ctx.get("region_status") != "active":
            raise InvalidState("该地区未启用", code="region_inactive")
        if ctx.get("currency") is None:
            raise ValidationFailed("该地区未配置币种", code="region_currency_missing")
        ctx["features"] = ctx.get("features") or {}
        return ctx

    async def _check_limits(self, session: AsyncSession, feats: dict[str, Any], account_id: int,
                            price: int) -> None:
        """§7.2 购买上限：单笔 / 单日 / 单月，`review_above` 以上直接拒。

        分子含**未支付的订单**：只算已付的会留口子——先开 20 单不付（看起来没占额度），
        等额度检查过了再一次性付掉。
        """
        limits = feats.get("points_pack_limits") or {}
        per_order = limits.get("per_order_max")
        if per_order is not None and price > int(per_order):
            raise ValidationFailed(f"超过单笔购买上限 {per_order}", code="pack_over_order_limit")
        review_above = limits.get("review_above")
        if review_above is not None and price > int(review_above):
            raise ValidationFailed("超过免复核额度，需人工复核后才能购买（复核工作台未实现）",
                                   code="pack_above_review_limit")
        now = datetime.now(UTC)
        for key, days, code in (("per_day_max", 1, "pack_over_day_limit"),
                                ("per_month_max", 30, "pack_over_month_limit")):
            cap = limits.get(key)
            if cap is None:
                continue
            since = now - timedelta(days=days)     # "月"按 30 天滚动窗口，不按自然月
            used = await self.repo.purchased_amount_since(session, account_id=account_id,
                                                          since=since)
            open_amt = await self.repo.open_order_amount_since(session, account_id=account_id,
                                                               since=since)
            if used + open_amt + price > int(cap):
                raise ValidationFailed(
                    f"超过 {days} 日内购买上限 {cap}（已用 {used}，未付 {open_amt}）", code=code)

    async def _pack_for_order(self, session: AsyncSession,
                              order_id: int) -> dict[str, Any] | None:
        row = (await session.execute(text("""
            SELECT pck.id, pck.pack_code, pck.points, pck.price, pck.currency,
                   pck.validity_months, pck.status
              FROM mkt.points_pack pck
              JOIN commerce.order_item it ON it.sku_id = pck.sku_id
             WHERE it.order_id = :o
             ORDER BY it.id LIMIT 1
        """), {"o": order_id})).mappings().one_or_none()
        return dict(row) if row else None

    @staticmethod
    def _no(prefix: str) -> str:
        return f"{prefix}{datetime.now(UTC):%y%m%d%H%M%S}{uuid4().hex[:6]}"
