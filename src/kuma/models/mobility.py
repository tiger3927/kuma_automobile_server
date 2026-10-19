"""`mobility` schema：司机、组织归属、车队合同、计价、派单、打车订单、拼车（14 表）。

表已在库中（`007_mobility.sql`）；业务逻辑在 M10 落地，这里先给映射，
因为 scope 过滤器（`repositories/base.py`）依赖 `fleet_contract`。
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


class DriverProfile(Base):
    __tablename__ = "driver_profile"
    __table_args__ = {"schema": "mobility"}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    account_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("core.account.id"), nullable=False, unique=True)
    license_no: Mapped[str | None] = mapped_column(Text)
    license_expiry: Mapped[date | None] = mapped_column(Date)
    region_id: Mapped[str] = mapped_column(CHAR(2), ForeignKey("core.region.country_code"), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="none")
    score: Mapped[float | None] = mapped_column(Numeric(4, 2))
    total_trips: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    online_status: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    last_dispatch_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()"))


class DriverApplication(Base):
    __tablename__ = "driver_application"
    __table_args__ = {"schema": "mobility"}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    account_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("core.account.id"), nullable=False)
    operator_org_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("core.org.id"), nullable=False)
    region_id: Mapped[str] = mapped_column(CHAR(2), ForeignKey("core.region.country_code"), nullable=False)
    answers: Mapped[dict] = mapped_column(JSONB, nullable=False)
    docs: Mapped[dict | None] = mapped_column(JSONB)
    status: Mapped[str | None] = mapped_column(Text)
    reviewed_by: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("core.account.id"))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()"))


class OperatorBinding(Base):
    """司机同一时刻只归属一个运营商（§2.3）。"""

    __tablename__ = "operator_binding"
    __table_args__ = {"schema": "mobility"}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    driver_profile_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("mobility.driver_profile.id"), nullable=False)
    operator_org_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("core.org.id"), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="active")
    effective_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class FleetContract(Base):
    """一车同一时刻只有一份 active 合同；asset 与 franchise 互斥（§2.3）。"""

    __tablename__ = "fleet_contract"
    __table_args__ = {"schema": "mobility"}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    contract_no: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    contract_type: Mapped[str | None] = mapped_column(Text)
    operator_org_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("core.org.id"), nullable=False)
    vehicle_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("core.vehicle.id"), nullable=False)
    owner_account_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("core.account.id"))
    driver_account_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("core.account.id"), nullable=False)
    terms_version: Mapped[int] = mapped_column(Integer, nullable=False)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    expires_at: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="active")


class VehicleOperationPerm(Base):
    __tablename__ = "vehicle_operation_perm"
    __table_args__ = {"schema": "mobility"}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    vehicle_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("core.vehicle.id"), nullable=False)
    region_id: Mapped[str] = mapped_column(CHAR(2), ForeignKey("core.region.country_code"), nullable=False)
    permit_no: Mapped[str] = mapped_column(Text, nullable=False)
    permit_expiry: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="active")


class Tariff(Base):
    __tablename__ = "tariff"
    __table_args__ = (
        UniqueConstraint("region_id", "type_code", "version"),
        {"schema": "mobility"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    region_id: Mapped[str] = mapped_column(CHAR(2), ForeignKey("core.region.country_code"), nullable=False)
    type_code: Mapped[str] = mapped_column(Text, ForeignKey("core.vehicle_type.type_code"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    base_fare: Mapped[int] = mapped_column(BigInteger, nullable=False)
    per_km: Mapped[int | None] = mapped_column(BigInteger)
    per_min: Mapped[int | None] = mapped_column(BigInteger)
    wait_per_min: Mapped[int | None] = mapped_column(BigInteger)
    min_fare: Mapped[int | None] = mapped_column(BigInteger)
    luggage_fee: Mapped[int | None] = mapped_column(BigInteger)
    cargo_fee: Mapped[int | None] = mapped_column(BigInteger)
    night: Mapped[dict | None] = mapped_column(JSONB)
    peak: Mapped[dict | None] = mapped_column(JSONB)
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    status: Mapped[str | None] = mapped_column(Text)
    submitted_by: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("core.account.id"))
    approved_by: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("core.account.id"))
    effective_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DispatchPolicy(Base):
    __tablename__ = "dispatch_policy"
    __table_args__ = (
        UniqueConstraint("region_id", "version"),
        {"schema": "mobility"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    region_id: Mapped[str] = mapped_column(CHAR(2), ForeignKey("core.region.country_code"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    hard_rules: Mapped[dict] = mapped_column(JSONB, nullable=False)
    weights: Mapped[dict] = mapped_column(JSONB, nullable=False)
    offer_timeout_s: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=15)
    max_retry: Mapped[int | None] = mapped_column(SmallInteger, default=3)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_by: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("core.account.id"))
    status: Mapped[str] = mapped_column(Text, nullable=False, default="active")


class RideOrder(Base):
    __tablename__ = "ride_order"
    __table_args__ = {"schema": "mobility"}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    ride_no: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    passenger_account_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("core.account.id"), nullable=False)
    region_id: Mapped[str] = mapped_column(CHAR(2), ForeignKey("core.region.country_code"), nullable=False)
    type_code: Mapped[str] = mapped_column(Text, ForeignKey("core.vehicle_type.type_code"), nullable=False)
    pool_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("mobility.ride_pool.id"))
    seats_requested: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=1)
    pickup: Mapped[str | None] = mapped_column(Geometry)
    dropoff: Mapped[str | None] = mapped_column(Geometry)
    pickup_addr: Mapped[str | None] = mapped_column(Text)
    dropoff_addr: Mapped[str | None] = mapped_column(Text)
    driver_account_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("core.account.id"))
    vehicle_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("core.vehicle.id"))
    status: Mapped[str] = mapped_column(Text, nullable=False)
    trip_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("biz.trip.id"))
    tariff_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("mobility.tariff.id"), nullable=False)
    pricing: Mapped[dict | None] = mapped_column(JSONB)
    amount: Mapped[int | None] = mapped_column(BigInteger)
    points_offset: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    cash_amount: Mapped[int | None] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    cancel_reason: Mapped[str | None] = mapped_column(Text)
    cancel_by: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()"))


class RideOffer(Base):
    __tablename__ = "ride_offer"
    __table_args__ = {"schema": "mobility"}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    ride_order_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("mobility.ride_order.id"), nullable=False)
    vehicle_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("core.vehicle.id"), nullable=False)
    driver_account_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("core.account.id"), nullable=False)
    expire_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()"))


class DispatchDecision(Base):
    """派单必须可解释（Task-Plan.md §10-19）。"""

    __tablename__ = "dispatch_decision"
    __table_args__ = {"schema": "mobility"}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    ride_order_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("mobility.ride_order.id"), nullable=False)
    candidate_snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)
    scores: Mapped[dict] = mapped_column(JSONB, nullable=False)
    chosen_vehicle_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("core.vehicle.id"))
    policy_version: Mapped[int] = mapped_column(Integer, nullable=False)
    outcome: Mapped[str | None] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(Text)
    decided_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()"))


class RidePool(Base):
    __tablename__ = "ride_pool"
    __table_args__ = {"schema": "mobility"}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    vehicle_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("core.vehicle.id"), nullable=False)
    driver_account_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("core.account.id"), nullable=False)
    operator_org_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("core.org.id"), nullable=False)
    seats_total: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    seats_used: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    route_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    trip_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("biz.trip.id"))
    status: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PoolStop(Base):
    __tablename__ = "pool_stop"
    __table_args__ = (
        UniqueConstraint("pool_id", "order_id", "action"),
        {"schema": "mobility"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    pool_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("mobility.ride_pool.id"), nullable=False)
    seq: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    order_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("commerce.order.id"), nullable=False)
    action: Mapped[str | None] = mapped_column(Text)
    planned_eta: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    actual_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RideEvent(Base):
    __tablename__ = "ride_event"
    __table_args__ = {"schema": "mobility"}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    ride_order_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("mobility.ride_order.id"), nullable=False)
    event: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()"))


class RideRating(Base):
    __tablename__ = "ride_rating"
    __table_args__ = (
        UniqueConstraint("ride_order_id", "by_account_id"),
        {"schema": "mobility"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    ride_order_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("mobility.ride_order.id"), nullable=False)
    by_account_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("core.account.id"), nullable=False)
    role: Mapped[str | None] = mapped_column(Text)
    score: Mapped[int | None] = mapped_column(SmallInteger)
    tags: Mapped[dict | None] = mapped_column(JSONB)
    content: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()"))
