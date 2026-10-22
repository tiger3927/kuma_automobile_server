"""`fin` schema：**本轮只映射积分抵扣要用的两张**（`Task-DataBase.md` §11.3 / 口径 §7.5）。

`fin` 12 张表里其余（`pay_order` / `receivable` / `settlement_*` / `fin_journal` …）
尚无写入方，故意不映射——映射了没有写入路径的表，只会诱导出"看起来已实现"的假接口
（`Task-Server.md` §16 C10 的同一立场）。

- **`subsidy_ledger`（append-only）**：§10-14 的铁律落点——**积分抵扣不影响收款方应收**，
  收款方按抵扣前金额收款，差额由平台补贴，这张表就是"平台该掏多少补贴"的台账；
  **必须按 `points_kind` 分记**（§7.5：`purchased` 冲合同负债、`earned` 是营销费用，账务不同）。
- **`points_liability`**：现金买来的未消耗积分是**负债**（§10-16），每日快照，
  必须与 `points_ledger` 重算一致；写入方是 worker 的日终任务（尚未实现，见 §7.6）。
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
