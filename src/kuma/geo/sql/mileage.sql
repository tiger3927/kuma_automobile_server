-- 里程口径的 SQL 侧（与 src/kuma/geo/mileage.py 同处一地，Task-Server.md §11）
-- 用途：worker 批量重算历史行程 gps 口径；Python 侧只处理单行程。

-- 1) 单行程 gps 口径（按 ts 排序连线，先按 3m 容差抽稀）
--    用法：SELECT gps_mileage_for_trip(:trip_id);
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
SELECT round(ST_Length(
         ST_Simplify(
           ST_MakeLine(pts.geom ORDER BY pts.ts)::geometry, 0.00003
         )::geography))::int
FROM pts
HAVING count(*) > 1;
$$;

-- 2) odo 口径（结算与政府上报唯一主口径）
CREATE OR REPLACE FUNCTION odo_mileage_for_trip(p_trip_id bigint)
RETURNS integer LANGUAGE sql STABLE AS $$
SELECT CASE
         WHEN tr.start_odo_km IS NULL OR tr.end_odo_km IS NULL THEN NULL
         WHEN tr.end_odo_km < tr.start_odo_km THEN NULL
         ELSE round((tr.end_odo_km - tr.start_odo_km) * 1000)::int
       END
FROM biz.trip tr WHERE tr.id = p_trip_id;
$$;

-- 3) 差异 > 15% 的行程（mileage_anomaly 告警来源）
CREATE OR REPLACE FUNCTION mileage_anomalies(p_since timestamptz)
RETURNS TABLE (trip_id bigint, vin char(17), odo_m integer, gps_m integer, ratio numeric)
LANGUAGE sql STABLE AS $$
SELECT tr.id, tr.vin,
       m1.distance_m, m2.distance_m,
       round(abs(m2.distance_m - m1.distance_m)::numeric / nullif(m1.distance_m, 0), 4)
FROM biz.trip tr
JOIN biz.trip_mileage m1 ON m1.trip_id = tr.id AND m1.method = 'odo'
JOIN biz.trip_mileage m2 ON m2.trip_id = tr.id AND m2.method = 'gps'
WHERE tr.start_ts >= p_since AND m1.distance_m > 0
  AND abs(m2.distance_m - m1.distance_m)::numeric / m1.distance_m > 0.15;
$$;
