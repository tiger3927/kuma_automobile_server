"""`tele` schema：遥测（按天 RANGE 分区，主键 (vin, ts) 支撑补报去重）。

写入**只经 `repositories/telemetry_repo.py`**（asyncpg COPY → staging → ON CONFLICT，
Task-Server.md §7 铁律 4 / §12-5）。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CHAR, BigInteger, DateTime, Numeric, SmallInteger, Text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, Geometry


class Telemetry(Base):
    __tablename__ = "telemetry"
    __table_args__ = {"schema": "tele"}

    vin: Mapped[str] = mapped_column(CHAR(17), primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    geom: Mapped[str | None] = mapped_column(Geometry)
    speed_kmh: Mapped[float | None] = mapped_column(Numeric(6, 2))
    soc: Mapped[float | None] = mapped_column(Numeric(5, 2))
    odo_km: Mapped[float | None] = mapped_column(Numeric(10, 2))
    seq: Mapped[int | None] = mapped_column(BigInteger)
    proto: Mapped[str | None] = mapped_column(Text)
    recv_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    quality: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
