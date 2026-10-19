"""遥测中转表 geom 改为 text（asyncpg COPY 无 geometry 编码器）

实测：`copy_records_to_table` 对 PostGIS geometry 抛
`no binary format encoder for type geometry (OID 18056)`。
COPY 只搬 EWKT 文本，几何化推迟到 `INSERT ... SELECT ST_GeomFromEWKT(geom)`，
顺带让中转表与 asyncpg 无类型耦合（换 ClickHouse/Timescale 也只改一个 repo）。

Revision ID: 0003_staging_geom_text
Revises: 0002_tele_staging
"""
from __future__ import annotations

from alembic import op

revision = "0003_staging_geom_text"
down_revision = "0002_tele_staging"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE tele.telemetry_staging
          ALTER COLUMN geom TYPE text USING ST_AsEWKT(geom)
    """)


def downgrade() -> None:
    op.execute("""
        ALTER TABLE tele.telemetry_staging
          ALTER COLUMN geom TYPE geometry(Point,4326)
          USING CASE WHEN geom IS NULL OR geom = '' THEN NULL
                     ELSE ST_SetSRID(ST_GeomFromEWKT(geom), 4326) END
    """)
