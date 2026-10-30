"""商城（配件电商）取数与写入：`commerce` 的 product / sku / stock / stock_unit / cart_item / order。

## 与 `purchase_repo` 的分工

订单骨架（`insert_order` / `insert_order_item` / `insert_fulfillment`）、收款线
（`channel_for` / `insert_pay_order` / `insert_voucher` / `set_pay_status`）**不在这里重写**：
积分包已经把那条链跑通了，商城直接复用 `PurchaseRepo` 的方法（本次给它们加了
`dealer_org_id` / `vin` / `pickup_code` / `delivered_now` 几个可选入参）。
本文件只放商城独有的东西：目录、双轨库存、购物车、订单查询、提货核销。

## 库存双轨的两条 SQL 纪律（`Task-Server.md` §7 铁律 7 的延伸）

1. **扣数量库存用条件 UPDATE，不用"先查后写"**：
   `UPDATE ... SET qty = qty - :q WHERE ... AND qty >= :q RETURNING qty`。
   并发下两条请求都读到 1 件、都判定够、都扣 → 库存 -1；条件 UPDATE 只有一条能拿到行。
2. **SN 台账用 `FOR UPDATE SKIP LOCKED` 挑件**：同一个 SKU 的两个人同时下单，
   各自锁到不同的实物行，不会互相等待，也不会把同一件卖给两个人。

## `stock_unit.sn` 唯一性

`uq_stock_unit_sn` 建在 `(sku_id, lower(sn))` 上（迁移 0013）：同一 SKU 下不允许两条同号，
但**不同 SKU 之间允许**（例如"电池 A 型"与"电池 B 型"各自台账里都有 SN-0001 这种厂内编号）。
全局唯一的是换电域的 `energy.battery`——那是运营商资产池，口径不同。
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from .purchase_repo import _strip_total

SKU_COLS = ("id, product_id, code, attrs, price, currency, inventory_kind, status")
UNIT_COLS = ("id, sku_id, sn, dealer_org_id, status, order_id, order_item_id, "
             "reserved_until, created_at")
ORDER_COLS = ('id, order_no, account_id, order_type, dealer_org_id, region_id, currency, '
              'amount_cash, amount_points, collect_mode, status, idempotency_key, '
              'paid_at, completed_at, cancel_reason, created_at, updated_at')


class ShopRepo:
    # ══ 目录（公开可看）═══════════════════════════════════════════════════════
    async def products_page(self, session: AsyncSession, *, region_id: str | None,
                            product_type: str | None = None, category: str | None = None,
                            q: str | None = None, limit: int = 20, offset: int = 0
                            ) -> tuple[list[dict[str, Any]], int]:
        """商品列表。价格取该商品下 active SKU 的最低值——列表页要先能显示"多少钱起"。"""
        where = ["p.status = 'active'"]
        p: dict[str, Any] = {"limit": limit, "offset": offset}
        if region_id:
            where.append("(p.region_id = :region OR p.region_id IS NULL)")
            p["region"] = region_id
        if product_type:
            where.append("p.product_type = :ptype")
            p["ptype"] = product_type
        if category:
            where.append("p.category = :cat")
            p["cat"] = category
        if q:
            where.append("p.name ILIKE :q")
            p["q"] = f"%{q}%"
        sql = f"""
            SELECT p.id, p.name, p.product_type, p.category, p.type_code, p.currency,
                   p.region_id, p.msrp, p.status, p.image_url, p.attrs,
                   COUNT(*) OVER() AS total,
                   (SELECT MIN(s.price) FROM commerce.sku s
                     WHERE s.product_id = p.id AND s.status = 'active') AS from_price,
                   (SELECT COUNT(*) FROM commerce.sku s
                     WHERE s.product_id = p.id AND s.status = 'active') AS sku_count
              FROM commerce.product p
             WHERE {' AND '.join(where)}
             ORDER BY p.id DESC
             LIMIT :limit OFFSET :offset
        """
        rows = (await session.execute(text(sql), p)).mappings().all()
        return _strip_total(rows)

    async def product_detail(self, session: AsyncSession, *, product_id: int
                             ) -> dict[str, Any] | None:
        row = (await session.execute(text("""
            SELECT id, name, product_type, category, type_code, manufacturer_org_id, msrp,
                   currency, region_id, status, created_at, image_url, attrs
              FROM commerce.product WHERE id = :id
        """), {"id": product_id})).mappings().one_or_none()
        return dict(row) if row else None

    async def skus_of_product(self, session: AsyncSession, product_id: int
                              ) -> list[dict[str, Any]]:
        """SKU + 每种库存模型的在手量（数量取 `stock.qty` 之和，SN 取 `in_stock` 行数）。"""
        rows = (await session.execute(text(f"""
            SELECT {SKU_COLS},
                   COALESCE((SELECT SUM(st.qty) FROM commerce.stock st
                              WHERE st.sku_id = s.id), 0) AS qty_available,
                   (SELECT COUNT(*) FROM commerce.stock_unit u
                     WHERE u.sku_id = s.id AND u.status = 'in_stock') AS unit_available
              FROM commerce.sku s
             WHERE s.product_id = :pid
             ORDER BY s.id
        """), {"pid": product_id})).mappings().all()
        return [dict(r) for r in rows]

    async def sku_by_code(self, session: AsyncSession, code: str) -> dict[str, Any] | None:
        row = (await session.execute(text(f"""
            SELECT {', '.join('s.' + c.strip() for c in SKU_COLS.split(','))},
                   p.name AS product_name, p.product_type, p.category, p.region_id,
                   p.manufacturer_org_id
              FROM commerce.sku s
              JOIN commerce.product p ON p.id = s.product_id
             WHERE s.code = :code
        """), {"code": code})).mappings().one_or_none()
        return dict(row) if row else None

    async def sku_by_id(self, session: AsyncSession, sku_id: int) -> dict[str, Any] | None:
        row = (await session.execute(text(f"""
            SELECT {', '.join('s.' + c.strip() for c in SKU_COLS.split(','))},
                   p.name AS product_name, p.product_type, p.category, p.region_id
              FROM commerce.sku s
              JOIN commerce.product p ON p.id = s.product_id
             WHERE s.id = :id
        """), {"id": sku_id})).mappings().one_or_none()
        return dict(row) if row else None

    async def dealers_with_stock(self, session: AsyncSession, *, sku_id: int,
                                 limit: int = 10) -> list[dict[str, Any]]:
        """哪些经销商有这个 SKU 的货（用户下单要选店，否则不知道去哪提货）。"""
        rows = (await session.execute(text("""
            SELECT o.id AS dealer_org_id, o.name, o.address, o.phone,
                   COALESCE(st.qty, 0) AS qty,
                   (SELECT COUNT(*) FROM commerce.stock_unit u
                     WHERE u.sku_id = :sku AND u.dealer_org_id = o.id
                       AND u.status = 'in_stock') AS units
              FROM core.org o
              LEFT JOIN commerce.stock st ON st.dealer_org_id = o.id AND st.sku_id = :sku
             WHERE o.org_type = 'dealer' AND o.status = 'active'
               AND (COALESCE(st.qty, 0) > 0
                    OR EXISTS (SELECT 1 FROM commerce.stock_unit u2
                                WHERE u2.sku_id = :sku AND u2.dealer_org_id = o.id
                                  AND u2.status = 'in_stock'))
             ORDER BY qty DESC, o.id
             LIMIT :limit
        """), {"sku": sku_id, "limit": limit})).mappings().all()
        return [dict(r) for r in rows]

    async def org_by_id(self, session: AsyncSession, org_id: int) -> dict[str, Any] | None:
        """卖方组织（下单要确认"这家店真的存在且在营业"，不然订单记在一个空组织上）。"""
        row = (await session.execute(text("""
            SELECT id, name, org_type, region_id, status, address, phone
              FROM core.org WHERE id = :id
        """), {"id": org_id})).mappings().one_or_none()
        return dict(row) if row else None

    async def order_vouchers(self, session: AsyncSession, order_id: int) -> list[dict[str, Any]]:
        """该订单名下所有转账凭证（核销界面要看到流水号与金额才敢点通过）。"""
        rows = (await session.execute(text("""
            SELECT v.id, v.pay_order_id, p.pay_no, v.transfer_no, v.amount, v.currency,
                   v.proof_url, v.uploaded_by, v.status, v.verified_by, v.verified_at,
                   v.created_at
              FROM fin.payment_voucher v
              JOIN fin.pay_order p ON p.id = v.pay_order_id
             WHERE p.order_id = :o
             ORDER BY v.id DESC
        """), {"o": order_id})).mappings().all()
        return [dict(r) for r in rows]

    # ══ 商家侧：商品 / SKU / 库存 ═════════════════════════════════════════════
    async def insert_product(self, session: AsyncSession, *, name: str, product_type: str,
                             category: str | None, currency: str, region_id: str | None,
                             msrp: int | None, manufacturer_org_id: int | None,
                             status: str = "active") -> dict[str, Any]:
        row = (await session.execute(text("""
            INSERT INTO commerce.product (name, product_type, category, currency, region_id,
                                         msrp, manufacturer_org_id, status)
            VALUES (:name, :ptype, :cat, :cur, :region, :msrp, :mfr, :status)
            RETURNING id, name, product_type, category, currency, region_id, status, created_at
        """), {"name": name, "ptype": product_type, "cat": category, "cur": currency,
               "region": region_id, "msrp": msrp, "mfr": manufacturer_org_id,
               "status": status})).mappings().one()
        await session.flush()
        return dict(row)

    async def insert_sku(self, session: AsyncSession, *, product_id: int, code: str,
                         attrs: dict[str, Any], price: int, currency: str,
                         inventory_kind: str, status: str = "active") -> dict[str, Any]:
        row = (await session.execute(text(f"""
            INSERT INTO commerce.sku (product_id, code, attrs, price, currency,
                                     inventory_kind, status)
            VALUES (:pid, :code, CAST(:attrs AS jsonb), :price, :cur, :kind, :status)
            RETURNING {SKU_COLS}
        """), {"pid": product_id, "code": code, "attrs": _json(attrs), "price": price,
               "cur": currency, "kind": inventory_kind, "status": status})).mappings().one()
        await session.flush()
        return dict(row)

    async def add_stock_qty(self, session: AsyncSession, *, sku_id: int, dealer_org_id: int,
                            delta: int) -> int:
        """数量库存增量（入库 +N / 扣减 -N）。库存在扣减时不允许变负（CHECK + 条件 UPDATE）。"""
        qty = (await session.execute(text("""
            INSERT INTO commerce.stock (sku_id, dealer_org_id, qty, updated_at)
            VALUES (:sku, :dealer, :delta, now())
            ON CONFLICT (sku_id, dealer_org_id)
            DO UPDATE SET qty = commerce.stock.qty + :delta, updated_at = now()
            RETURNING qty
        """), {"sku": sku_id, "dealer": dealer_org_id, "delta": delta})).scalar_one()
        await session.flush()
        return int(qty)

    async def lock_qty(self, session: AsyncSession, *, sku_id: int, dealer_org_id: int,
                       qty: int) -> int | None:
        """扣减数量库存（条件 UPDATE）。拿不到行 = 库存不足，返回 None。"""
        got = (await session.execute(text("""
            UPDATE commerce.stock
               SET qty = qty - :q, updated_at = now()
             WHERE sku_id = :sku AND dealer_org_id = :dealer AND qty >= :q
             RETURNING qty
        """), {"sku": sku_id, "dealer": dealer_org_id, "q": qty})).scalar_one_or_none()
        await session.flush()
        return int(got) if got is not None else None

    async def insert_stock_units(self, session: AsyncSession, *, sku_id: int, dealer_org_id: int,
                                 sns: list[str], note: str | None = None) -> list[str]:
        """SN 入库，返回**本次真正插入**的 SN（已存在的被跳过）。

        `ON CONFLICT (sku_id, sn) DO NOTHING` 让"重复导入同一批 SN"成为幂等操作，
        而不是让运维在第二次导入时看到 500。SN 的归一化（去空格、转大写）在 service 层做，
        索引就是普通 `(sku_id, sn)`——不用 `lower(sn)` 表达式索引，是因为
        `ON CONFLICT` 推断表达式索引比普通索引脆，而这个唯一性的目的是"别重复登记同一件货"，
        归一化后完全够。
        """
        if not sns:
            return []
        # **一条 INSERT ... SELECT unnest(...)**，不是 executemany：
        # SQLAlchemy 的 executemany 模式下结果集被关闭，`RETURNING` 取不到行
        # （实测 `ResourceClosedError: This result object does not return rows`），
        # 而"哪些 SN 真的插进去了"必须靠 RETURNING——否则前端看到的是"全部成功"。
        rows = (await session.execute(text("""
            INSERT INTO commerce.stock_unit (sku_id, sn, dealer_org_id, status, note)
            SELECT :sku, sn, :dealer, 'in_stock', :note
              FROM unnest(CAST(:sns AS text[])) AS sn
            ON CONFLICT (sku_id, sn) DO NOTHING
            RETURNING sn
        """), {"sku": sku_id, "dealer": dealer_org_id, "note": note,
               "sns": list(sns)})).scalars().all()
        await session.flush()
        return [str(r) for r in rows]

    async def reserve_units(self, session: AsyncSession, *, sku_id: int, dealer_org_id: int,
                            qty: int, order_id: int, order_item_id: int,
                            reserved_until: datetime) -> list[dict[str, Any]]:
        """挑 `qty` 件 `in_stock` 的实物并置 `reserved`（`SKIP LOCKED` 防并发互等）。"""
        rows = (await session.execute(text("""
            WITH picked AS (
                SELECT id FROM commerce.stock_unit
                 WHERE sku_id = :sku AND dealer_org_id = :dealer AND status = 'in_stock'
                 ORDER BY id
                 LIMIT :qty
                 FOR UPDATE SKIP LOCKED
            )
            UPDATE commerce.stock_unit u
               SET status = 'reserved', order_id = :order, order_item_id = :item,
                   reserved_until = :until, updated_at = now()
              FROM picked
             WHERE u.id = picked.id
            RETURNING u.id, u.sn
        """), {"sku": sku_id, "dealer": dealer_org_id, "qty": qty, "order": order_id,
               "item": order_item_id, "until": reserved_until})).mappings().all()
        await session.flush()
        return [dict(r) for r in rows]

    async def sell_units_of_order(self, session: AsyncSession, order_id: int) -> list[str]:
        rows = (await session.execute(text("""
            UPDATE commerce.stock_unit
               SET status = 'sold', reserved_until = NULL, updated_at = now()
             WHERE order_id = :o AND status = 'reserved'
            RETURNING sn
        """), {"o": order_id})).scalars().all()
        await session.flush()
        return [str(r) for r in rows]

    async def release_units_of_order(self, session: AsyncSession, order_id: int) -> int:
        """取消/超时：把该单占着的 SN 放回在库（只动 `reserved`，`sold` 不碰）。"""
        ids = (await session.execute(text("""
            UPDATE commerce.stock_unit
               SET status = 'in_stock', order_id = NULL, order_item_id = NULL,
                   reserved_until = NULL, updated_at = now()
             WHERE order_id = :o AND status = 'reserved'
            RETURNING id
        """), {"o": order_id})).scalars().all()
        await session.flush()
        return len(ids)

    async def release_expired_units(self, session: AsyncSession, *, limit: int = 500) -> int:
        """worker 定时任务：支付超时的预留放回在库。

        用 `RETURNING id` 数行数，不用 `Result.rowcount`——SQLAlchemy 的 `Result` 上没有这个属性
        （`core/deps.py:134` 记着同一条坑），`mypy` 也会直接报 attr-defined。
        """
        ids = (await session.execute(text("""
            UPDATE commerce.stock_unit
               SET status = 'in_stock', order_id = NULL, order_item_id = NULL,
                   reserved_until = NULL, updated_at = now()
             WHERE id IN (
                 SELECT id FROM commerce.stock_unit
                  WHERE status = 'reserved' AND reserved_until IS NOT NULL
                    AND reserved_until < now()
                  LIMIT :limit
             )
            RETURNING id
        """), {"limit": limit})).scalars().all()
        await session.flush()
        return len(ids)

    async def units_page(self, session: AsyncSession, *, sku_id: int | None = None,
                         dealer_org_id: int | None = None, status: str | None = None,
                         limit: int = 50, offset: int = 0
                         ) -> tuple[list[dict[str, Any]], int]:
        where = ["1=1"]
        p: dict[str, Any] = {"limit": limit, "offset": offset}
        if sku_id is not None:
            where.append("u.sku_id = :sku")
            p["sku"] = sku_id
        if dealer_org_id is not None:
            where.append("u.dealer_org_id = :dealer")
            p["dealer"] = dealer_org_id
        if status:
            where.append("u.status = :st")
            p["st"] = status
        rows = (await session.execute(text(f"""
            SELECT u.id, u.sku_id, s.code AS sku_code, p.name AS product_name, u.sn,
                   u.dealer_org_id, o.name AS dealer_name, u.status, u.order_id,
                   u.reserved_until, u.created_at, COUNT(*) OVER() AS total
              FROM commerce.stock_unit u
              JOIN commerce.sku s ON s.id = u.sku_id
              JOIN commerce.product p ON p.id = s.product_id
              LEFT JOIN core.org o ON o.id = u.dealer_org_id
             WHERE {' AND '.join(where)}
             ORDER BY u.id DESC
             LIMIT :limit OFFSET :offset
        """), p)).mappings().all()
        return _strip_total(rows)

    async def stock_page(self, session: AsyncSession, *, dealer_org_id: int | None,
                         limit: int = 50, offset: int = 0
                         ) -> tuple[list[dict[str, Any]], int]:
        where = ["1=1"]
        p: dict[str, Any] = {"limit": limit, "offset": offset}
        if dealer_org_id is not None:
            where.append("st.dealer_org_id = :dealer")
            p["dealer"] = dealer_org_id
        rows = (await session.execute(text(f"""
            SELECT st.id, st.sku_id, s.code AS sku_code, s.inventory_kind, s.price,
                   s.currency, p.name AS product_name, p.product_type,
                   st.dealer_org_id, o.name AS dealer_name, st.qty, st.updated_at,
                   COUNT(*) OVER() AS total
              FROM commerce.stock st
              JOIN commerce.sku s ON s.id = st.sku_id
              JOIN commerce.product p ON p.id = s.product_id
              LEFT JOIN core.org o ON o.id = st.dealer_org_id
             WHERE {' AND '.join(where)}
             ORDER BY st.updated_at DESC, st.id DESC
             LIMIT :limit OFFSET :offset
        """), p)).mappings().all()
        return _strip_total(rows)

    # ══ 购物车 ═══════════════════════════════════════════════════════════════
    async def cart_rows(self, session: AsyncSession, account_id: int) -> list[dict[str, Any]]:
        rows = (await session.execute(text("""
            SELECT ci.id, ci.sku_id, ci.qty, s.code AS sku_code, s.price, s.currency,
                   s.inventory_kind, s.status AS sku_status,
                   p.id AS product_id, p.name AS product_name, p.product_type, p.region_id,
                   COALESCE((SELECT SUM(st.qty) FROM commerce.stock st
                              WHERE st.sku_id = s.id), 0) AS qty_available,
                   (SELECT COUNT(*) FROM commerce.stock_unit u
                     WHERE u.sku_id = s.id AND u.status = 'in_stock') AS unit_available
              FROM commerce.cart_item ci
              JOIN commerce.sku s ON s.id = ci.sku_id
              JOIN commerce.product p ON p.id = s.product_id
             WHERE ci.account_id = :acc
             ORDER BY ci.id
        """), {"acc": account_id})).mappings().all()
        return [dict(r) for r in rows]

    async def cart_upsert(self, session: AsyncSession, *, account_id: int, sku_id: int,
                          qty: int) -> None:
        """`qty <= 0` 视为移除（前端"减到 0"不应该留下一条 qty=0 的僵尸行）。"""
        if qty <= 0:
            await session.execute(text("""
                DELETE FROM commerce.cart_item WHERE account_id = :acc AND sku_id = :sku
            """), {"acc": account_id, "sku": sku_id})
        else:
            await session.execute(text("""
                INSERT INTO commerce.cart_item (account_id, sku_id, qty)
                VALUES (:acc, :sku, :qty)
                ON CONFLICT (account_id, sku_id) DO UPDATE SET qty = :qty
            """), {"acc": account_id, "sku": sku_id, "qty": qty})
        await session.flush()

    async def cart_clear(self, session: AsyncSession, account_id: int,
                         sku_ids: list[int] | None = None) -> int:
        if sku_ids:
            ids = (await session.execute(text("""
                DELETE FROM commerce.cart_item
                 WHERE account_id = :acc AND sku_id = ANY(:ids)
                RETURNING id
            """), {"acc": account_id, "ids": sku_ids})).scalars().all()
        else:
            ids = (await session.execute(text("""
                DELETE FROM commerce.cart_item WHERE account_id = :acc RETURNING id
            """), {"acc": account_id})).scalars().all()
        await session.flush()
        return len(ids)

    # ══ 订单 ═════════════════════════════════════════════════════════════════
    async def order_by_no(self, session: AsyncSession, order_no: str) -> dict[str, Any] | None:
        """单据行 + 门店名。

        门店名用**标量子查询**取而不是 JOIN：`ORDER_COLS` 是无限定名的列（`id`/`status`/`created_at`…），
        一旦 JOIN `core.org` 这些列全部歧义，去改 ORDER_COLS 又会波及所有其它查询。
        详情页要显示"去哪家店提货"，之前只有列表接口带这个名字，详情页是 `—`。
        """
        row = (await session.execute(text(f"""
            SELECT {ORDER_COLS},
                   (SELECT og.name FROM core.org og
                     WHERE og.id = commerce."order".dealer_org_id) AS dealer_name
              FROM commerce."order" WHERE order_no = :no
        """), {"no": order_no})).mappings().one_or_none()
        return dict(row) if row else None

    async def order_by_id(self, session: AsyncSession, order_id: int) -> dict[str, Any] | None:
        row = (await session.execute(text(f"""
            SELECT {ORDER_COLS} FROM commerce."order" WHERE id = :id
        """), {"id": order_id})).mappings().one_or_none()
        return dict(row) if row else None

    async def lock_order(self, session: AsyncSession, order_id: int) -> dict[str, Any] | None:
        """锁单据行：核销/取消要串行，否则同一笔能被两个人同时核销。"""
        row = (await session.execute(text(f"""
            SELECT {ORDER_COLS} FROM commerce."order" WHERE id = :id FOR UPDATE
        """), {"id": order_id})).mappings().one_or_none()
        return dict(row) if row else None

    async def order_items(self, session: AsyncSession, order_id: int) -> list[dict[str, Any]]:
        rows = (await session.execute(text("""
            SELECT oi.id, oi.sku_id, s.code AS sku_code, s.inventory_kind,
                   p.name AS product_name, oi.product_type, oi.vin, oi.qty, oi.unit_price,
                   oi.points_cost
              FROM commerce.order_item oi
              JOIN commerce.sku s ON s.id = oi.sku_id
              JOIN commerce.product p ON p.id = s.product_id
             WHERE oi.order_id = :o
             ORDER BY oi.id
        """), {"o": order_id})).mappings().all()
        return [dict(r) for r in rows]

    async def order_units(self, session: AsyncSession, order_id: int) -> list[dict[str, Any]]:
        rows = (await session.execute(text(f"""
            SELECT {UNIT_COLS} FROM commerce.stock_unit WHERE order_id = :o ORDER BY id
        """), {"o": order_id})).mappings().all()
        return [dict(r) for r in rows]

    async def fulfillments_of_order(self, session: AsyncSession, order_id: int
                                    ) -> list[dict[str, Any]]:
        rows = (await session.execute(text("""
            SELECT id, order_id, type, dealer_org_id, pickup_code, status, delivered_at,
                   confirmed_by
              FROM commerce.fulfillment WHERE order_id = :o ORDER BY id
        """), {"o": order_id})).mappings().all()
        return [dict(r) for r in rows]

    async def orders_page(self, session: AsyncSession, *, account_id: int | None = None,
                          dealer_org_id: int | None = None, status: str | None = None,
                          order_types: tuple[str, ...] = ("part", "service"),
                          limit: int = 20, offset: int = 0
                          ) -> tuple[list[dict[str, Any]], int]:
        where = ["o.order_type = ANY(:types)"]
        p: dict[str, Any] = {"types": list(order_types), "limit": limit, "offset": offset}
        if account_id is not None:
            where.append("o.account_id = :acc")
            p["acc"] = account_id
        if dealer_org_id is not None:
            where.append("o.dealer_org_id = :dealer")
            p["dealer"] = dealer_org_id
        if status:
            where.append("o.status = :st")
            p["st"] = status
        rows = (await session.execute(text(f"""
            SELECT {', '.join('o.' + c.strip() for c in ORDER_COLS.split(','))},
                   d.name AS dealer_name, COUNT(*) OVER() AS total,
                   (SELECT COUNT(*) FROM commerce.order_item i WHERE i.order_id = o.id) AS item_count
              FROM commerce."order" o
              LEFT JOIN core.org d ON d.id = o.dealer_org_id
             WHERE {' AND '.join(where)}
             ORDER BY o.id DESC
             LIMIT :limit OFFSET :offset
        """), p)).mappings().all()
        return _strip_total(rows)

    async def mark_paid(self, session: AsyncSession, order_id: int) -> dict[str, Any] | None:
        row = (await session.execute(text(f"""
            UPDATE commerce."order"
               SET status = 'paid', paid_at = COALESCE(paid_at, now()), updated_at = now()
             WHERE id = :id RETURNING {ORDER_COLS}
        """), {"id": order_id})).mappings().one_or_none()
        await session.flush()
        return dict(row) if row else None

    async def mark_completed(self, session: AsyncSession, order_id: int
                             ) -> dict[str, Any] | None:
        row = (await session.execute(text(f"""
            UPDATE commerce."order"
               SET status = 'completed', completed_at = COALESCE(completed_at, now()),
                   updated_at = now()
             WHERE id = :id RETURNING {ORDER_COLS}
        """), {"id": order_id})).mappings().one_or_none()
        await session.flush()
        return dict(row) if row else None

    async def mark_cancelled(self, session: AsyncSession, order_id: int, reason: str
                             ) -> dict[str, Any] | None:
        row = (await session.execute(text(f"""
            UPDATE commerce."order"
               SET status = 'cancelled', cancel_reason = :reason, updated_at = now()
             WHERE id = :id RETURNING {ORDER_COLS}
        """), {"id": order_id, "reason": reason})).mappings().one_or_none()
        await session.flush()
        return dict(row) if row else None

    async def expired_unpaid(self, session: AsyncSession, *, limit: int = 200
                             ) -> list[dict[str, Any]]:
        """还是 `created`、且支付单已过期的商城单（worker 每 5 分钟扫一次）。"""
        rows = (await session.execute(text(f"""
            SELECT {', '.join('o.' + c.strip() for c in ORDER_COLS.split(','))}
              FROM commerce."order" o
              JOIN fin.pay_order p ON p.order_id = o.id
             WHERE o.status = 'created' AND o.order_type = ANY(:types)
               AND p.expires_at IS NOT NULL AND p.expires_at < now()
             ORDER BY o.id
             LIMIT :limit
        """), {"types": ["part", "service"], "limit": limit})).mappings().all()
        return [dict(r) for r in rows]

    # ══ 履约 ═════════════════════════════════════════════════════════════════
    async def fulfillment_by_pickup(self, session: AsyncSession, pickup_code: str
                                    ) -> dict[str, Any] | None:
        row = (await session.execute(text("""
            SELECT f.id, f.order_id, f.type, f.dealer_org_id, f.pickup_code, f.status,
                   f.delivered_at, f.confirmed_by, o.order_no, o.account_id, o.status AS order_status
              FROM commerce.fulfillment f
              JOIN commerce."order" o ON o.id = f.order_id
             WHERE f.pickup_code = :code
        """), {"code": pickup_code})).mappings().one_or_none()
        return dict(row) if row else None

    async def confirm_fulfillment(self, session: AsyncSession, *, fulfillment_id: int,
                                  account_id: int) -> bool:
        fid = (await session.execute(text("""
            UPDATE commerce.fulfillment
               SET status = 'delivered', delivered_at = now(), confirmed_by = :acc
             WHERE id = :id AND status <> 'delivered'
            RETURNING id
        """), {"id": fulfillment_id, "acc": account_id})).scalar_one_or_none()
        await session.flush()
        return fid is not None

    async def sales_summary(self, session: AsyncSession, *, dealer_org_ids: list[int] | None = None
                            ) -> dict[str, Any]:
        """商家看板：已收金额 / 待收款单数 / 待提货单数。**不猜口径**——
        金额只统计 `paid`/`completed`（`created` 是没付钱的单，算进去就是虚增营收）。

        **口径必须由调用方给死**（`dealer_org_ids=None` = 全平台，只有平台/监管能走到）：
        这里原来是单个 `dealer_org_id`，而 service 对非平台身份直接透传 `None`，
        于是任意登录用户（consumer 也有 `order:read`）都能读到**全站 GMV**（2026-10-30 实测）。
        改成显式列表后，多店员工也不必被硬塞成单店口径。

        `CAST(:orgs AS bigint[])` 的显式 cast 是必须的：`text()` 绑定不带列类型，
        写成 `(:orgs IS NULL OR ...)` asyncpg 会报 "could not determine data type"，
        而且这种错只有连真库才会暴露（`Task-Server.md` §7 铁律 7）。
        """
        row = (await session.execute(text("""
            SELECT COUNT(*) FILTER (WHERE o.status = 'created') AS unpaid_orders,
                   COUNT(*) FILTER (WHERE o.status = 'paid') AS paid_orders,
                   COUNT(*) FILTER (WHERE o.status = 'completed') AS completed_orders,
                   COALESCE(SUM(o.amount_cash) FILTER (
                       WHERE o.status IN ('paid','completed')), 0)::bigint AS revenue_minor,
                   COALESCE(SUM(o.amount_cash) FILTER (
                       WHERE o.status = 'created'), 0)::bigint AS pending_minor
              FROM commerce."order" o
             WHERE o.order_type = ANY(:types)
               AND (CAST(:orgs AS bigint[]) IS NULL
                    OR o.dealer_org_id = ANY(CAST(:orgs AS bigint[])))
        """), {"types": ["part", "service"],
               "orgs": dealer_org_ids})).mappings().one()
        return dict(row)


def _json(v: Any) -> str:
    """jsonb 入参必须自己序列化：`text()` 绑定不带列类型，asyncpg 收到 dict/list 直接报错。"""
    return json.dumps(v, ensure_ascii=False)
