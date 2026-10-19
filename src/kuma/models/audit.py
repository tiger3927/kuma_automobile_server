"""`audit` schema：审计日志（按月分区、append-only）与 outbox（跨域一致性）。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, SmallInteger, Text, text
from sqlalchemy.dialects.postgresql import INET, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class AuditLog(Base):
    """对 `kuma_app` 只开放 INSERT（`010_grants_appendonly.sql`）——ORM 里不要 update/delete。"""

    __tablename__ = "audit_log"
    __table_args__ = {"schema": "audit"}

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), primary_key=True, server_default=text("now()"))
    actor_account_id: Mapped[int | None] = mapped_column(BigInteger)
    actor_identity: Mapped[str | None] = mapped_column(Text)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    resource_type: Mapped[str] = mapped_column(Text, nullable=False)
    resource_id: Mapped[str | None] = mapped_column(Text)
    before: Mapped[dict | None] = mapped_column(JSONB)
    after: Mapped[dict | None] = mapped_column(JSONB)
    query: Mapped[dict | None] = mapped_column(JSONB)   # 监管查询条件必存
    ip: Mapped[str | None] = mapped_column(INET)


class Outbox(Base):
    __tablename__ = "outbox"
    __table_args__ = {"schema": "audit"}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    aggregate_type: Mapped[str] = mapped_column(Text, nullable=False)
    aggregate_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    retry_count: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()"))
