"""给 kuma_app 补 tele.telemetry_staging 的 TRUNCATE 权限

`telemetry_repo.flush()` 收尾要 `TRUNCATE tele.telemetry_staging`（比 DELETE 快、不产生死元组）。
PG 16 起 TRUNCATE 是独立权限（`GRANT ... , TRUNCATE`），0002 只给了 SELECT/INSERT/UPDATE/DELETE，
实测报 `permission denied for table telemetry_staging`。
中转表是 UNLOGGED 的临时缓冲，给 TRUNCATE 不扩大数据面（表里不保留历史）。

Revision ID: 0005_truncate_staging
Revises: 0004_search_path
"""
from __future__ import annotations

from alembic import op

revision = "0005_truncate_staging"
down_revision = "0004_search_path"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("GRANT TRUNCATE ON tele.telemetry_staging TO kuma_app")


def downgrade() -> None:
    op.execute("REVOKE TRUNCATE ON tele.telemetry_staging FROM kuma_app")
