"""`biz` schema：行程、里程核算、告警、下行指令与不可篡改审计（6 表）。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    CHAR,
    BigInteger,
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

from .base import Base


class Trip(Base):
    __tablename__ = "trip"
    __table_args__ = (
        UniqueConstraint("vin", "start_ts"),
        {"schema": "biz"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    vin: Mapped[str] = mapped_column(CHAR(17), ForeignKey("core.vehicle.vin"), nullable=False)
    owner_type: Mapped[str] = mapped_column(Text, nullable=False)
    owner_account_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("core.account.id"))
    owner_org_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("core.org.id"))
    driver_account_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("core.account.id"))
    operator_org_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("core.org.id"))
    region_id: Mapped[str | None] = mapped_column(CHAR(2), ForeignKey("core.region.country_code"))
    start_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    start_odo_km: Mapped[int | None] = mapped_column(Integer)
    end_odo_km: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()"))


class TripMileage(Base):
    """三种口径并存（§11）：odo 是结算与政府上报的唯一主口径。"""

    __tablename__ = "trip_mileage"
    __table_args__ = (
        UniqueConstraint("trip_id", "method"),
        {"schema": "biz"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    trip_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("biz.trip.id"), nullable=False)
    method: Mapped[str] = mapped_column(Text, nullable=False)   # odo|gps|road
    distance_m: Mapped[int] = mapped_column(Integer, nullable=False)
    confidence: Mapped[float | None] = mapped_column(Numeric(4, 3))
    flags: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()"))


class Alarm(Base):
    __tablename__ = "alarm"
    __table_args__ = (
        UniqueConstraint("vin", "ts", "type"),
        {"schema": "biz"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    vin: Mapped[str] = mapped_column(CHAR(17), ForeignKey("core.vehicle.vin"), nullable=False)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    type: Mapped[str] = mapped_column(Text, nullable=False)
    level: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=2)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="open")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()"))


class AlarmAck(Base):
    __tablename__ = "alarm_ack"
    __table_args__ = {"schema": "biz"}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    alarm_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("biz.alarm.id"), nullable=False)
    ack_by: Mapped[int] = mapped_column(BigInteger, ForeignKey("core.account.id"), nullable=False)
    ack_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()"))
    note: Mapped[str | None] = mapped_column(Text)


class Command(Base):
    """下行指令状态机：pending → sent → acked | timeout → retry(≤3) → failed。"""

    __tablename__ = "command"
    __table_args__ = {"schema": "biz"}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    cmd_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    vin: Mapped[str] = mapped_column(CHAR(17), ForeignKey("core.vehicle.vin"), nullable=False)
    cmd_type: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    attempt: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    created_by: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("core.account.id"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()"))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    acked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CommandAudit(Base):
    """append-only，永久保留；对 kuma_app 无 UPDATE/DELETE 权限。"""

    __tablename__ = "command_audit"
    __table_args__ = {"schema": "biz"}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    cmd_id: Mapped[str] = mapped_column(Text, nullable=False)
    attempt: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    event: Mapped[str] = mapped_column(Text, nullable=False)
    actor_account_id: Mapped[int | None] = mapped_column(BigInteger)
    detail: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()"))
