"""`geo` schema：电子围栏（运营商获准经营区域、禁行区、告警区）。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CHAR, BigInteger, DateTime, ForeignKey, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, Geometry


class Fence(Base):
    __tablename__ = "fence"
    __table_args__ = {"schema": "geo"}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    fence_type: Mapped[str] = mapped_column(Text, nullable=False)
    geom: Mapped[str] = mapped_column(Geometry, nullable=False)
    org_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("core.org.id"))
    region_id: Mapped[str | None] = mapped_column(CHAR(2), ForeignKey("core.region.country_code"))
    active_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    active_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(Text, nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()"))
