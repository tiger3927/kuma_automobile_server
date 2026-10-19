-- 009 rep：只读视图与物化视图（kuma_ro 唯一可访问，§5.2）
BEGIN;

-- 小时聚合（worker 每小时 REFRESH ... CONCURRENTLY，故必须唯一索引）
CREATE MATERIALIZED VIEW rep.telemetry_hourly AS
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

CREATE UNIQUE INDEX ux_rep_hourly ON rep.telemetry_hourly (vin, hour);

-- 车队最新状态快照（试点量级可用；生产走 Redis 状态，Task-Server.md §12.9）
CREATE VIEW rep.fleet_snapshot AS
SELECT DISTINCT ON (t.vin)
       t.vin, t.ts, t.speed_kmh, t.soc, t.odo_km, t.geom,
       v.status AS vehicle_status, v.region_id, v.cur_dealer_org_id, v.owner_type,
       v.owner_account_id, v.owner_org_id
FROM tele.telemetry t
JOIN core.vehicle v ON v.vin = t.vin
ORDER BY t.vin, t.ts DESC;

-- 政府/结算报表视图：只读 odo 口径（Task-Server.md §11）
CREATE VIEW rep.gov_trip_view AS
SELECT tr.id AS trip_id, tr.vin, v.plate, v.type_code, v.region_id,
       tr.start_ts, tr.end_ts, tr.start_odo_km, tr.end_odo_km,
       m.distance_m AS odo_distance_m,
       tr.driver_account_id, tr.operator_org_id, o.name AS operator_name
FROM biz.trip tr
JOIN biz.trip_mileage m ON m.trip_id = tr.id AND m.method = 'odo'
JOIN core.vehicle v ON v.vin = tr.vin
LEFT JOIN core.org o ON o.id = tr.operator_org_id;

-- 商业看板视图（带 region 维度，三国同一视图分组，§5.4）
CREATE VIEW rep.sales_region_view AS
SELECT ord.region_id, ord.currency, ord.order_type, ord.collect_mode,
       count(*) AS orders, sum(ord.amount_cash) AS cash_sum, sum(ord.amount_points) AS points_sum
FROM commerce."order" ord
WHERE ord.status NOT IN ('cancelled')
GROUP BY ord.region_id, ord.currency, ord.order_type, ord.collect_mode;

COMMIT;
