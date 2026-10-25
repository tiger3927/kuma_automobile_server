"""补 `rep` 里 009 那 4 个对象的 `kuma_app` 授权（`GET /fleet/stats` 500 的根因）

线上实测（2026-10-26，`pg_class.relacl` / `pg_default_acl`）：

    telemetry_hourly(m)   kuma_owner=arwdDxt , kuma_ro=r    ← 缺 kuma_app
    fleet_snapshot(v)     kuma_owner=arwdDxt , kuma_ro=r    ← 缺 kuma_app
    gov_trip_view(v)      kuma_owner=arwdDxt , kuma_ro=r    ← 缺 kuma_app
    sales_region_view(v)  kuma_owner=arwdDxt , kuma_ro=r    ← 缺 kuma_app

**根因不是"物化视图不被默认权限覆盖"**（我一开始这么判断，是错的）：`pg_default_acl`
里 objtype=`r` 这一类**包含表、视图、物化视图、外部表**。真正的原因是 `rep` 的默认权限
只授给了 `kuma_ro`，压根没有 `kuma_app`：

    kuma_owner | rep | r | kuma_ro=r/kuma_owner

0006/0007 建的 7 个视图之所以能读，是因为迁移里**逐条显式**授权
（`0006_alarm_rules_cred_delivery_gov_views.py:130`、`0007_energy_swap_domain.py:542`）。
而 `migrations/sql/009_rep_views.sql` 是一条 GRANT 都没有的纯 DDL，于是它建的 4 个对象
只有 `kuma_ro` 能读。应用进程连的是 `kuma_app`，所以：

- `GET /fleet/stats` → `permission denied for materialized view telemetry_hourly`（实测 500）
- `GET /gov/trips` 与销售按地区汇总同样会 500（读 `gov_trip_view` / `sales_region_view`）

第二件事顺手补掉：这 4 个对象**只存在于 `migrations/sql/009_rep_views.sql`**，alembic 链里
没有任何 `MATERIALIZED VIEW`（`0001_baseline.py` 里 `rep.` 出现 0 次）——从 alembic 重建库时
它们根本不存在，`fleet/stats` 会直接报错而不是 500。这里按 009 的定义幂等补齐，
让"线上增量"与"重建库"两条路都得到对象。**定义必须与 009 逐字一致**，否则两边漂移。

只给 SELECT：`REFRESH MATERIALIZED VIEW ... CONCURRENTLY`（`telemetry_repo.py:122`）
需要 owner 或 PG16 的 MAINTAIN，`kuma_app` 拿不到也不该拿到——刷新走 worker 的 owner 连接。
"""

from alembic import op

revision = "0011_rep_grants"
down_revision = "0010_kyc_media_org_contact"
branch_labels = None
depends_on = None

# 009 里那 4 个对象（本迁移只碰它们，不动 0006/0007 已授权好的视图）
REP_OBJECTS = (
    "rep.telemetry_hourly",
    "rep.fleet_snapshot",
    "rep.gov_trip_view",
    "rep.sales_region_view",
)


def upgrade() -> None:
    # ── 1. 幂等补齐对象（定义与 migrations/sql/009_rep_views.sql 一致）──────
    op.execute("""
        CREATE MATERIALIZED VIEW IF NOT EXISTS rep.telemetry_hourly AS
        SELECT vin,
               date_trunc('hour', ts) AS hour,
               count(*)               AS points,
               avg(speed_kmh)         AS avg_speed_kmh,
               max(speed_kmh)         AS max_speed_kmh,
               avg(soc)               AS avg_soc,
               min(odo_km)            AS odo_min_km,
               max(odo_km)            AS odo_max_km
        FROM tele.telemetry
        GROUP BY vin, date_trunc('hour', ts);
    """)
    # CONCURRENTLY 刷新必须有唯一索引，缺了 worker 每小时刷新就会失败
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_rep_hourly ON rep.telemetry_hourly (vin, hour);")

    op.execute("""
        CREATE OR REPLACE VIEW rep.fleet_snapshot AS
        SELECT DISTINCT ON (t.vin)
               t.vin, t.ts, t.speed_kmh, t.soc, t.odo_km, t.geom,
               v.status AS vehicle_status, v.region_id, v.cur_dealer_org_id, v.owner_type,
               v.owner_account_id, v.owner_org_id
        FROM tele.telemetry t
        JOIN core.vehicle v ON v.vin = t.vin
        ORDER BY t.vin, t.ts DESC;
    """)
    op.execute("""
        CREATE OR REPLACE VIEW rep.gov_trip_view AS
        SELECT tr.id AS trip_id, tr.vin, v.plate, v.type_code, v.region_id,
               tr.start_ts, tr.end_ts, tr.start_odo_km, tr.end_odo_km,
               m.distance_m AS odo_distance_m,
               tr.driver_account_id, tr.operator_org_id, o.name AS operator_name
        FROM biz.trip tr
        JOIN biz.trip_mileage m ON m.trip_id = tr.id AND m.method = 'odo'
        JOIN core.vehicle v ON v.vin = tr.vin
        LEFT JOIN core.org o ON o.id = tr.operator_org_id;
    """)
    op.execute("""
        CREATE OR REPLACE VIEW rep.sales_region_view AS
        SELECT ord.region_id, ord.currency, ord.order_type, ord.collect_mode,
               count(*) AS orders, sum(ord.amount_cash) AS cash_sum, sum(ord.amount_points) AS points_sum
        FROM commerce."order" ord
        WHERE ord.status NOT IN ('cancelled')
        GROUP BY ord.region_id, ord.currency, ord.order_type, ord.collect_mode;
    """)

    # ── 2. 补那 4 个对象的授权（存量修复）───────────────────────────────
    op.execute(f"GRANT SELECT ON {', '.join(REP_OBJECTS)} TO kuma_app")

    # ── 3. 根治：rep 的默认权限补上 kuma_app，以后新建视图/matview 不再漏 ────
    # objtype 'r' 覆盖表 / 视图 / 物化视图 / 外部表，所以一条就够（不需要 ON VIEWS 之类，
    # PG 也没有那个类别）。rep 是只读报表 schema，kuma_app 读它本就是设计内
    # （0006/0007 已对 7 个视图显式授权），这里只是把"默认"补齐。
    op.execute("ALTER DEFAULT PRIVILEGES FOR ROLE kuma_owner IN SCHEMA rep GRANT SELECT ON TABLES TO kuma_app")


def downgrade() -> None:
    # 只撤销本迁移引入的东西；**不 DROP 对象**——它们在 009 里创建，早于本迁移就存在于线上。
    op.execute("ALTER DEFAULT PRIVILEGES FOR ROLE kuma_owner IN SCHEMA rep REVOKE ALL ON TABLES FROM kuma_app")
    op.execute(f"REVOKE SELECT ON {', '.join(REP_OBJECTS)} FROM kuma_app")
