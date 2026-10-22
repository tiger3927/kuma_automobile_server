"""积分抵扣上线（换电侧先接，`Task-Plan.md` §7.3 / §7.5 / §16.9）

**为什么只加一张表**：`mkt` 九张积分表与 `fin.points_liability` / `fin.subsidy_ledger`
早在 `006_mkt.sql` / `005_fin.sql` 就建好了（实测 0 行），缺的不是库，而是
① **没有任何地方存"1 积分抵多少钱、最多抵百分之几"**——`points_rule` 只有获取比例
`ratio`，`points_pack` 是包价（拿包价当抵扣率 = 买最贵的包按最便宜的率抵，套利口子）；
② 换电返积分的来源值。所以这里只补版本化的抵扣规则 + 扩一个 CHECK。

1. **`mkt.points_offset_rule`（版本化，照 §11.0-6 规则表统一模式）**：
   `biz_line(commerce|ride|swap)` + `region_id` + `currency` + `points_per_unit` +
   `unit_scale` + `max_offset_pct` + `version/effective_from/effective_to/status/approved_by`。
   - **`points_per_unit` = 1 个主币单位值多少积分**（本轮定 **100**）；
     `unit_scale` = 该主币单位含多少**最小货币单位**（THB/MYR = 100，JPY/KRW = 1）。
     金额列一律是最小单位（§3.2"金额用整数、禁止 float"），所以抵扣现额 =
     `points × unit_scale / points_per_unit`（向下取整，余数留在现金侧，不凭空多抵一分钱）。
     把 `unit_scale` 存进规则而不是写死在代码里：多币种是本项目硬前提，而币种的小数位不是常数。
   - 上限按业态分开配（§7.3 货款 30% / 车费 20%、§16.9 换电 20%），
     `purchased` 与 `earned` **合并计算**（§7.4 防两类额度叠加）。
   - 唯一约束用 `UNIQUE NULLS NOT DISTINCT`（§11.0-6：`region_id` 为 NULL 即全区兜底行，
     PG 默认 UNIQUE 视 NULL 互不相等 → 兜底规则可被无限重复插入）。
2. **`mkt.points_rule.source_type` CHECK 扩 `'swap'`**：§7.2 换电返积分
   （实付换电费 × 比例，`settled` 才发）需要一个来源值，现值里没有。
3. **`mkt.points_account.frozen`**：§7.6 要求"三方对账不一致即告警并**冻结该账号积分**
   （不自动改账）"。没有这一列，冻结就只能靠插流水表达，而流水是 append-only，
   表达不了"当前状态"。

**授权不用写**：`010_grants_appendonly.sql:11-16` 已给 `mkt` 设了默认权限
（实测 `pg_default_acl` = `{kuma_app=arwd}` + `{kuma_app=rU}`），新表自动带 DML。
`points_ledger` 的 append-only（`010:19` 已 REVOKE UPDATE/DELETE）不动——
**冲正只能插正数流水**，这是账务底线。

**没有 seed**：抵扣汇率与上限是钱和营销口径，由平台在管理端配置（双人授权，§9.5），
迁移里写死数字等于替业务定价。

Revision ID: 0008_points_offset
Revises: 0007_energy_swap
"""
from __future__ import annotations

from alembic import op

revision = "0008_points_offset"
down_revision = "0007_energy_swap"
branch_labels = None
depends_on = None

SOURCE_TYPES_OLD = ("vehicle_purchase", "commerce", "ride", "campaign", "purchase")
SOURCE_TYPES_NEW = (*SOURCE_TYPES_OLD, "swap")


def _source_check(value: tuple[str, ...]) -> str:
    vals = ", ".join(f"'{v}'" for v in value)
    return f"CHECK (source_type IN ({vals}))"


def upgrade() -> None:
    # ══ 1. 抵扣规则（汇率 + 上限，版本化）═══════════════════════════════════
    op.execute("""
        CREATE TABLE mkt.points_offset_rule (
          id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
          rule_code text NOT NULL,
          biz_line text NOT NULL CHECK (biz_line IN ('commerce','ride','swap')),
          region_id char(2) REFERENCES core.region,
          currency char(3) NOT NULL,
          -- 1 主币单位 = points_per_unit 积分；unit_scale = 1 主币单位含多少最小单位
          points_per_unit bigint NOT NULL CHECK (points_per_unit > 0),
          unit_scale smallint NOT NULL DEFAULT 100 CHECK (unit_scale > 0),
          max_offset_pct smallint NOT NULL CHECK (max_offset_pct BETWEEN 0 AND 100),
          min_amount bigint CHECK (min_amount IS NULL OR min_amount >= 0),
          version int NOT NULL,
          effective_from date NOT NULL,
          effective_to date,
          status text NOT NULL DEFAULT 'draft'
            CHECK (status IN ('draft','pending_review','active','retired')),
          proposed_by bigint REFERENCES core.account,
          approved_by bigint REFERENCES core.account,
          created_at timestamptz NOT NULL DEFAULT now(),
          CONSTRAINT uq_offset_rule_version
            UNIQUE NULLS NOT DISTINCT (rule_code, biz_line, region_id, currency, version),
          CONSTRAINT chk_offset_period CHECK (effective_to IS NULL OR effective_to >= effective_from)
        )
    """)
    op.execute("""
        CREATE INDEX idx_offset_rule_lookup ON mkt.points_offset_rule
          (biz_line, currency, status) WHERE status = 'active'
    """)

    # ══ 2. 换电返积分的来源值（§7.2）════════════════════════════════════════
    op.execute("ALTER TABLE mkt.points_rule "
               "DROP CONSTRAINT IF EXISTS points_rule_source_type_check")
    op.execute("ALTER TABLE mkt.points_rule ADD CONSTRAINT points_rule_source_type_check "
               f"{_source_check(SOURCE_TYPES_NEW)}")

    # ══ 3. 账号冻结位（§7.6 对账不一致 → 冻结积分消耗，不自动改账）═══════════
    op.execute("ALTER TABLE mkt.points_account "
               "ADD COLUMN IF NOT EXISTS frozen boolean NOT NULL DEFAULT FALSE")
    op.execute("COMMENT ON COLUMN mkt.points_account.frozen IS "
               "'对账不一致或风控命中时冻结积分消耗（§7.6）；不影响已发放积分的展示与退回'")


def downgrade() -> None:
    op.execute("ALTER TABLE mkt.points_account DROP COLUMN IF EXISTS frozen")
    op.execute("ALTER TABLE mkt.points_rule "
               "DROP CONSTRAINT IF EXISTS points_rule_source_type_check")
    op.execute("ALTER TABLE mkt.points_rule ADD CONSTRAINT points_rule_source_type_check "
               f"{_source_check(SOURCE_TYPES_OLD)}")
    op.execute("DROP TABLE IF EXISTS mkt.points_offset_rule")
