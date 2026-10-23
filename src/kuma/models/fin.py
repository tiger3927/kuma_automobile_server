"""`fin` schema：**只映射有写入方的表**（`Task-DataBase.md` §11.3 / 口径 §7.5 / §5.3）。

`fin` 12 张表里 `receivable` / `settlement_*` / `fin_journal` / `fx_rate_snapshot` /
`settlement_period` 尚无写入方，故意不映射——映射了没有写入路径的表，只会诱导出
"看起来已实现"的假接口（`Task-Server.md` §16 C10 的同一立场）。

- **`subsidy_ledger`（append-only）**：§10-14 的铁律落点——**积分抵扣不影响收款方应收**，
  收款方按抵扣前金额收款，差额由平台补贴，这张表就是"平台该掏多少补贴"的台账；
  **必须按 `points_kind` 分记**（§7.5：`purchased` 冲合同负债、`earned` 是营销费用，账务不同）。
- **`points_liability`**：现金买来的未消耗积分是**负债**（§10-16），每日快照，
  必须与 `points_ledger` 重算一致；写入方是 worker 的 `liability_snapshot` 任务。
- **`pay_order` / `payment_voucher` / `refund`**（收款线，与履约线正交，§5.2）：
  积分包是平台自营商品 → `collect_mode='platform_online'`，v1 的真实收款方式是
  **静态二维码 + 用户回填转账流水号 + 平台人工核销**（`provider='qr_transfer'`）。
  `uq_transfer_no`（`WHERE status <> 'rejected'`）是"同一转账流水号跨订单重复核销"的
  库级防线——§7.2 明写这是 v1 最大事故源，也是"同一支付账户给多账号购买"的拦截点。
  `refund.mode` 只有 `cash|points` 且**禁止交叉**（§7.4：交叉退就是积分变现通道）。
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import CHAR, BigInteger, Date, DateTime, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class SubsidyLedger(Base):
    """平台补贴流水（append-only）：积分抵扣与拼车折扣的差额，按 `points_kind` 分记。"""

    __tablename__ = "subsidy_ledger"
    __table_args__ = {"schema": "fin"}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    order_ref_type: Mapped[str] = mapped_column(Text, nullable=False)
    order_ref_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # 取值域在 DDL CHECK：points_offset | carpool_discount | swap_offset（0007 扩的最后一个）
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    points_kind: Mapped[str | None] = mapped_column(Text)
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    beneficiary_type: Mapped[str | None] = mapped_column(Text)
    beneficiary_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False,
                                                 server_default=text("now()"))


class PayOrder(Base):
    """支付单（收款线，与订单履约线正交）。

    `platform_online` 线的状态机（§5.2）：
    `created → qr_issued（出示收款二维码/账户）→ voucher_pending（用户回填流水号）
    → verifying（平台核销）→ paid`，↘ `timeout`（超时未回填/未过核销）→ `closed`。
    **一单只允许一张进行中的支付单**（`uq_pay_active` 部分唯一索引）——
    否则用户可以拿同一订单开多条支付单，用不同流水号付两次或核销两次。
    """

    __tablename__ = "pay_order"
    __table_args__ = {"schema": "fin"}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    pay_no: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    order_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # 取值域在 DDL CHECK：qr_transfer | points_offset | offline_voucher | channel
    provider: Mapped[str | None] = mapped_column(Text)
    channel_id: Mapped[int | None] = mapped_column(BigInteger)
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    status: Mapped[str | None] = mapped_column(Text)
    out_trade_no: Mapped[str | None] = mapped_column(Text, unique=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False,
                                                 server_default=text("now()"))


class PaymentVoucher(Base):
    """转账凭证：用户回填的流水号 + 截图，等平台核销（§9.5 转账核销工作台）。"""

    __tablename__ = "payment_voucher"
    __table_args__ = {"schema": "fin"}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    pay_order_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    transfer_no: Mapped[str] = mapped_column(Text, nullable=False)
    amount: Mapped[int | None] = mapped_column(BigInteger)
    currency: Mapped[str | None] = mapped_column(CHAR(3))
    proof_url: Mapped[str | None] = mapped_column(Text)
    uploaded_by: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'pending'"))
    verified_by: Mapped[int | None] = mapped_column(BigInteger)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False,
                                                 server_default=text("now()"))


class Refund(Base):
    """退款（按原路）。`mode` 只有 `cash|points`，**禁止交叉**（§7.4）。

    `points_reversal_ref` 指向被扣回的积分批次/购买单号——退款必须同时回退积分，
    否则出现"退了钱留着积分"的套利（§5.2 规则表）。
    """

    __tablename__ = "refund"
    __table_args__ = {"schema": "fin"}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    refund_no: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    pay_order_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    mode: Mapped[str | None] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    processed_by: Mapped[int | None] = mapped_column(BigInteger)
    points_reversal_ref: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False,
                                                 server_default=text("now()"))


class PointsLiability(Base):
    """积分负债日快照：Σ(未消耗 `purchased` 批次余额)，与 ledger 重算必须一致（§7.5）。"""

    __tablename__ = "points_liability"
    __table_args__ = (
        UniqueConstraint("snapshot_date", "account_id", name="uq_points_liability_snapshot"),
        {"schema": "fin"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    snapshot_date: Mapped[date] = mapped_column(Date, nullable=False)
    account_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    booked_points: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    fx_rate_id: Mapped[int | None] = mapped_column(BigInteger)
    cash_equiv: Mapped[int | None] = mapped_column(BigInteger)
