"""修正角色级 search_path（引号 bug + 缺 public）

`001_roles_schemas.sql` 里写的是 `SET search_path = 'core, biz, ...'`：
单引号让整串变成**一个**带逗号的标识符，实测 kuma_app 的 search_path 里
根本没有 public，于是 `ST_GeomFromEWKT` 之类 PostGIS 函数解析不到
（function st_geomfromewkt(text) does not exist）。

正确写法是不加引号的逗号列表，并且必须带 public（PostGIS/pg_trgm 等扩展装在 public）。

Revision ID: 0004_search_path
Revises: 0003_staging_geom_text
"""
from __future__ import annotations

from alembic import op

revision = "0004_search_path"
down_revision = "0003_staging_geom_text"
branch_labels = None
depends_on = None

APP_PATH = "core, biz, tele, geo, rep, commerce, fin, mkt, mobility, audit, public"


def upgrade() -> None:
    op.execute(f"ALTER ROLE kuma_app IN DATABASE kuma SET search_path = {APP_PATH}")
    op.execute("ALTER ROLE kuma_ro IN DATABASE kuma SET search_path = rep, public")


def downgrade() -> None:
    op.execute("ALTER ROLE kuma_app IN DATABASE kuma RESET search_path")
    op.execute("ALTER ROLE kuma_ro IN DATABASE kuma RESET search_path")
