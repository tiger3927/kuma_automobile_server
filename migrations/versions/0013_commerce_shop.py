"""商城（配件电商）：SKU 库存双轨 + 订单支付/完成时间

## 为什么库存要双轨（`sku.inventory_kind`）

`004_commerce.sql` 的 `commerce.stock` 只有 `qty`——那是**数量库存**，适合螺丝、坐垫、
充电器这类"同质、可互换、不关心是哪一件"的东西。但电池、控制器、仪表这类贵重件
必须能追到"具体哪一件去了哪台车"，否则质保期、召回、二手残值全都无从谈起
（与换电域 `energy.battery` 同一个理由，见 `Task-Plan.md` §5.1 自洽要点 3）。

两种库存不能混在一张表里：数量库存在 `stock.qty` 上做加减，SN 库存是"每一行一个实物"，
把 SN 塞进 `stock.attrs` 就等于放弃逐个查询与唯一性约束。
所以按 **SKU 声明**（而不是按商品）：同一个商品的"电池版"和"普通版"本就是不同 SKU，
库存模型跟着 SKU 走才不会被"换个规格就换不了库存模型"卡住。

- `qty` 类：`commerce.stock.qty` 增减（现有表，不动语义）
- `serial` 类：`commerce.stock_unit` 每个 SN 一行，状态机
  `in_stock → reserved → sold`（另有 `returned`/`scrapped` 兜售后与报废）

## 为什么预留挂在 `stock_unit` 上而不是另建预留表

下单到收款核销之间的窗口很短（支付单 TTL 30 分钟），预留只有"谁占的、占多久"两件事。
另建 `stock_reservation` 表会多一次 join 和一次清理任务，而 `stock_unit` 本来就一行一件，
`order_id + reserved_until` 两个字段足够；超时释放就是 `UPDATE ... WHERE reserved_until < now()`。

## 订单补三个时间字段

`004` 的 `commerce."order"` 只有 `created_at/updated_at`。商城订单要看
"什么时候付的钱"（对账/账期）与"什么时候完成交付"（售后起算日），
以及取消原因（用户取消 vs 超时自动取消 vs 商家缺货），所以补 `paid_at` / `completed_at` / `cancel_reason`。

## 授权

`010_grants_appendonly.sql` 已给 `commerce` 设了 `ALTER DEFAULT PRIVILEGES ... TO kuma_app`，
本迁移由 `kuma_owner` 跑 ⇒ 新表与新序列自动可用（与 0012 同一结论）。

Revision ID: 0013_commerce_shop
Revises: 0012_commerce_lead
"""
from __future__ import annotations

from alembic import op

revision = "0013_commerce_shop"
down_revision = "0012_commerce_lead"
branch_labels = None
depends_on = None

#: 与 `models/commerce.py` 的 `Sku.inventory_kind` 注释、`services/shop_service.py` 逐字一致
INVENTORY_KINDS = ("qty", "serial")


def _in_check(col: str, values: tuple[str, ...]) -> str:
    vals = ", ".join(f"'{v}'" for v in values)
    return f"CHECK ({col} IN ({vals}))"


def upgrade() -> None:
    # ① SKU 声明库存模型
    op.execute("ALTER TABLE commerce.sku ADD COLUMN IF NOT EXISTS inventory_kind text "
               "NOT NULL DEFAULT 'qty'")
    op.execute(f"ALTER TABLE commerce.sku ADD CONSTRAINT sku_inventory_kind_check "
               f"{_in_check('inventory_kind', INVENTORY_KINDS)}")

    # ② SN 级台账
    op.execute("""
        CREATE TABLE IF NOT EXISTS commerce.stock_unit (
            id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            sku_id bigint NOT NULL REFERENCES commerce.sku,
            sn text NOT NULL,
            dealer_org_id bigint NOT NULL REFERENCES core.org,
            status text NOT NULL DEFAULT 'in_stock',
            order_id bigint REFERENCES commerce."order",
            order_item_id bigint REFERENCES commerce.order_item,
            reserved_until timestamptz,
            note text,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT stock_unit_status_check CHECK
                (status IN ('in_stock','reserved','sold','returned','scrapped'))
        )
    """)
    # 同一 SKU 下 SN 唯一（换电域 `energy.battery` 是全局唯一，因为那是运营商资产池；
    # 这里是商家库存，不同 SKU 之间有同名厂内编号是常态）。
    # 不用 `lower(sn)` 表达式索引：`ON CONFLICT` 对表达式索引的推断比普通索引脆，
    # 而归一化（去空格/转大写）放在 service 层做一次就够。
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_stock_unit_sn "
               "ON commerce.stock_unit (sku_id, sn)")
    # 商家看"我有什么可卖"、用户看"这店有没有货"
    op.execute("CREATE INDEX IF NOT EXISTS idx_stock_unit_avail "
               "ON commerce.stock_unit (sku_id, dealer_org_id, status)")
    # 超时释放扫的索引（只要 reserved 的那几行，别全表扫）
    op.execute("CREATE INDEX IF NOT EXISTS idx_stock_unit_reserved "
               "ON commerce.stock_unit (reserved_until) WHERE status = 'reserved'")
    op.execute("CREATE INDEX IF NOT EXISTS idx_stock_unit_order "
               "ON commerce.stock_unit (order_id) WHERE order_id IS NOT NULL")

    # ③ 数量库存补 updated_at（admin 库存页要显示"上次变动"，没有它只能猜）
    op.execute("ALTER TABLE commerce.stock "
               "ADD COLUMN IF NOT EXISTS updated_at timestamptz NOT NULL DEFAULT now()")

    # ④ 订单补时间与取消原因
    op.execute("ALTER TABLE commerce.\"order\" ADD COLUMN IF NOT EXISTS paid_at timestamptz")
    op.execute("ALTER TABLE commerce.\"order\" "
               "ADD COLUMN IF NOT EXISTS completed_at timestamptz")
    op.execute("ALTER TABLE commerce.\"order\" ADD COLUMN IF NOT EXISTS cancel_reason text")
    # 超时取消要扫"还没付款的旧单"
    op.execute("CREATE INDEX IF NOT EXISTS idx_order_payable "
               "ON commerce.\"order\" (created_at) WHERE status IN ('created','paid')")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_order_payable")
    op.execute("ALTER TABLE commerce.\"order\" DROP COLUMN IF EXISTS cancel_reason")
    op.execute("ALTER TABLE commerce.\"order\" DROP COLUMN IF EXISTS completed_at")
    op.execute("ALTER TABLE commerce.\"order\" DROP COLUMN IF EXISTS paid_at")
    op.execute("ALTER TABLE commerce.stock DROP COLUMN IF EXISTS updated_at")
    op.execute("DROP INDEX IF EXISTS idx_stock_unit_order")
    op.execute("DROP INDEX IF EXISTS idx_stock_unit_reserved")
    op.execute("DROP INDEX IF EXISTS idx_stock_unit_avail")
    op.execute("DROP INDEX IF EXISTS uq_stock_unit_sn")
    op.execute("DROP TABLE IF EXISTS commerce.stock_unit")
    op.execute("ALTER TABLE commerce.sku DROP CONSTRAINT IF EXISTS sku_inventory_kind_check")
    op.execute("ALTER TABLE commerce.sku DROP COLUMN IF EXISTS inventory_kind")
