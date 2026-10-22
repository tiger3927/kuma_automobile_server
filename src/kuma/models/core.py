"""`core` schema：账号与叠加身份、组织分级、车辆主数据、订阅、媒体（21 表）。

字段与 `migrations/sql/002_core.sql` 一一对应。
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
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base


class Region(Base):
    __tablename__ = "region"
    __table_args__ = {"schema": "core"}

    country_code: Mapped[str] = mapped_column(CHAR(2), primary_key=True)
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    timezone: Mapped[str] = mapped_column(Text, nullable=False)
    features: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="active")


class Account(Base):
    __tablename__ = "account"
    __table_args__ = {"schema": "core"}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    username: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    phone: Mapped[str | None] = mapped_column(Text)
    email: Mapped[str | None] = mapped_column(String)
    password_hash: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="active")
    kyc_level: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    region_id: Mapped[str | None] = mapped_column(CHAR(2), ForeignKey("core.region.country_code"))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()"))

    identities: Mapped[list[Identity]] = relationship(back_populates="account")


class Org(Base):
    __tablename__ = "org"
    __table_args__ = {"schema": "core"}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    org_type: Mapped[str] = mapped_column(Text, nullable=False)
    code: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    parent_org_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("core.org.id"))
    level: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=1)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    region_id: Mapped[str | None] = mapped_column(CHAR(2), ForeignKey("core.region.country_code"))
    primary_account_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("core.account.id"), unique=True)
    # M15：集团聚合（兼容"多法人各持牌"）；NULL = 不属任何集团
    group_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("core.org_group.id"))
    status: Mapped[str] = mapped_column(Text, nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()"))


class OrgGroup(Base):
    """集团（M15）：多条 org 挂同一集团，集团视图与报表有唯一来源。"""

    __tablename__ = "org_group"
    __table_args__ = {"schema": "core"}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    country_code: Mapped[str | None] = mapped_column(CHAR(2))
    tax_id_hash: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()"))


class OrgBusiness(Base):
    """业务线 + 牌照（M15）：一家公司同时是经销商 / 网约车运营商 / 换电站运营商。

    `org_type` 语义降级为**主业态**（CHECK 不动，向后兼容 M1–M2 已交付代码）；
    scope 判定按业务线走 `Principal.org_ids_for_line()`，不再只看 `org_type`。
    """

    __tablename__ = "org_business"
    __table_args__ = (
        UniqueConstraint("org_id", "line"),
        {"schema": "core"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    org_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("core.org.id"), nullable=False)
    line: Mapped[str] = mapped_column(Text, nullable=False)
    license_no: Mapped[str | None] = mapped_column(Text)
    license_expiry: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="active")
    approved_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()"))


class Identity(Base):
    __tablename__ = "identity"
    __table_args__ = (
        UniqueConstraint("account_id", "identity_type"),
        {"schema": "core"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    account_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("core.account.id"), nullable=False)
    identity_type: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()"))

    account: Mapped[Account] = relationship(back_populates="identities")


class Membership(Base):
    __tablename__ = "membership"
    __table_args__ = (
        UniqueConstraint("account_id", "org_id", "role"),
        {"schema": "core"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    account_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("core.account.id"), nullable=False)
    org_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("core.org.id"), nullable=False)
    role: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()"))


class DealerProfile(Base):
    __tablename__ = "dealer_profile"
    __table_args__ = {"schema": "core"}

    org_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("core.org.id"), primary_key=True)
    tier: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=1)
    msrp_min_pct: Mapped[float | None] = mapped_column(Numeric(5, 4))
    msrp_max_pct: Mapped[float | None] = mapped_column(Numeric(5, 4))
    commission_override: Mapped[float | None] = mapped_column(Numeric(5, 4))


class OperatorProfile(Base):
    __tablename__ = "operator_profile"
    __table_args__ = {"schema": "core"}

    org_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("core.org.id"), primary_key=True)
    license_no: Mapped[str | None] = mapped_column(Text)
    license_expiry: Mapped[date | None] = mapped_column(Date)
    fare_template_owner: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class UserProfile(Base):
    __tablename__ = "user_profile"
    __table_args__ = {"schema": "core"}

    account_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("core.account.id"), primary_key=True)
    display_name: Mapped[str | None] = mapped_column(Text)
    avatar_url: Mapped[str | None] = mapped_column(Text)
    locale: Mapped[str | None] = mapped_column(Text)
    id_type: Mapped[str | None] = mapped_column(Text)
    id_number_hash: Mapped[str | None] = mapped_column(Text)


class KycRecord(Base):
    __tablename__ = "kyc_record"
    __table_args__ = {"schema": "core"}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    account_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("core.account.id"), nullable=False)
    level: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    id_type: Mapped[str | None] = mapped_column(Text)
    id_number_hash: Mapped[str | None] = mapped_column(Text)
    docs: Mapped[dict | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    reviewed_by: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("core.account.id"))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()"))


class RefreshToken(Base):
    __tablename__ = "refresh_token"
    __table_args__ = {"schema": "core"}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    account_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("core.account.id"), nullable=False)
    token_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    device_id: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OtpCode(Base):
    __tablename__ = "otp_code"
    __table_args__ = {"schema": "core"}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    target: Mapped[str] = mapped_column(Text, nullable=False)
    purpose: Mapped[str] = mapped_column(Text, nullable=False)
    code_hash: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int | None] = mapped_column(SmallInteger, default=0)


class IdempotencyKey(Base):
    __tablename__ = "idempotency_key"
    __table_args__ = {"schema": "core"}

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    account_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("core.account.id"))
    response_hash: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()"))


class VehicleType(Base):
    __tablename__ = "vehicle_type"
    __table_args__ = {"schema": "core"}

    type_code: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    product_line: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(Text, nullable=False)
    seats: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    passenger_seats: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    doors: Mapped[int | None] = mapped_column(SmallInteger)
    load_capacity_kg: Mapped[int | None] = mapped_column(Integer)
    cargo_len_mm: Mapped[int | None] = mapped_column(Integer)
    cargo_wid_mm: Mapped[int | None] = mapped_column(Integer)
    cargo_hgt_mm: Mapped[int | None] = mapped_column(Integer)
    length_mm: Mapped[int | None] = mapped_column(Integer)
    width_mm: Mapped[int | None] = mapped_column(Integer)
    height_mm: Mapped[int | None] = mapped_column(Integer)
    wheelbase_mm: Mapped[int | None] = mapped_column(Integer)
    min_ground_clearance_mm: Mapped[int | None] = mapped_column(Integer)
    curb_weight_kg: Mapped[int | None] = mapped_column(Integer)
    gvwr_kg: Mapped[int | None] = mapped_column(Integer)
    power_type: Mapped[str] = mapped_column(Text, nullable=False, default="ev")
    motor_power_kw: Mapped[float | None] = mapped_column(Numeric(6, 2))
    peak_power_kw: Mapped[float | None] = mapped_column(Numeric(6, 2))
    motor_count: Mapped[int | None] = mapped_column(SmallInteger)
    battery_kwh: Mapped[float | None] = mapped_column(Numeric(6, 2))
    battery_type: Mapped[str | None] = mapped_column(Text)
    range_km: Mapped[int | None] = mapped_column(Integer)
    range_condition: Mapped[str | None] = mapped_column(Text)
    top_speed_kmh: Mapped[int | None] = mapped_column(SmallInteger)
    climbing_grade_pct: Mapped[int | None] = mapped_column(SmallInteger)
    charge_fast_h: Mapped[float | None] = mapped_column(Numeric(4, 1))
    charge_slow_h: Mapped[float | None] = mapped_column(Numeric(4, 1))
    drive_type: Mapped[str | None] = mapped_column(Text)
    fuel_l_100km: Mapped[float | None] = mapped_column(Numeric(5, 2))
    specs: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    features: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    protocol_profile: Mapped[str] = mapped_column(Text, nullable=False)
    regions: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    # M15 换电：与 `energy.battery_model.compatible_types` 构成**双向声明**，缺一即拒换电
    swap_compatible: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    battery_model_code: Mapped[str | None] = mapped_column(
        Text, ForeignKey("energy.battery_model.model_code"))
    status: Mapped[str] = mapped_column(Text, nullable=False, default="active")


class Vehicle(Base):
    __tablename__ = "vehicle"
    __table_args__ = {"schema": "core"}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    vin: Mapped[str] = mapped_column(CHAR(17), nullable=False, unique=True)
    type_code: Mapped[str] = mapped_column(Text, ForeignKey("core.vehicle_type.type_code"), nullable=False)
    plate: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(Text)
    trim: Mapped[str | None] = mapped_column(Text)
    model_year: Mapped[int | None] = mapped_column(SmallInteger)
    color: Mapped[str | None] = mapped_column(Text)
    color_code: Mapped[str | None] = mapped_column(Text)
    manufacture_date: Mapped[date | None] = mapped_column(Date)
    factory_code: Mapped[str | None] = mapped_column(Text)
    # ⚠ M15：battery_sn 语义 = **出厂电池**（出厂追溯/质保），运行时"车上装哪块"以
    #    core.vehicle_battery_binding 为准；其唯一索引 uq_vehicle_battery_sn 已在迁移 0007 撤销
    #    （换电后一块电池先后属于多台车，SN 唯一性由 energy.battery 接管）
    battery_sn: Mapped[str | None] = mapped_column(Text)
    motor_sn: Mapped[str | None] = mapped_column(Text)
    battery_kwh_actual: Mapped[float | None] = mapped_column(Numeric(6, 2))
    odo_at_delivery_km: Mapped[int | None] = mapped_column(Integer)
    factory_attrs: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    owner_type: Mapped[str] = mapped_column(Text, nullable=False)
    owner_account_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("core.account.id"))
    owner_org_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("core.org.id"))
    usage_type: Mapped[str] = mapped_column(Text, nullable=False, default="retail")
    region_id: Mapped[str | None] = mapped_column(CHAR(2), ForeignKey("core.region.country_code"))
    cur_dealer_org_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("core.org.id"))
    status: Mapped[str] = mapped_column(Text, nullable=False, default="produced")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()"))


class VehicleBatteryBinding(Base):
    """运行时"这台车上装的是哪块电池"的**唯一来源**（M15 / `Task-Plan.md` §4.5）。

    两条部分唯一索引在 DDL 里兜底（ORM 不声明 partial index）：
    `uq_binding_active`(vin) 与 `uq_binding_battery_active`(battery_sn)，
    `ended_at IS NULL` 即当前生效——换电时解绑旧行 + 插新行必须同事务。
    """

    __tablename__ = "vehicle_battery_binding"
    __table_args__ = {"schema": "core"}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    vin: Mapped[str] = mapped_column(CHAR(17), ForeignKey("core.vehicle.vin"), nullable=False)
    battery_sn: Mapped[str] = mapped_column(
        Text, ForeignKey("energy.battery.battery_sn"), nullable=False)
    station_id: Mapped[int | None] = mapped_column(BigInteger)
    swap_order_id: Mapped[int | None] = mapped_column(BigInteger)
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="swap")
    effective_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()"))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Device(Base):
    __tablename__ = "device"
    __table_args__ = {"schema": "core"}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    device_sn: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    vin: Mapped[str | None] = mapped_column(CHAR(17), ForeignKey("core.vehicle.vin"))
    model: Mapped[str | None] = mapped_column(Text)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    replaced_from_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("core.device.id"))


class Sim(Base):
    __tablename__ = "sim"
    __table_args__ = {"schema": "core"}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    iccid: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    carrier: Mapped[str | None] = mapped_column(Text)
    device_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("core.device.id"))


class VehicleCredential(Base):
    """一机一密（M2 只生成不下发；下发在 M9 交付核销 + 订阅生效之后）。"""

    __tablename__ = "vehicle_credential"
    __table_args__ = {"schema": "core"}

    vin: Mapped[str] = mapped_column(CHAR(17), ForeignKey("core.vehicle.vin"), primary_key=True)
    mqtt_username: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=1)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="active")
    issued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SubscriptionPlan(Base):
    __tablename__ = "subscription_plan"
    __table_args__ = {"schema": "core"}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    code: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    region_id: Mapped[str | None] = mapped_column(CHAR(2), ForeignKey("core.region.country_code"))
    plan_type: Mapped[str | None] = mapped_column(Text)
    name: Mapped[str | None] = mapped_column(Text)
    price: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    duration_days: Mapped[int] = mapped_column(Integer, nullable=False)
    features: Mapped[dict | None] = mapped_column(JSONB)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="active")


class Subscription(Base):
    __tablename__ = "subscription"
    __table_args__ = {"schema": "core"}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    vin: Mapped[str] = mapped_column(CHAR(17), ForeignKey("core.vehicle.vin"), nullable=False)
    plan_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("core.subscription_plan.id"), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="active")
    start_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    end_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    auto_renew: Mapped[bool | None] = mapped_column(Boolean, default=True)
    payer_account_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("core.account.id"))


class OwnershipTransfer(Base):
    __tablename__ = "ownership_transfer"
    __table_args__ = {"schema": "core"}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    vin: Mapped[str] = mapped_column(CHAR(17), ForeignKey("core.vehicle.vin"), nullable=False)
    from_type: Mapped[str | None] = mapped_column(Text)
    from_account_id: Mapped[int | None] = mapped_column(BigInteger)
    from_org_id: Mapped[int | None] = mapped_column(BigInteger)
    to_type: Mapped[str | None] = mapped_column(Text)
    to_account_id: Mapped[int | None] = mapped_column(BigInteger)
    to_org_id: Mapped[int | None] = mapped_column(BigInteger)
    transferred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    operator_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("core.account.id"))
    attachment: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()"))


class Media(Base):
    """图片/文件统一表（§11.1：不为每种对象建 media 列）。"""

    __tablename__ = "media"
    __table_args__ = {"schema": "core"}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    owner_type: Mapped[str] = mapped_column(Text, nullable=False)
    owner_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    role: Mapped[str] = mapped_column(Text, nullable=False)
    file_key: Mapped[str] = mapped_column(Text, nullable=False)
    mime: Mapped[str | None] = mapped_column(Text)
    width_px: Mapped[int | None] = mapped_column(Integer)
    height_px: Mapped[int | None] = mapped_column(Integer)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    sha256: Mapped[str | None] = mapped_column(CHAR(64))
    sort: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    uploaded_by: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("core.account.id"))
    status: Mapped[str] = mapped_column(Text, nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()"))
