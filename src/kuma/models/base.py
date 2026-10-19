"""ORM 基类。

表结构以 `migrations/sql/*.sql`（= `Task-DataBase.md` §11）为准，模型只做映射，**不做建表**：
Alembic 以现有结构为 baseline（`alembic stamp head`），此后结构改动一律走迁移。
"""

from __future__ import annotations

from sqlalchemy import Text, text
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


class Geometry(Text):
    """PostGIS geometry 的轻量映射：写入接受 WKT（'SRID=4326;POINT(lon lat)'）。

    刻意不引入 geoalchemy2（避免扩大依赖面）；空间计算一律在 SQL 里用 PostGIS 函数。
    """

    __visit_name__ = "TEXT"

    def get_col_spec(self, **kw) -> str:
        return "geometry"


NOW = text("now()")
