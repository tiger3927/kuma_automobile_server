#!/usr/bin/env bash
# 建库后验收（Task-DataBase.md §9）
set -uo pipefail
cd "$(dirname "$0")"
set -a; . ../../deploy/.env; set +a

PSQL() { docker exec -i kuma-postgres psql -U kuma_owner -d kuma -tAc "$1"; }

echo "=== 1) 扩展（应恰好 4 个：postgis/pg_trgm/btree_gist/citext + plpgsql）==="
PSQL "select extname||' '||extversion from pg_extension order by extname"

echo "=== 2) 各 schema 表/视图/物化视图数量 ==="
PSQL "select n.nspname||' : '||count(*) filter (where c.relkind='r')||' 表 / '||count(*) filter (where c.relkind='v')||' 视图 / '||count(*) filter (where c.relkind='m')||' 物化视图 from pg_class c join pg_namespace n on n.oid=c.relnamespace where n.nspname in ('core','biz','tele','geo','rep','commerce','fin','mkt','mobility','audit') and c.relkind in ('r','v','m') group by n.nspname order by n.nspname"

echo "=== 3) 分区 ==="
PSQL "select count(*) from pg_class where relispartition"
PSQL "select c.relname from pg_class c join pg_inherits i on i.inhrelid=c.oid where i.inhparent='tele.telemetry'::regclass order by 1"

echo "=== 4) seed 计数 ==="
PSQL "select 'region='||count(*) from core.region"
PSQL "select 'settlement_rule='||count(*) from fin.settlement_rule"
PSQL "select 'payment_channel='||count(*) from fin.payment_channel"
PSQL "select 'dispatch_policy='||count(*) from mobility.dispatch_policy"

echo "=== 5) 权限隔离：kuma_app 改表结构必须失败 ==="
docker exec -i -e PGPASSWORD="$KUMA_APP_PASSWORD" kuma-postgres \
  psql -h 127.0.0.1 -U kuma_app -d kuma -c "alter table core.account add column hack int" 2>&1 | head -2

echo "=== 6) 权限隔离：kuma_app 删 append-only 表必须失败 ==="
docker exec -i -e PGPASSWORD="$KUMA_APP_PASSWORD" kuma-postgres \
  psql -h 127.0.0.1 -U kuma_app -d kuma -c "delete from mkt.points_ledger" 2>&1 | head -2

echo "=== 7) 权限隔离：kuma_ro 只能读 rep，写必须失败 ==="
docker exec -i -e PGPASSWORD="$KUMA_RO_PASSWORD" kuma-postgres \
  psql -h 127.0.0.1 -U kuma_ro -d kuma -c "insert into rep.sales_region_view default values" 2>&1 | head -2
docker exec -i -e PGPASSWORD="$KUMA_RO_PASSWORD" kuma-postgres \
  psql -h 127.0.0.1 -U kuma_ro -d kuma -tAc "select count(*) from rep.gov_trip_view" 2>&1 | head -2
docker exec -i -e PGPASSWORD="$KUMA_RO_PASSWORD" kuma-postgres \
  psql -h 127.0.0.1 -U kuma_ro -d kuma -tAc "select count(*) from core.account" 2>&1 | head -2

echo "=== 8) 遥测补报去重（ON CONFLICT (vin, ts) DO NOTHING）==="
docker exec -i -e PGPASSWORD="$KUMA_APP_PASSWORD" kuma-postgres psql -h 127.0.0.1 -U kuma_app -d kuma <<'SQL'
INSERT INTO tele.telemetry (vin, ts, geom, speed_kmh, soc, odo_km, seq, proto)
VALUES ('TESTVIN0000000001','2026-10-19 08:00:00+00', ST_SetSRID(ST_MakePoint(100.5,13.75),4326), 42.0, 88.0, 1234.5, 1, 'v1')
ON CONFLICT (vin, ts) DO NOTHING;
INSERT INTO tele.telemetry (vin, ts, geom, speed_kmh, soc, odo_km, seq, proto)
VALUES ('TESTVIN0000000001','2026-10-19 08:00:00+00', ST_SetSRID(ST_MakePoint(100.5,13.75),4326), 42.0, 88.0, 1234.5, 1, 'v1')
ON CONFLICT (vin, ts) DO NOTHING;
SELECT 'telemetry_rows='||count(*) FROM tele.telemetry WHERE vin='TESTVIN0000000001';
SELECT 'partition_used='||tableoid::regclass FROM tele.telemetry WHERE vin='TESTVIN0000000001' LIMIT 1;
DELETE FROM tele.telemetry WHERE vin='TESTVIN0000000001';
SQL

echo "=== 9) 分区清理演练（DROP PARTITION 秒级）==="
docker exec -i kuma-postgres psql -U kuma_owner -d kuma -c "create table tele.telemetry_probe partition of tele.telemetry for values from ('2099-01-01') to ('2099-01-02')"
docker exec -i kuma-postgres psql -U kuma_owner -d kuma -c "drop table tele.telemetry_probe"

echo "=== 10) 内存与磁盘 ==="
docker stats --no-stream --format '{{.Name}} {{.MemUsage}} {{.PIDs}}' kuma-postgres
PSQL "select pg_size_pretty(pg_database_size('kuma'))"
free -m | head -2
