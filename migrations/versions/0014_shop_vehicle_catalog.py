"""商城整车目录：`commerce.product` 加 `image_url` / `attrs`，车型字典放宽 `product_line`

## 为什么商品表要加这两列

整车与配件在商城里的差别不是"类型字段"，而是**这个商品有没有 SKU**：

- 配件：`commerce.sku`（价格/库存/SN）→ 能直接下单；
- 整车：只有**指导价**（`product.msrp`）与**参数表**，成交靠询价（`commerce.lead`），
  所以它没有 SKU，也就没地方放"车型图 + 参数 + 归属产品线"。

`attrs` 存营销属性（`{"line": "passenger", "model": "KUMA 310", "specs": [...]}`），
`image_url` 存站内相对路径（`/vehicles/kuma310.jpg`）。这两样都是**商品**概念，不塞进
`core.vehicle_type`——那是车联网/合规的车型字典（VIN 上报、协议档），一个车型可以对应
多个年款/配置商品，两者不是一张表。

## 为什么放宽 `product_line`

`core.vehicle_type.product_line` 原来只允许 `tricycle|minibus|sedan|pickup`，
而实际产品线还有**环卫车**（保洁车、侧翻桶清运车、高压冲洗车、纯吸式清扫车）与
**无人作业车**（CR-1 扫地车）。加值不改既有行，向后兼容；不放宽的话这 5 个车型
进不了字典，询价的 `intent_type_code` 就挂不上（该列是 FK）。

## 数据不在这里 seed

12 个车型的行由 `scripts/seed_vehicle_catalog.py` 幂等写入（可重复跑、可改价），
迁移只管结构——把价格与参数写死在迁移里，改一次价就要一个新迁移。

Revision ID: 0014_shop_vehicle_catalog
Revises: 0013_commerce_shop
"""
from __future__ import annotations

from alembic import op

revision = "0014_shop_vehicle_catalog"
down_revision = "0013_commerce_shop"
branch_labels = None
depends_on = None

#: 与 `migrations/sql/002_core.sql` 的原 CHECK 相比只增加了两条产品线
PRODUCT_LINES = ("tricycle", "minibus", "sedan", "pickup", "sanitation", "robot")


def upgrade() -> None:
    op.execute("ALTER TABLE commerce.product ADD COLUMN IF NOT EXISTS image_url text")
    op.execute("ALTER TABLE commerce.product ADD COLUMN IF NOT EXISTS attrs jsonb "
               "NOT NULL DEFAULT '{}'")

    # 车型字典放宽产品线（列是 NOT NULL 的 text + CHECK，不是 PG enum，改 CHECK 即可）
    op.execute("ALTER TABLE core.vehicle_type "
               "DROP CONSTRAINT IF EXISTS vehicle_type_product_line_check")
    vals = ", ".join(f"'{v}'" for v in PRODUCT_LINES)
    op.execute(f"ALTER TABLE core.vehicle_type ADD CONSTRAINT vehicle_type_product_line_check "
               f"CHECK (product_line IN ({vals}))")

    # 目录按"在售 + 类型"翻页（idx_product_region_type 的第一列是 region，翻不动的场景在这）
    op.execute("CREATE INDEX IF NOT EXISTS idx_product_type_status "
               "ON commerce.product (product_type, status, id DESC)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS commerce.idx_product_type_status")
    op.execute("ALTER TABLE core.vehicle_type "
               "DROP CONSTRAINT IF EXISTS vehicle_type_product_line_check")
    op.execute("ALTER TABLE core.vehicle_type ADD CONSTRAINT vehicle_type_product_line_check "
               "CHECK (product_line IN ('tricycle','minibus','sedan','pickup'))")
    op.execute("ALTER TABLE commerce.product DROP COLUMN IF EXISTS attrs")
    op.execute("ALTER TABLE commerce.product DROP COLUMN IF EXISTS image_url")
