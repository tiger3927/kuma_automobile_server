"""baseline：认领 2026-10-19 由 `migrations/sql/001`–`014` 建立的结构。

本迁移**不做任何 DDL**——结构已由 SQL 脚本落地并验收（Task-DataBase.md §9 实测记录）。
用法：`KUMA_MIGRATE_DSN=... alembic stamp head`（或 `alembic upgrade head`，等价空操作）。
此后所有结构改动必须新增迁移，禁止手改表。
"""
from __future__ import annotations

from alembic import op  # noqa: F401  保留以符合迁移契约

revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """空操作：基线由 SQL 脚本建立。"""


def downgrade() -> None:
    raise RuntimeError("基线不可回滚（回滚等于删库，请走备份恢复）")
