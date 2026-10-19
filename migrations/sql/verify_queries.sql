-- 建库后核对（用 psql -f 执行，避免 shell 引号问题）
\set ON_ERROR_STOP off

SELECT '1 扩展' AS chk, string_agg(extname, ', ' ORDER BY extname) AS result
FROM pg_extension;

SELECT '2 对象数' AS chk,
       n.nspname || ' = ' ||
       count(*) FILTER (WHERE c.relkind = 'r') || ' 表 / ' ||
       count(*) FILTER (WHERE c.relkind IN ('v','m')) || ' 视图' AS result
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname IN ('core','biz','tele','geo','rep','commerce','fin','mkt','mobility','audit')
  AND c.relkind IN ('r','v','m')
GROUP BY n.nspname ORDER BY n.nspname;

SELECT '3 分区数' AS chk, count(*)::text AS result FROM pg_class WHERE relispartition;

SELECT '4 seed' AS chk,
  (SELECT count(*) FROM core.region)::text          AS region,
  (SELECT count(*) FROM fin.settlement_rule)::text  AS rules,
  (SELECT count(*) FROM fin.payment_channel)::text  AS channels,
  (SELECT count(*) FROM mobility.dispatch_policy)::text AS policies;

-- 5 遥测补报去重（同一 (vin,ts) 插两次只留一行）
INSERT INTO tele.telemetry (vin, ts, geom, speed_kmh, soc, odo_km, seq, proto)
VALUES ('TESTVIN0000000001','2026-10-19 08:00:00+00',
        ST_GeomFromText('POINT(100.5 13.75)',4326), 42.0, 88.0, 1234.5, 1, 'v1')
ON CONFLICT (vin, ts) DO NOTHING;

INSERT INTO tele.telemetry (vin, ts, geom, speed_kmh, soc, odo_km, seq, proto)
VALUES ('TESTVIN0000000001','2026-10-19 08:00:00+00',
        ST_GeomFromText('POINT(100.5 13.75)',4326), 42.0, 88.0, 1234.5, 1, 'v1')
ON CONFLICT (vin, ts) DO NOTHING;

SELECT '5 去重' AS chk,
       (SELECT count(*) FROM tele.telemetry WHERE vin = 'TESTVIN0000000001')::text AS rows_kept,
       (SELECT tableoid::regclass::text FROM tele.telemetry WHERE vin = 'TESTVIN0000000001' LIMIT 1) AS partition_hit;

DELETE FROM tele.telemetry WHERE vin = 'TESTVIN0000000001';

-- 6 规则表兜底行（region/type 为 NULL）不可重复
INSERT INTO fin.settlement_rule (rule_type, subject, region_id, type_code, ratio, version, effective_from, status)
VALUES ('commission','vehicle',NULL,NULL,'{"platform":0.02}'::jsonb, 1, DATE '2026-01-01','active')
ON CONFLICT DO NOTHING;
SELECT '6 兜底规则去重' AS chk, count(*)::text AS vehicle_rules
FROM fin.settlement_rule WHERE rule_type = 'commission' AND subject = 'vehicle';

SELECT '7 库大小' AS chk, pg_size_pretty(pg_database_size('kuma')) AS result;
