"""返积分 + 积分包购买链路的库侧补齐（`Task-Plan.md` §7.1 / §7.2 / §5.1 / §7.5）

抵扣已上线（0008），本轮补两件事的库侧缺口。**不新建积分表**——`points_rule` /
`points_pack` / `points_purchase` 早在 `006_mkt.sql` 就有，缺的是"能不能用"的列。

## 1. `mkt.points_rule`（返积分规则）——规则本来就该在表里，本轮补齐它的可用性

- **`currency char(3)`**：`ratio` 是"**每 1 最小货币单位**返多少积分"。没有币种列，
  一条 `region_id IS NULL` 的兜底规则会同时套到 THB/ZAR/NGN 上——
  三种货币的最小单位价值差一个数量级，返积分要么少 100 倍要么多 100 倍
  （抵扣规则 0008 带 `currency` 是同一理由）。
- **`validity_months smallint DEFAULT 12`**：§7.1"`earned` 赠送积分 **12 个月**过期"。
  写死在代码里 = 改口径要发版；规则本身是版本化的，过期月数属于规则的一部分
  （同一 rule_code 不同 version 可以给不同期限）。
- **`proposed_by bigint`**：§9.5 积分发放规则**双人授权**。0006 只有 `approved_by`，
  没有"谁提的"，两个人签字就无从证明（0008 的抵扣规则表已有这一列，这里对齐）。
- **`status` CHECK**：现列无取值域，`draft/pending_review/active/retired` 全靠代码自觉；
  与 `swap_price` / `deposit_rule` / `points_offset_rule` 同一模式。
- **唯一键加 `currency`**：现库里的约束是 `uq_points_rule UNIQUE NULLS NOT DISTINCT
  (rule_code, region_id, version)`——`NULLS NOT DISTINCT` 已经有了（兜底行不会被重复插），
  但**键里没有币种**：加了 `currency` 列之后，"同一 rule_code 同一版本、THB 一行 ZAR 一行"
  会被旧约束判为重复而插不进来。多币种必须换键（§11.0-6）。
- **`idx_points_rule_active` 部分索引**：worker 每笔换电完成都要查一次生效规则
  （`source_type + region + currency + status='active'` + 日期窗），没索引就是全表扫。

## 2. `mkt.points_pack.sku_id`——积分包是**商品**，不是"充值接口"

§5.1 自洽要点 2 写死了：现金买积分必须走同一订单骨架、同一支付核销、同一退款规则、同一发票口径。
而 `commerce.order_item.sku_id` 是 NOT NULL → **没有 sku 就开不出订单**，开不出订单就没有
`points_purchase.order_id`（NOT NULL FK）。所以平台配置积分包时必须同步落
`commerce.product(product_type='points_pack')` + `commerce.sku`，`sku_id` 就是那条链的锚点。
（另一条路是把 `order_item.sku_id` 改可空，那等于给"绕过订单骨架的充值旁路"开门，正是 §5.1 要避免的事。）

## 3. `mkt.points_purchase` 补 `account_id` / `paid_at` + 状态 CHECK

- `account_id`：查"我的购买记录"和日/月购买限额都要按账号聚合；现表只有 `order_id`，
  每次聚合都得 join `commerce."order"`（限额校验在下单热路径上）。
- `paid_at`：§7.2"支付成功后**即时入账**"——入账时刻与 `created_at` 不是一回事（订单可以先建后付）。
- `status` CHECK（`completed|refunded|cancelled`）：退款按原路（§7.4）要把"已退"记成事实，
  没有取值域就会出现 `refunded`/`refund`/`已退` 三种写法并存。
- 索引：原 `idx_points_purchase_account` 名字骗人——它建在 **`order_id`** 上。改名
  `idx_points_purchase_order`，另建真正的 `(account_id, created_at DESC)`。

**没有 seed**：返积分比例、包规格与价格都是钱和营销口径，由平台在管理端配置（双人授权，§9.5）。
**地区开关也不改**：`core.region.features.points_pack_purchase` 三国 seed 全是 `false`
（§6.4：积分包具单用途预付卡性质，可能需备案）——开关由业务/合规决定，迁移不替业务打开。

Revision ID: 0009_points_award_pack
Revises: 0008_points_offset
"""
from __future__ import annotations

from alembic import op

revision = "0009_points_award_pack"
down_revision = "0008_points_offset"
branch_labels = None
depends_on = None

RULE_STATUS = ("draft", "pending_review", "active", "retired")
PACK_STATUS = ("draft", "active", "retired")
PURCHASE_STATUS = ("completed", "refunded", "cancelled")


