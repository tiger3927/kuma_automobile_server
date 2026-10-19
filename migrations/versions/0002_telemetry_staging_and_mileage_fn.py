"""遥测 COPY 中转表 + 里程口径 SQL 函数

结构基线之后第一个迁移：把 `migrations/sql/014_telemetry_staging.sql` 与
`src/kuma/geo/sql/mileage.sql` 纳入迁移管理（此后一律走迁移，不再手改表）。

Revision ID: 0002_tele_staging
Revises: 0001_baseline
"""
from __future__ import annotations

from alembic import op

revision = "0002_tele_staging"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE UNLOGGED TABLE IF NOT EXISTS tele.telemetry_staging (
          vin char(17) NOT NULL,
          ts timestamptz NOT NULL,
          geom geometry(Point,4326),
          speed_kmh numeric(6,2),
          soc numeric(5,2),
          odo_km numeric(10,2),
          seq bigint,
          proto text,
          recv_ts timestamptz NOT NULL DEFAULT now(),
          quality smallint NOT NULL DEFAULT 0
        )
    """)
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON tele.telemetry_staging TO kuma_app")

    op.execute("""
        CREATE OR REPLACE FUNCTION gps_mileage_for_trip(p_trip_id bigint)
        RETURNS integer LANGUAGE sql STABLE AS $$
        WITH t AS (
          SELECT tr.vin, tr.start_ts, coalesce(tr.end_ts, now()) AS end_ts
          FROM biz.trip tr WHERE tr.id = p_trip_id
        ), pts AS (
          SELECT t0.vin, e.ts, e.geom
          FROM t t0
          JOIN tele.telemetry e ON e.vin = t0.vin AND e.ts >= t0.start_ts AND e.ts <= t0.end_ts
          WHERE e.geom IS NOT NULL
          ORDER BY e.ts
        )
        SELECT round(ST_Length(ST_Simplify(ST_MakeLine(pts.geom ORDER BY pts.ts)::geometry,
                                           0.00003)::geography))::int
        FROM pts HAVING count(*) > 1;
        $$
    """)

    op.execute("""
        CREATE OR REPLACE FUNCTION odo_mileage_for_trip(p_trip_id bigint)
        RETURNS integer LANGUAGE sql STABLE AS $$
        SELECT CASE
                 WHEN tr.start_odo_km IS NULL OR tr.end_odo_km IS NULL THEN NULL
                 WHEN tr.end_odo_km < tr.start_odo_km THEN NULL
                 ELSE round((tr.end_odo_km - tr.start_odo_km) * 1000)::int
               END
        FROM biz.trip tr WHERE tr.id = p_trip_id;
        $$
    """)

    op.execute("""
        CREATE OR REPLACE FUNCTION mileage_anomalies(p_since timestamptz)
        RETURNS TABLE (trip_id bigint, vin char(17), odo_m integer, gps_m integer, ratio numeric)
        LANGUAGE sql STABLE AS $$
        SELECT tr.id, tr.vin, m1.distance_m, m2.distance_m,
               round(abs(m2.distance_m - m1.distance_m)::numeric / nullif(m1.distance_m, 0), 4)
        FROM biz.trip tr
        JOIN biz.trip_mileage m1 ON m1.trip_id = tr.id AND m1.method = 'odo'
        JOIN biz.trip_mileage m2 ON m2.trip_id = tr.id AND m2.method = 'gps'
        WHERE tr.start_ts >= p_since AND m1.distance_m > 0
          AND abs(m2.distance_m - m1.distance_m)::numeric / m1.distance_m > 0.15;
        $$
    """)


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS mileage_anomalies(timestamptz)")
    op.execute("DROP FUNCTION IF EXISTS odo_mileage_for_trip(bigint)")
    op.execute("DROP FUNCTION IF EXISTS gps_mileage_for_trip(bigint)")
    op.execute("DROP TABLE IF EXISTS tele.telemetry_staging")
