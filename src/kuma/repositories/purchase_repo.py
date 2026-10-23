"""积分包购买的取数与写入：`mkt.points_pack` / `points_purchase` + `commerce` 订单骨架
+ `fin` 收款线（`Task-Plan.md` §5.1 / §5.2 / §5.3 / §7.2 / §7.5）。

**为什么买积分要动三套 schema**（§5.1 自洽要点 2）：积分包是**商品**不是"充值接口"。
现金买积分必须走同一订单骨架、同一支付核销流程、同一退款规则、同一发票口径——
否则"现金换积分"就成了一条游离在财务与合规之外的旁路。三张 NOT NULL FK 把这条路钉死了：

    mkt.points_purchase.order_id  → commerce."order".id      （没有订单就没有购买记录）
    commerce.order_item.sku_id    → commerce.sku.id          （没有 sku 就开不出订单行）
    mkt.points_pack.sku_id        → commerce.sku.id          （0009：配包时必须建 sku）

**限额与开关不在代码里**（§5.1"地区配置单一来源，禁止代码写死国家判断"）：
`core.region.features` 里有 `points_pack_purchase`（地区开关）、`kyc_level_for_points`（KYC 门槛）、
`points_pack_limits`（单笔/单日/单月上限）。三国 seed 全是 `false`（§6.4 单用途预付卡可能要备案），
**打开它是业务与合规的决定，不是代码的默认值**。

**幂等**：`commerce."order".idempotency_key` UNIQUE（弱网重试）+
`fin.pay_order` 的 `uq_pay_active`（一单只允许一张进行中的支付单）+
`fin.payment_voucher` 的 `uq_transfer_no`（同一流水号跨订单重复核销 = v1 最大事故源，
也正是 §7.2"同一支付账户给多账号购买"的拦截点）+
`uq_ledger_source`（入账幂等）。
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

PACK_COLS = ("id, pack_code, points, price, currency, validity_months, region_id, status, sku_id")
ORDER_COLS = ('id, order_no, account_id, order_type, region_id, currency, amount_cash, '
              'amount_points, collect_mode, status, idempotency_key, created_at')
PAY_COLS = ("id, pay_no, order_id, provider, channel_id, amount, currency, status, "
            "out_trade_no, expires_at, paid_at, created_at")
VOUCHER_COLS = ("id, pay_order_id, transfer_no, amount, currency, proof_url, uploaded_by, "
                "status, verified_by, verified_at, created_at")
PURCHASE_COLS = ("id, purchase_no, order_id, account_id, pack_id, points, amount, currency, "
                 "status, paid_at, created_at")


class PurchaseRepo:
    # ══ 地区配置（开关 / KYC 门槛 / 限额的唯一来源）═══════════════════════════
    async def account_region(self, session: AsyncSession, account_id: int
                             ) -> dict[str, Any] | None:
        """账号地区 + 该地区配置。

        **不读 token 里的地区**：`Principal` 没有 region 字段，而且地区决定币种与合规口径
        （§5.1"地区配置单一来源"），只能以库里的账号为准。
        """
        row = (await session.execute(text("""
            SELECT a.id AS account_id, a.status AS account_status, a.kyc_level,
                   a.region_id, r.currency, r.status AS region_status, r.features
              FROM core.account a
              LEFT JOIN core.region r ON r.country_code = a.region_id
             WHERE a.id = :id
        """), {"id": account_id})).mappings().one_or_none()
        return dict(row) if row else None

    async def region(self, session: AsyncSession, region_id: str) -> dict[str, Any] | None:
        row = (await session.execute(text("""
            SELECT country_code, currency, timezone, features, status FROM core.region
             WHERE country_code = :id
        """), {"id": region_id})).mappings().one_or_none()
        return dict(row) if row else None

    # ══ 积分包（配包 = 建 product + sku + pack，一次做完）══════════════════════
    async def packs(self, session: AsyncSession, region_id: str) -> list[dict[str, Any]]:
        rows = (await session.execute(text(f"""
            SELECT {PACK_COLS} FROM mkt.points_pack
             WHERE status = 'active' AND (region_id IS NULL OR region_id = :r)
             ORDER BY points
        """), {"r": region_id})).mappings().all()
        return [dict(r) for r in rows]

    async def pack_by_code(self, session: AsyncSession, pack_code: str) -> dict[str, Any] | None:
        row = (await session.execute(text(f"""
            SELECT {PACK_COLS} FROM mkt.points_pack WHERE pack_code = :c
        """), {"c": pack_code})).mappings().one_or_none()
        return dict(row) if row else None

    async def create_pack(self, session: AsyncSession, *, pack_code: str, points: int,
                          price: int, currency: str, validity_months: int,
                          region_id: str | None, name: str) -> dict[str, Any]:
        """一个事务里落 `product` → `sku` → `points_pack`（顺序不能反：sku 依赖 product）。

        每张表 insert 后都 `flush()`：本仓没有 relationship，unit of work 不知道父子顺序，
        不显式 flush 就会把子表 INSERT 排到前面撞 FK（`Task-Server.md` §7 铁律 7）。
        """
        product_id = (await session.execute(text("""
            INSERT INTO commerce.product (product_type, category, name, currency, region_id, status)
            VALUES ('points_pack', 'points', :name, :currency, :region_id, 'active')
            RETURNING id
        """), {"name": name, "currency": currency, "region_id": region_id})).scalar_one()
        await session.flush()
        sku_id = (await session.execute(text("""
            INSERT INTO commerce.sku (product_id, code, attrs, price, currency, status)
            VALUES (:pid, :code, CAST(:attrs AS jsonb), :price, :currency, 'active')
            RETURNING id
        """), {"pid": product_id, "code": f"PACK-{pack_code}", "price": price,
               "currency": currency,
               "attrs": json.dumps({"points": points, "validity_months": validity_months})
               })).scalar_one()
        await session.flush()
        row = (await session.execute(text(f"""
            INSERT INTO mkt.points_pack (pack_code, points, price, currency, validity_months,
                                         region_id, status, sku_id)
            VALUES (:pack_code, :points, :price, :currency, :validity_months, :region_id,
                    'active', :sku_id)
            RETURNING {PACK_COLS}
        """), {"pack_code": pack_code, "points": points, "price": price, "currency": currency,
               "validity_months": validity_months, "region_id": region_id, "sku_id": sku_id,
               })).mappings().one()
        await session.flush()
        return dict(row)

    async def retire_pack(self, session: AsyncSession, pack_code: str
                          ) -> dict[str, Any] | None:
        """下架：包、sku、product 一起 retired（只停包会留下"能下单但没包"的半死状态）。"""
        row = (await session.execute(text(f"""
            UPDATE mkt.points_pack SET status = 'retired'
             WHERE pack_code = :c AND status = 'active'
             RETURNING {PACK_COLS}
        """), {"c": pack_code})).mappings().one_or_none()
        if row is None:
            return None
        await session.execute(text("""
            UPDATE commerce.sku SET status = 'retired' WHERE id = :s
        """), {"s": row["sku_id"]})
        await session.execute(text("""
            UPDATE commerce.product SET status = 'retired'
             WHERE id = (SELECT product_id FROM commerce.sku WHERE id = :s)
        """), {"s": row["sku_id"]})
        await session.flush()
        return dict(row)

    # ══ 订单骨架 ═════════════════════════════════════════════════════════════
    async def order_by_idempotency(self, session: AsyncSession, key: str
                                   ) -> dict[str, Any] | None:
        row = (await session.execute(text(f"""
            SELECT {ORDER_COLS} FROM commerce."order" WHERE idempotency_key = :k
        """), {"k": key})).mappings().one_or_none()
        return dict(row) if row else None

    async def insert_order(self, session: AsyncSession, *, order_no: str, account_id: int,
                           order_type: str, region_id: str, currency: str, amount_cash: int,
                           amount_points: int, collect_mode: str, status: str,
                           idempotency_key: str) -> dict[str, Any]:
        row = (await session.execute(text(f"""
            INSERT INTO commerce."order" (order_no, account_id, order_type, region_id, currency,
                amount_cash, amount_points, collect_mode, status, idempotency_key)
            VALUES (:order_no, :account_id, :order_type, :region_id, :currency,
                    :amount_cash, :amount_points, :collect_mode, :status, :idempotency_key)
            RETURNING {ORDER_COLS}
        """), {"order_no": order_no, "account_id": account_id, "order_type": order_type,
               "region_id": region_id, "currency": currency, "amount_cash": amount_cash,
               "amount_points": amount_points, "collect_mode": collect_mode, "status": status,
               "idempotency_key": idempotency_key, })).mappings().one()
        await session.flush()
        return dict(row)

    async def insert_order_item(self, session: AsyncSession, *, order_id: int, sku_id: int,
                                product_type: str, qty: int, unit_price: int,
                                points_cost: int) -> int:
        oid = (await session.execute(text("""
            INSERT INTO commerce.order_item (order_id, sku_id, product_type, qty, unit_price,
                                             points_cost)
            VALUES (:order_id, :sku_id, :product_type, :qty, :unit_price, :points_cost)
            RETURNING id
        """), {"order_id": order_id, "sku_id": sku_id, "product_type": product_type, "qty": qty,
               "unit_price": unit_price, "points_cost": points_cost})).scalar_one()
        await session.flush()
        return int(oid)

    async def set_order_status(self, session: AsyncSession, order_id: int, status: str
                               ) -> dict[str, Any] | None:
        row = (await session.execute(text(f"""
            UPDATE commerce."order" SET status = :s, updated_at = now()
             WHERE id = :id RETURNING {ORDER_COLS}
        """), {"s": status, "id": order_id})).mappings().one_or_none()
        await session.flush()
        return dict(row) if row else None

    async def insert_fulfillment(self, session: AsyncSession, *, order_id: int, type_: str,
                                 status: str, confirmed_by: int | None) -> int:
        fid = (await session.execute(text("""
            INSERT INTO commerce.fulfillment (order_id, type, status, delivered_at, confirmed_by)
            VALUES (:order_id, :type, :status, now(), :confirmed_by)
            RETURNING id
        """), {"order_id": order_id, "type": type_, "status": status,
               "confirmed_by": confirmed_by})).scalar_one()
        await session.flush()
        return int(fid)

    # ══ 收款线（pay_order / voucher）══════════════════════════════════════════
    async def channel_for(self, session: AsyncSession, *, provider_type: str, region_id: str,
                          currency: str) -> dict[str, Any] | None:
        """该地区在用的收款渠道（§5.3"改行不改代码"）。

        `regions` / `currencies` 是 jsonb 数组，用 `@>` 判包含——
        入参必须 `json.dumps`（`text()` 绑定不带列类型，asyncpg 收到 list 直接 DataError）。
        """
        row = (await session.execute(text("""
            SELECT id, channel_code, provider_type, collect_mode, account_entity,
                   fee_rule, config_version
              FROM fin.payment_channel
             WHERE provider_type = :p AND status = 'on'
               AND regions @> CAST(:regions AS jsonb)
               AND currencies @> CAST(:cur AS jsonb)
             ORDER BY id LIMIT 1
        """), {"p": provider_type, "regions": json.dumps([region_id]),
               "cur": json.dumps([currency])})).mappings().one_or_none()
        return dict(row) if row else None

    async def insert_pay_order(self, session: AsyncSession, *, pay_no: str, order_id: int,
                               provider: str, channel_id: int | None, amount: int,
                               currency: str, status: str,
                               expires_at: datetime | None) -> dict[str, Any]:
        row = (await session.execute(text(f"""
            INSERT INTO fin.pay_order (pay_no, order_id, provider, channel_id, amount, currency,
                                       status, expires_at)
            VALUES (:pay_no, :order_id, :provider, :channel_id, :amount, :currency,
                    :status, :expires_at)
            RETURNING {PAY_COLS}
        """), {"pay_no": pay_no, "order_id": order_id, "provider": provider,
               "channel_id": channel_id, "amount": amount, "currency": currency,
               "status": status, "expires_at": expires_at, })).mappings().one()
        await session.flush()
        return dict(row)

    async def pay_order_by_no(self, session: AsyncSession, pay_no: str) -> dict[str, Any] | None:
        """支付单 + 它挂的订单（核销要同时看两边状态，分开查会读到不一致的快照）。"""
        row = (await session.execute(text(f"""
            SELECT p.{', p.'.join(PAY_COLS.split(', '))},
                   o.order_no, o.account_id, o.status AS order_status, o.region_id,
                   o.order_type, o.collect_mode
              FROM fin.pay_order p
              JOIN commerce."order" o ON o.id = p.order_id
             WHERE p.pay_no = :no
        """), {"no": pay_no})).mappings().one_or_none()
        return dict(row) if row else None

    async def pay_order_for_order(self, session: AsyncSession, order_id: int
                                  ) -> dict[str, Any] | None:
        """订单的支付单（幂等重放时要把已存在的支付单原样返回，不能靠单号字符串推导）。"""
        row = (await session.execute(text(f"""
            SELECT {PAY_COLS} FROM fin.pay_order WHERE order_id = :id ORDER BY id DESC LIMIT 1
        """), {"id": order_id})).mappings().one_or_none()
        return dict(row) if row else None

    async def set_pay_status(self, session: AsyncSession, *, pay_id: int, status: str,
                             out_trade_no: str | None = None, mark_paid: bool = False,
                             ) -> dict[str, Any] | None:
        row = (await session.execute(text(f"""
            UPDATE fin.pay_order
               SET status = :s,
                   out_trade_no = COALESCE(:otn, out_trade_no),
                   paid_at = CASE WHEN :mark_paid THEN now() ELSE paid_at END
             WHERE id = :id RETURNING {PAY_COLS}
        """), {"s": status, "otn": out_trade_no, "mark_paid": mark_paid,
               "id": pay_id})).mappings().one_or_none()
        await session.flush()
        return dict(row) if row else None

    async def insert_voucher(self, session: AsyncSession, *, pay_order_id: int, transfer_no: str,
                             amount: int | None, currency: str | None, proof_url: str | None,
                             uploaded_by: int) -> dict[str, Any]:
        """插转账凭证。**重复流水号会抛 IntegrityError**（`uq_transfer_no`），
        由 services 层翻译成可读错误——这条约束是"同一笔转账给多个订单核销"的唯一防线。
        """
        row = (await session.execute(text(f"""
            INSERT INTO fin.payment_voucher (pay_order_id, transfer_no, amount, currency,
                                             proof_url, uploaded_by, status)
            VALUES (:pay_order_id, :transfer_no, :amount, :currency, :proof_url,
                    :uploaded_by, 'pending')
            RETURNING {VOUCHER_COLS}
        """), {"pay_order_id": pay_order_id, "transfer_no": transfer_no, "amount": amount,
               "currency": currency, "proof_url": proof_url, "uploaded_by": uploaded_by,
               })).mappings().one()
        await session.flush()
        return dict(row)

    async def voucher_by_id(self, session: AsyncSession, voucher_id: int
                            ) -> dict[str, Any] | None:
        row = (await session.execute(text(f"""
            SELECT v.{', v.'.join(VOUCHER_COLS.split(', '))},
                   p.pay_no, p.order_id, p.amount AS pay_amount, p.currency AS pay_currency,
                   p.status AS pay_status, p.channel_id,
                   o.order_no, o.account_id, o.status AS order_status, o.order_type
              FROM fin.payment_voucher v
              JOIN fin.pay_order p ON p.id = v.pay_order_id
              JOIN commerce."order" o ON o.id = p.order_id
             WHERE v.id = :id
        """), {"id": voucher_id})).mappings().one_or_none()
        return dict(row) if row else None

    async def set_voucher_status(self, session: AsyncSession, *, voucher_id: int, status: str,
                                 verifier: int) -> dict[str, Any] | None:
        row = (await session.execute(text(f"""
            UPDATE fin.payment_voucher
               SET status = :s, verified_by = :by, verified_at = now()
             WHERE id = :id AND status = 'pending' RETURNING {VOUCHER_COLS}
        """), {"s": status, "by": verifier, "id": voucher_id})).mappings().one_or_none()
        await session.flush()
        return dict(row) if row else None

    async def expired_open_pay_orders(self, session: AsyncSession, *, limit: int
                                      ) -> list[dict[str, Any]]:
        """超时未回填/未过核销的支付单（§5.2 `pay_timeout` → 自动关单）。

        不关会怎样：`uq_pay_active` 让这单永远占着"进行中"，用户重新下单也付不了，
        订单卡在 `created` 变成僵尸单。
        """
        rows = (await session.execute(text("""
            SELECT id, pay_no, order_id, status FROM fin.pay_order
             WHERE status IN ('created','qr_issued','voucher_pending','verifying')
               AND expires_at IS NOT NULL AND expires_at < now()
             ORDER BY id LIMIT :lim
        """), {"lim": limit})).mappings().all()
        return [dict(r) for r in rows]

    # ══ 购买记录与限额 ════════════════════════════════════════════════════════
    async def insert_purchase(self, session: AsyncSession, *, purchase_no: str, order_id: int,
                              account_id: int, pack_id: int, points: int, amount: int,
                              currency: str) -> dict[str, Any]:
        row = (await session.execute(text(f"""
            INSERT INTO mkt.points_purchase (purchase_no, order_id, account_id, pack_id, points,
                                             amount, currency, status, paid_at)
            VALUES (:purchase_no, :order_id, :account_id, :pack_id, :points, :amount,
                    :currency, 'completed', now())
            RETURNING {PURCHASE_COLS}
        """), {"purchase_no": purchase_no, "order_id": order_id, "account_id": account_id,
               "pack_id": pack_id, "points": points, "amount": amount, "currency": currency,
               })).mappings().one()
        await session.flush()
        return dict(row)

    async def purchase_by_no(self, session: AsyncSession, purchase_no: str
                             ) -> dict[str, Any] | None:
        row = (await session.execute(text(f"""
            SELECT pp.{', pp.'.join(PURCHASE_COLS.split(', '))},
                   pck.pack_code, pck.validity_months,
                   o.order_no, o.status AS order_status, o.collect_mode,
                   po.id AS pay_order_id, po.pay_no, po.status AS pay_status
              FROM mkt.points_purchase pp
              JOIN mkt.points_pack pck ON pck.id = pp.pack_id
              JOIN commerce."order" o ON o.id = pp.order_id
              LEFT JOIN fin.pay_order po ON po.order_id = pp.order_id
             WHERE pp.purchase_no = :no
        """), {"no": purchase_no})).mappings().one_or_none()
        return dict(row) if row else None

    async def purchases_of_account(self, session: AsyncSession, account_id: int, *, limit: int,
                                   offset: int) -> list[dict[str, Any]]:
        rows = (await session.execute(text(f"""
            SELECT {PURCHASE_COLS} FROM mkt.points_purchase
             WHERE account_id = :id ORDER BY created_at DESC, id DESC LIMIT :lim OFFSET :off
        """), {"id": account_id, "lim": limit, "off": offset})).mappings().all()
        return [dict(r) for r in rows]

    async def purchased_amount_since(self, session: AsyncSession, *, account_id: int,
                                     since: datetime) -> int:
        """已付成功的购买金额合计（§7.2 日/月上限的分子）。"""
        return int((await session.execute(text("""
            SELECT COALESCE(SUM(amount), 0) FROM mkt.points_purchase
             WHERE account_id = :a AND status = 'completed' AND paid_at >= :since
        """), {"a": account_id, "since": since})).scalar() or 0)

    async def open_order_amount_since(self, session: AsyncSession, *, account_id: int,
                                      since: datetime) -> int:
        """未支付/未关闭的积分包订单金额（占额度的部分）。

        只算已付的会留一个口子：先开 20 单不付，等额度看起来没用完再一次性付掉。
        """
        return int((await session.execute(text("""
            SELECT COALESCE(SUM(amount_cash), 0) FROM commerce."order"
             WHERE account_id = :a AND order_type = 'points_pack'
               AND status NOT IN ('completed','cancelled','refunded') AND created_at >= :since
        """), {"a": account_id, "since": since})).scalar() or 0)

    # ══ 退款（按原路，§7.4）═══════════════════════════════════════════════════
    async def purchase_batch(self, session: AsyncSession, purchase_no: str
                             ) -> dict[str, Any] | None:
        """这次购买开出的批次（退款要按它判断"有没有被消耗"）。"""
        row = (await session.execute(text("""
            SELECT id, total, remaining, points_kind, expires_at FROM mkt.points_batch
             WHERE source_ref = :sr AND points_kind = 'purchased' ORDER BY id LIMIT 1
        """), {"sr": purchase_no})).mappings().one_or_none()
        return dict(row) if row else None

    async def zero_batch(self, session: AsyncSession, *, batch_id: int, expect_remaining: int
                         ) -> bool:
        """按预期余额清零（`remaining = :expect` 是并发防线：期间被消耗过就更新 0 行）。"""
        got = (await session.execute(text("""
            UPDATE mkt.points_batch SET remaining = 0
             WHERE id = :id AND remaining = :expect RETURNING id
        """), {"id": batch_id, "expect": expect_remaining})).scalar()
        await session.flush()
        return got is not None

    async def insert_refund(self, session: AsyncSession, *, refund_no: str, pay_order_id: int,
                            amount: int, currency: str, mode: str, reason: str | None,
                            status: str, processed_by: int,
                            points_reversal_ref: str) -> dict[str, Any]:
        row = (await session.execute(text("""
            INSERT INTO fin.refund (refund_no, pay_order_id, amount, currency, mode, reason,
                                    status, processed_by, points_reversal_ref)
            VALUES (:refund_no, :pay_order_id, :amount, :currency, :mode, :reason,
                    :status, :processed_by, :points_reversal_ref)
            RETURNING id, refund_no, pay_order_id, amount, currency, mode, reason, status,
                      processed_by, points_reversal_ref, created_at
        """), {"refund_no": refund_no, "pay_order_id": pay_order_id, "amount": amount,
               "currency": currency, "mode": mode, "reason": reason, "status": status,
               "processed_by": processed_by, "points_reversal_ref": points_reversal_ref,
               })).mappings().one()
        await session.flush()
        return dict(row)

    async def set_purchase_status(self, session: AsyncSession, *, purchase_no: str, status: str
                                  ) -> dict[str, Any] | None:
        row = (await session.execute(text(f"""
            UPDATE mkt.points_purchase SET status = :s
             WHERE purchase_no = :no AND status = 'completed' RETURNING {PURCHASE_COLS}
        """), {"s": status, "no": purchase_no})).mappings().one_or_none()
        await session.flush()
        return dict(row) if row else None
