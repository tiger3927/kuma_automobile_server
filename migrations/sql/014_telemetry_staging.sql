-- 014 遥测 COPY 落地的中转表（Task-Server.md §12-5：禁止逐条 INSERT）
-- 路径：asyncpg COPY → tele.telemetry_staging（UNLOGGED，无索引）
--       → INSERT ... ON CONFLICT (vin,ts) DO NOTHING → 清空中转表
-- 这样既有 COPY 的吞吐，又有 (vin,ts) 补报去重（§8.8）
-- geom 存 EWKT 文本：asyncpg COPY 没有 geometry 编码器（实测 no binary format encoder
-- for type geometry），几何化在 INSERT ... SELECT ST_GeomFromEWKT(geom) 时完成（迁移 0003）
BEGIN;

CREATE UNLOGGED TABLE IF NOT EXISTS tele.telemetry_staging (
  vin char(17) NOT NULL,
  ts timestamptz NOT NULL,
  geom text,
  speed_kmh numeric(6,2),
  soc numeric(5,2),
  odo_km numeric(10,2),
  seq bigint,
  proto text,
  recv_ts timestamptz NOT NULL DEFAULT now(),
  quality smallint NOT NULL DEFAULT 0
);

GRANT SELECT, INSERT, UPDATE, DELETE ON tele.telemetry_staging TO kuma_app;

COMMIT;
