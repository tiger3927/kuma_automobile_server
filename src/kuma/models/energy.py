"""`energy` schema：换电与电池资产（11 表，M15）。

字段与 `migrations/versions/0007_energy_swap_domain.py` 一一对应；
业务口径见 `Task-Plan.md` §16，DDL 说明见 `Task-DataBase.md` §11.11。

三条映射口径（与全项目一致）：
- **CHECK 与 partial unique 不在 ORM 声明**：取值域与"一车一条 active 绑定"这类
  部分唯一约束只存在于 DDL（`uq_binding_active` / `uq_lease_active_*` / `uq_deposit_open`），
  由数据库兜底，services 层负责给出可读错误码；
- **数组一律 JSONB**（不用 PG `ARRAY`），几何用 `base.Geometry`（EWKT 写入）；
- **`battery` 不存"车上是哪块"**：运行时位置唯一来源是 `core.vehicle_battery_binding`，
  这里只有 `station_id`（站内）——两处真相必然对不上账（`Task-Plan.md` §10-24）。
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    CHAR,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    SmallInteger,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, Geometry


class BatteryModel(Base):
    """电池型号字典：**型号 → 度数(kWh) → 换电单价**，同时是站点准入键与车型兼容键。

    `model_code` 一经用于台账与定价不可改（改规格 = 新增一行 + 旧行 `retired`），
    否则历史换电单引用的型号与价目规则无法解析。
    """

    __tablename__ = "battery_model"
    __table_args__ = {"schema": "energy"}

    model_code: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    vendor: Mapped[str] = mapped_column(Text, nullable=False)
    spec_doc_media_id: Mapped[int | None] = mapped_column(BigInteger)

    kwh_nominal: Mapped[float] = mapped_column(Numeric(6, 2), nullable=False)
    kwh_accept_min: Mapped[float] = mapped_column(Numeric(6, 2), nullable=False)
    voltage_nominal_v: Mapped[int] = mapped_column(Integer, nullable=False)
    voltage_max_v: Mapped[int | None] = mapped_column(Integer)
    voltage_cutoff_v: Mapped[int | None] = mapped_column(Integer)
    capacity_ah: Mapped[int | None] = mapped_column(Integer)
    chemistry: Mapped[str] = mapped_column(Text, nullable=False)
    cell_format: Mapped[str | None] = mapped_column(Text)
    cycle_life_rated: Mapped[int | None] = mapped_column(Integer)
    charge_max_c: Mapped[float | None] = mapped_column(Numeric(3, 2))
    discharge_max_c: Mapped[float | None] = mapped_column(Numeric(3, 2))
    temp_charge_min_c: Mapped[int | None] = mapped_column(Integer)
    temp_charge_max_c: Mapped[int | None] = mapped_column(Integer)

    dims_l_mm: Mapped[int] = mapped_column(Integer, nullable=False)
    dims_w_mm: Mapped[int] = mapped_column(Integer, nullable=False)
    dims_h_mm: Mapped[int] = mapped_column(Integer, nullable=False)
    weight_kg: Mapped[float] = mapped_column(Numeric(5, 2), nullable=False)
    needs_handling_aid: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    mounting: Mapped[str] = mapped_column(Text, nullable=False)
    mounting_drawing_media_id: Mapped[int | None] = mapped_column(BigInteger)
    ip_rating: Mapped[str] = mapped_column(Text, nullable=False)

    connector_type: Mapped[str] = mapped_column(Text, nullable=False)
    connector_keying: Mapped[str | None] = mapped_column(Text)
    bms_proto: Mapped[str] = mapped_column(Text, nullable=False)
    bms_sn_field: Mapped[str | None] = mapped_column(Text)
    telemetry_readable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    compatible_types: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    cert_marks: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    hazard_class: Mapped[str | None] = mapped_column(Text)
    recall_batch_support: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    status: Mapped[str] = mapped_column(Text, nullable=False, default="draft")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    approved_by: Mapped[int | None] = mapped_column(BigInteger)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()"))


class Station(Base):
    """换电站（运营商资产）。`allowed_models` 是**准入型号白名单**，改它必须写审计。"""

    __tablename__ = "station"
    __table_args__ = {"schema": "energy"}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    code: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    org_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("core.org.id"), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    geom: Mapped[str] = mapped_column(Geometry, nullable=False)
    address: Mapped[str | None] = mapped_column(Text)
    region_id: Mapped[str | None] = mapped_column(
        CHAR(2), ForeignKey("core.region.country_code"))
    business_hours: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    allowed_models: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    bay_count: Mapped[int | None] = mapped_column(SmallInteger)
    needs_handling_aid: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()"))


class Battery(Base):
    """SN 级单件资产（不是数量库存，所以不进 `commerce.stock`）。"""

    __tablename__ = "battery"
    __table_args__ = {"schema": "energy"}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    battery_sn: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    model_code: Mapped[str] = mapped_column(
        Text, ForeignKey("energy.battery_model.model_code"), nullable=False)
    owner_org_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("core.org.id"), nullable=False)
    batch_no: Mapped[str | None] = mapped_column(Text)
    kwh_actual: Mapped[float | None] = mapped_column(Numeric(6, 2))
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    accept_media_id: Mapped[int | None] = mapped_column(BigInteger)
    cycles: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    soc: Mapped[int | None] = mapped_column(SmallInteger)
    station_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("energy.station.id"))
    status: Mapped[str] = mapped_column(Text, nullable=False, default="in_stock")
    quality_complaints: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()"))


class BatteryFlow(Base):
    """电池流转流水（append-only，库里 `REVOKE UPDATE, DELETE`）：召回与丢失举证的唯一依据。"""

    __tablename__ = "battery_flow"
    __table_args__ = {"schema": "energy"}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    battery_sn: Mapped[str] = mapped_column(Text, nullable=False)
    from_status: Mapped[str | None] = mapped_column(Text)
    to_status: Mapped[str] = mapped_column(Text, nullable=False)
    station_id: Mapped[int | None] = mapped_column(BigInteger)
    vin: Mapped[str | None] = mapped_column(CHAR(17))
    swap_order_id: Mapped[int | None] = mapped_column(BigInteger)
    actor_account_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    actor_role: Mapped[str | None] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text)
    media_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()"))


class StationStock(Base):
    """"这个站这个型号还有几块能用"的唯一来源（用户端/看板/报表都只读这张）。"""

    __tablename__ = "station_stock"
    __table_args__ = (
        UniqueConstraint("station_id", "model_code"),
        {"schema": "energy"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    station_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("energy.station.id"), nullable=False)
    model_code: Mapped[str] = mapped_column(
        Text, ForeignKey("energy.battery_model.model_code"), nullable=False)
    ready_count: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    charging_count: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    faulty_count: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()"))


class SwapPrice(Base):
    """按型号按次定价（版本化规则表；NULL `region_id`/`station_org_id` = 兜底规则）。"""

    __tablename__ = "swap_price"
    __table_args__ = (
        UniqueConstraint("region_id", "model_code", "station_org_id", "version"),
        {"schema": "energy"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    region_id: Mapped[str | None] = mapped_column(
        CHAR(2), ForeignKey("core.region.country_code"))
    model_code: Mapped[str] = mapped_column(
        Text, ForeignKey("energy.battery_model.model_code"), nullable=False)
    station_org_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("core.org.id"))
    price: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    min_soc_pct: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=95)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="draft")
    proposed_by: Mapped[int | None] = mapped_column(BigInteger)
    approved_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()"))


class DepositRule(Base):
    """押金档位（按型号与地区；度数越大越高）。免押门槛 `waiver_min_kyc` 也在这里。"""

    __tablename__ = "deposit_rule"
    __table_args__ = (
        UniqueConstraint("region_id", "model_code", "version"),
        {"schema": "energy"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    region_id: Mapped[str | None] = mapped_column(
        CHAR(2), ForeignKey("core.region.country_code"))
    model_code: Mapped[str] = mapped_column(
        Text, ForeignKey("energy.battery_model.model_code"), nullable=False)
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    waiver_min_kyc: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=3)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="draft")
    proposed_by: Mapped[int | None] = mapped_column(BigInteger)
    approved_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()"))


class BatteryLease(Base):
    """换电协议：押金是**生效前置**，条款版本化；按人/组织，不按 VIN。"""

    __tablename__ = "battery_lease"
    __table_args__ = {"schema": "energy"}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    lease_no: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    holder_type: Mapped[str] = mapped_column(Text, nullable=False)
    holder_account_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("core.account.id"))
    holder_org_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("core.org.id"))
    operator_org_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("core.org.id"), nullable=False)
    model_code: Mapped[str] = mapped_column(
        Text, ForeignKey("energy.battery_model.model_code"), nullable=False)
    deposit_amount_snapshot: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    deposit_waived: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    terms_version: Mapped[str] = mapped_column(Text, nullable=False)
    terms_accepted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="draft")
    joined_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()"))


class DepositLedger(Base):
    """押金台账：**负债，不是收入**。与 `fin.settlement_item` / `fin.fin_journal` 无 FK、无写入路径。"""

    __tablename__ = "deposit_ledger"
    __table_args__ = {"schema": "energy"}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    deposit_no: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    lease_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("energy.battery_lease.id"), nullable=False)
    payer_type: Mapped[str] = mapped_column(Text, nullable=False)
    payer_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    operator_org_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("core.org.id"), nullable=False)
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="open")
    collected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    collected_voucher_no: Mapped[str | None] = mapped_column(Text)
    proof_media_id: Mapped[int | None] = mapped_column(BigInteger)
    refunded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    refund_voucher_no: Mapped[str | None] = mapped_column(Text)
    forfeit_reason: Mapped[str | None] = mapped_column(Text)
    forfeit_swap_order_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()"))


class SwapOrder(Base):
    """换电单：即时服务单，与 `mobility.ride_order` 同构（不复用 `commerce."order"`）。

    `idempotency_key` 唯一是站点弱网重试的防线；`price_rule_id` 是版本快照——
    改价后历史单仍可复现（§11.0-6）。
    """

    __tablename__ = "swap_order"
    __table_args__ = {"schema": "energy"}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    swap_no: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    station_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("energy.station.id"), nullable=False)
    operator_org_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("core.org.id"), nullable=False)
    lease_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("energy.battery_lease.id"), nullable=False)
    account_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("core.account.id"), nullable=False)
    vin: Mapped[str] = mapped_column(CHAR(17), ForeignKey("core.vehicle.vin"), nullable=False)
    model_code: Mapped[str] = mapped_column(
        Text, ForeignKey("energy.battery_model.model_code"), nullable=False)
    out_battery_sn: Mapped[str] = mapped_column(Text, nullable=False)
    in_battery_sn: Mapped[str | None] = mapped_column(Text)
    price_rule_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("energy.swap_price.id"), nullable=False)
    pricing: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    cash_amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    points_offset: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    collect_mode: Mapped[str] = mapped_column(Text, nullable=False, default="operator_offline")
    pay_order_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("fin.pay_order.id"))
    idempotency_key: Mapped[str | None] = mapped_column(Text, unique=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="created")
    reject_reason: Mapped[str | None] = mapped_column(Text)
    re_swap_of: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("energy.swap_order.id"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()"))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SwapEvent(Base):
    """换电状态轨迹（append-only，照 `mobility.ride_event`）。"""

    __tablename__ = "swap_event"
    __table_args__ = {"schema": "energy"}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    swap_order_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("energy.swap_order.id"), nullable=False)
    event: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    actor_account_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()"))
