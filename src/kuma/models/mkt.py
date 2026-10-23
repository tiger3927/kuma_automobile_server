"""`mkt` schema：积分账本与抵扣规则（7 表，`Task-DataBase.md` §11.4 / 口径 `Task-Plan.md` §7）。

表早在 `006_mkt.sql` 就建好了，这里只做映射；本轮为"积分抵扣"上线而补 `PointsOffsetRule`
（迁移 `0008_points_offset`）。三条映射口径：

- **`points_ledger` 是 append-only**（`010_grants_appendonly.sql:19` 对 `kuma_app`
  REVOKE UPDATE/DELETE）：任何调整都只能**插一条反向流水**（冲正），
  改历史流水在库里就会被拒（§10-17）。`points_account` 是可重算的余额快照，
  必须与 ledger 重算一致（§7.6 三方对账）。
- **两类积分分列计量**（`earned` 赠送 / `purchased` 现金购买）：账务不同（§7.5，
  购买的是合同负债、赠送的是营销费用），所以 `points_account` 两列、
  `points_batch`/`points_ledger`/`subsidy_ledger` 都带 `points_kind`。混成一列，
  负债表就再也对不平（§10-16）。
- **余额不是真相**：真相是 `points_batch.remaining`（按 `expires_at` 升序扣，§7.6
  先扣快过期的，对用户有利）。`points_account` 只是快照，扣减必须在锁住批次行的同事务里改。
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    CHAR,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Integer,
    Numeric,
    SmallInteger,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class PointsRule(Base):
    """获取规则（版本化）：买车 / 商城 / 打车 / 换电 / 活动 / 买包各一条比例或固定点数。

    `source_type` 的取值域在 DDL CHECK 里（`0008` 扩了 `'swap'`，§7.2 换电返积分）。

    **`ratio` 的口径必须钉死：每 1「最小货币单位」返多少积分**（金额列是最小单位，§3.2）。
    例：THB 下"实付 1 泰铢返 1 积分" = `ratio = 0.01`（1 泰铢 = 100 satang）。
    所以 `currency` 不能省（迁移 0009）——一条 `region_id IS NULL` 的兜底规则套到
    另一种货币上，返积分会差一个数量级。`fixed_points` 是"每单固定点数"（买车按车型用）。
    """

    __tablename__ = "points_rule"
    __table_args__ = (
        # 0009：改 NULLS NOT DISTINCT 并把 currency 纳入——原约束视 NULL 互不相等，
        # 全区兜底行（region_id IS NULL）可被无限重复插入（§11.0-6）
        UniqueConstraint("rule_code", "region_id", "currency", "version",
                         name="uq_points_rule_version"),
        {"schema": "mkt"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    rule_code: Mapped[str] = mapped_column(Text, nullable=False)
    source_type: Mapped[str | None] = mapped_column(Text)
    region_id: Mapped[str | None] = mapped_column(CHAR(2))
    currency: Mapped[str | None] = mapped_column(CHAR(3))
    type_code: Mapped[str | None] = mapped_column(Text)
    ratio: Mapped[float | None] = mapped_column(Numeric(8, 6))
    fixed_points: Mapped[int | None] = mapped_column(BigInteger)
    caps: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'"))
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'draft'"))
    proposed_by: Mapped[int | None] = mapped_column(BigInteger)
    approved_by: Mapped[int | None] = mapped_column(BigInteger)
    # 0009：§7.1 赠送积分 12 个月过期——期限属于规则（版本化），不是代码常量
    validity_months: Mapped[int] = mapped_column(SmallInteger, nullable=False,
                                                 server_default=text("12"))


class PointsPack(Base):
    """积分包是**商品**不是充值接口（§5.1 自洽要点 2）：现金买积分必须走同一订单骨架。

    `sku_id`（迁移 0009）是那条链的锚点：`commerce.order_item.sku_id` 是 NOT NULL，
    没有 sku 就开不出订单，也就没有 `points_purchase.order_id`。
    平台配包时必须同步落 `commerce.product` + `commerce.sku`（见 `purchase_service.create_pack`）。
    """

    __tablename__ = "points_pack"
    __table_args__ = {"schema": "mkt"}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    pack_code: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    points: Mapped[int] = mapped_column(BigInteger, nullable=False)
    price: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    validity_months: Mapped[int] = mapped_column(SmallInteger, nullable=False,
                                                 server_default=text("24"))
    region_id: Mapped[str | None] = mapped_column(CHAR(2))
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'active'"))
    sku_id: Mapped[int | None] = mapped_column(BigInteger)


class PointsPurchase(Base):
    """一次积分包购买（挂 `commerce.order`）；入账写 batch + ledger，同时写积分负债。

    `account_id`（0009）不是冗余：日/月购买限额在下单热路径上按账号聚合，
    只有 `order_id` 就得每次 join `commerce."order"`。
    `paid_at` ≠ `created_at`：订单可以先建后付，§7.2 的"支付成功后即时入账"以 `paid_at` 为准。
    """

    __tablename__ = "points_purchase"
    __table_args__ = {"schema": "mkt"}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    purchase_no: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    order_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    account_id: Mapped[int | None] = mapped_column(BigInteger)
    pack_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    points: Mapped[int] = mapped_column(BigInteger, nullable=False)
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    fx_rate_id: Mapped[int | None] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'completed'"))
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False,
                                                 server_default=text("now()"))


class PointsAccount(Base):
    """余额快照（可按 ledger 全量重算校验）。**两类积分分列**，因为账务不同（§7.5）。"""

    __tablename__ = "points_account"
    __table_args__ = {"schema": "mkt"}

    account_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    earned_balance: Mapped[int] = mapped_column(BigInteger, nullable=False,
                                               server_default=text("0"))
    purchased_balance: Mapped[int] = mapped_column(BigInteger, nullable=False,
                                                  server_default=text("0"))
    # 0008：§7.6 三方对账不一致 / 风控命中 → 冻结消耗（不自动改账）
    frozen: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False,
                                                 server_default=text("now()"))


class PointsBatch(Base):
    """积分批次：扣减按 `expires_at` **升序**（§7.6），`remaining` 才是可花余额的真相。"""

    __tablename__ = "points_batch"
    __table_args__ = {"schema": "mkt"}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    account_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    points_kind: Mapped[str] = mapped_column(Text, nullable=False)
    total: Mapped[int] = mapped_column(BigInteger, nullable=False)
    remaining: Mapped[int] = mapped_column(BigInteger, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source_type: Mapped[str] = mapped_column(Text, nullable=False)
    source_ref: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False,
                                                 server_default=text("now()"))


class PointsLedger(Base):
    """append-only 流水（正=发放/购买/冲正，负=消耗/过期）。

    `uq_ledger_source (source_type, source_ref, points_kind)` 是**幂等的最后一道防线**：
    同一笔业务单对同一类积分只能记一次，重复扣/重复发在库里直接被拒。
    """

    __tablename__ = "points_ledger"
    __table_args__ = {"schema": "mkt"}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    account_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    points_kind: Mapped[str] = mapped_column(Text, nullable=False)
    delta: Mapped[int] = mapped_column(BigInteger, nullable=False)
    balance_after: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_type: Mapped[str] = mapped_column(Text, nullable=False)
    source_ref: Mapped[str] = mapped_column(Text, nullable=False)
    memo: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False,
                                                 server_default=text("now()"))


class PointsOffsetRule(Base):
    """抵扣规则（迁移 0008）：**1 主币单位 = `points_per_unit` 积分**，单笔最多抵 `max_offset_pct`%。

    为什么必须有这张表（而不是复用 `points_pack` 的包价）：
    - 金额列是最小货币单位，`unit_scale` 表达"1 主币单位含多少最小单位"
      （THB/MYR=100、JPY/KRW=1），写死在代码里会在多币种下算错一个数量级；
    - 上限按业态不同（§7.3 货款 30% / 车费 20%，§16.9 换电 20%）且必须**版本化**——
      改汇率不能追溯改变历史单的抵扣口径（`swap_order.pricing` 存规则快照）。
    """

    __tablename__ = "points_offset_rule"
    __table_args__ = (
        UniqueConstraint("rule_code", "biz_line", "region_id", "currency", "version",
                         name="uq_offset_rule_version"),
        {"schema": "mkt"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    rule_code: Mapped[str] = mapped_column(Text, nullable=False)
    biz_line: Mapped[str] = mapped_column(Text, nullable=False)
    region_id: Mapped[str | None] = mapped_column(CHAR(2))
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    points_per_unit: Mapped[int] = mapped_column(BigInteger, nullable=False)
    unit_scale: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("100"))
    max_offset_pct: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    min_amount: Mapped[int | None] = mapped_column(BigInteger)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'draft'"))
    proposed_by: Mapped[int | None] = mapped_column(BigInteger)
    approved_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False,
                                                 server_default=text("now()"))