def _check(col: str, values: tuple[str, ...]) -> str:
    vals = ", ".join(f"'{v}'" for v in values)
    return f"CHECK ({col} IN ({vals}))"


def upgrade() -> None:
    # ══ 1. 返积分规则可用化 ═══════════════════════════════════════════════════
    op.execute("""
        ALTER TABLE mkt.points_rule
          ADD COLUMN IF NOT EXISTS currency char(3),
          ADD COLUMN IF NOT EXISTS validity_months smallint NOT NULL DEFAULT 12
            CHECK (validity_months > 0),
          ADD COLUMN IF NOT EXISTS proposed_by bigint REFERENCES core.account
    """)
    # 唯一键要加 currency。实测现库的约束名是 `uq_points_rule`（不是 PG 默认名），
    # 两个名字都 IF EXISTS 一遍：约束名不存在会让整个迁移事务回滚（第一次跑就是这么挂的）。
    op.execute("ALTER TABLE mkt.points_rule "
               "DROP CONSTRAINT IF EXISTS points_rule_rule_code_region_id_version_key")
    op.execute("ALTER TABLE mkt.points_rule DROP CONSTRAINT IF EXISTS uq_points_rule")
    op.execute("""
        ALTER TABLE mkt.points_rule
          ADD CONSTRAINT uq_points_rule_version
          UNIQUE NULLS NOT DISTINCT (rule_code, region_id, currency, version)
    """)
    op.execute(f"ALTER TABLE mkt.points_rule ADD CONSTRAINT points_rule_status_check "
               f"{_check('status', RULE_STATUS)}")
    op.execute("""
        CREATE INDEX idx_points_rule_active ON mkt.points_rule
          (source_type, region_id, currency, effective_from DESC)
          WHERE status = 'active'
    """)

    # ══ 2. 积分包 → 商品骨架的锚点 ════════════════════════════════════════════
    op.execute("ALTER TABLE mkt.points_pack ADD COLUMN IF NOT EXISTS sku_id bigint "
               "REFERENCES commerce.sku")
    op.execute(f"ALTER TABLE mkt.points_pack ADD CONSTRAINT points_pack_status_check "
               f"{_check('status', PACK_STATUS)}")

    # ══ 3. 购买记录：按账号聚合 + 入账时刻 + 状态域 ═══════════════════════════
    op.execute("""
        ALTER TABLE mkt.points_purchase
          ADD COLUMN IF NOT EXISTS account_id bigint REFERENCES core.account,
          ADD COLUMN IF NOT EXISTS paid_at timestamptz
    """)
    op.execute(f"ALTER TABLE mkt.points_purchase ADD CONSTRAINT points_purchase_status_check "
               f"{_check('status', PURCHASE_STATUS)}")
    op.execute("DROP INDEX IF EXISTS mkt.idx_points_purchase_account")
    op.execute("CREATE INDEX idx_points_purchase_order ON mkt.points_purchase (order_id)")
    op.execute("CREATE INDEX idx_points_purchase_account "
               "ON mkt.points_purchase (account_id, created_at DESC)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS mkt.idx_points_purchase_account")
    op.execute("DROP INDEX IF EXISTS mkt.idx_points_purchase_order")
    op.execute("CREATE INDEX idx_points_purchase_account ON mkt.points_purchase (order_id)")
    op.execute("ALTER TABLE mkt.points_purchase DROP CONSTRAINT IF EXISTS points_purchase_status_check")
    op.execute("ALTER TABLE mkt.points_purchase DROP COLUMN IF EXISTS paid_at, "
               "DROP COLUMN IF EXISTS account_id")

    op.execute("ALTER TABLE mkt.points_pack DROP CONSTRAINT IF EXISTS points_pack_status_check")
    op.execute("ALTER TABLE mkt.points_pack DROP COLUMN IF EXISTS sku_id")

    op.execute("DROP INDEX IF EXISTS mkt.idx_points_rule_active")
    op.execute("ALTER TABLE mkt.points_rule DROP CONSTRAINT IF EXISTS points_rule_status_check")
    op.execute("ALTER TABLE mkt.points_rule DROP CONSTRAINT IF EXISTS uq_points_rule_version")
    # 恢复成实测的原样（名字 + NULLS NOT DISTINCT），否则 downgrade 之后再 upgrade 会撞键名
    op.execute("""
        ALTER TABLE mkt.points_rule
          ADD CONSTRAINT uq_points_rule UNIQUE NULLS NOT DISTINCT (rule_code, region_id, version)
    """)
    op.execute("ALTER TABLE mkt.points_rule DROP COLUMN IF EXISTS proposed_by, "
               "DROP COLUMN IF EXISTS validity_months, DROP COLUMN IF EXISTS currency")
