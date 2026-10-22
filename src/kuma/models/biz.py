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
    """告警。状态机（2026-10-22 定，`status` 列无 CHECK，取值域由代码与文档约束）：

        open ──ack──> ack ──close──> closed
          │              ↑              │
          ├──silence──> silenced ──────┤      snooze_until 到期由 worker 自动回 open
          └──close────────────────────>┘      reopen: closed → open（必须带 reason，留审计）
        auto_closed：系统自动关（如 offline 告警在车辆恢复上报时）。

    `snooze_until` 只对被静默的告警有意义；它同时是"同类新告警不再新建"的抑制依据
    （见 `AlarmRepo.raise_alarm` 的 NOT EXISTS）。
    """

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
    snooze_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()"))


class AlarmRule(Base):
    """告警阈值配置（迁移 0006）。此前阈值硬编码在 `tasks/alarm_rules.py:20-21`
    （`SPEED_LIMIT_KMH=80` / `HARSH_BRAKE_DROP=25`），改一次要发一次版。

    `region_code` 为 NULL = 全局默认；非 NULL = 该地区覆盖（§12.1 禁止代码写死国家，
    阈值属于地区差异——不同国家限速不同，必须能配）。
    """

    __tablename__ = "alarm_rule"
    __table_args__ = (
        UniqueConstraint("type", "region_code"),
        {"schema": "biz"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    type: Mapped[str] = mapped_column(Text, nullable=False)
    region_code: Mapped[str | None] = mapped_column(CHAR(2), ForeignKey("core.region.country_code"))
    enabled: Mapped[bool] = mapped_column(nullable=False, default=True)
    level: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=2)
    params: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    dedupe_window_s: Mapped[int] = mapped_column(nullable=False, default=600)
    updated_at: Mapped[datetime] = mapped_column(
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
