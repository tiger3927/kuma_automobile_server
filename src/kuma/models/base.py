"""ORM 基类。

表结构以 `migrations/sql/*.sql`（= `Task-DataBase.md` §11）为准，模型只做映射，**不做建表**：
Alembic 以现有结构为 baseline（`alembic stamp head`），此后结构改动一律走迁移。
"""

from __future__ import annotations

from sqlalchemy import Text, TypeDecorator, func, text
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


class Geometry(TypeDecorator):
    """PostGIS geometry 的轻量映射：写入接受 EWKT（'SRID=4326;POINT(lon lat)'）。

    刻意不引入 geoalchemy2（避免扩大依赖面）；空间计算一律在 SQL 里用 PostGIS 函数。

    ⚠️ `bind_expression` 不是可选的：asyncpg 会把绑定参数显式标成 `$1::VARCHAR`，
    直接写进 geometry 列会报 `column "geom" is of type geometry but expression is of
    type character varying`（2026-10-20 跑 seed_demo 时才暴露；psycopg2 时代靠隐式转换侥幸能写）。
    读侧不受影响——本项目的空间查询都走 `ST_*` 函数，从不 select 裸 geom 列。
    """

    impl = Text
    cache_ok = True

    __visit_name__ = "TEXT"

    def get_col_spec(self, **kw) -> str:
        return "geometry"

    def bind_expression(self, bindvalue):
        return func.ST_GeomFromEWKT(bindvalue)


NOW = text("now()")
