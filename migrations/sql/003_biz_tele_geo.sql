-- 003 biz / tele / geo（车联网侧，依 Task-DataBase.md §5.2 §5.3、Task-Server.md §11）
BEGIN;

-- ---------- biz ----------
CREATE TABLE biz.trip (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  vin char(17) NOT NULL REFERENCES core.vehicle (vin),
  owner_type text NOT NULL CHECK (owner_type IN ('account','org')),
  owner_account_id bigint REFERENCES core.account,
  owner_org_id bigint REFERENCES core.org,
  driver_account_id bigint REFERENCES core.account,
  operator_org_id bigint REFERENCES core.org,        -- 网约车行程才有（政府上报与分账用）
  region_id char(2) REFERENCES core.region,
  start_ts timestamptz NOT NULL,
  end_ts timestamptz,
  start_odo_km int, end_odo_km int,
  status text NOT NULL DEFAULT 'active',             -- active|completed|anomaly
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (vin, start_ts)
);
CREATE INDEX idx_trip_owner_account ON biz.trip (owner_account_id, start_ts DESC);
CREATE INDEX idx_trip_owner_org ON biz.trip (owner_org_id, start_ts DESC);
CREATE INDEX idx_trip_vin_start ON biz.trip (vin, start_ts DESC);
CREATE INDEX idx_trip_operator_region ON biz.trip (operator_org_id, region_id, start_ts DESC);

CREATE TABLE biz.trip_mileage (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  trip_id bigint NOT NULL REFERENCES biz.trip,
  method text NOT NULL CHECK (method IN ('odo','gps','road')),
  distance_m int NOT NULL,
  confidence numeric(4,3),
  flags jsonb NOT NULL DEFAULT '{}',                 -- pricing_fallback / anomaly 标记
  computed_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (trip_id, method)
);

CREATE TABLE biz.alarm (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  vin char(17) NOT NULL REFERENCES core.vehicle (vin),
  ts timestamptz NOT NULL,
  type text NOT NULL,                                -- overspeed|harsh_brake|fence_exit|idle|night_abnormal…
  level smallint NOT NULL DEFAULT 2,
  payload jsonb NOT NULL DEFAULT '{}',
  status text NOT NULL DEFAULT 'open',               -- open|ack|auto_closed
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (vin, ts, type)
);
CREATE INDEX idx_alarm_status_ts ON biz.alarm (status, ts DESC);
CREATE INDEX idx_alarm_vin_ts ON biz.alarm (vin, ts DESC);

CREATE TABLE biz.alarm_ack (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  alarm_id bigint NOT NULL REFERENCES biz.alarm,
  ack_by bigint NOT NULL REFERENCES core.account,
  ack_at timestamptz NOT NULL DEFAULT now(),
  note text
);
CREATE INDEX idx_alarm_ack_alarm ON biz.alarm_ack (alarm_id);

CREATE TABLE biz.command (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  cmd_id text UNIQUE NOT NULL,
  vin char(17) NOT NULL REFERENCES core.vehicle (vin),
  cmd_type text NOT NULL,
  payload jsonb NOT NULL DEFAULT '{}',
  status text NOT NULL DEFAULT 'pending',            -- pending|sent|acked|timeout|failed
  attempt smallint NOT NULL DEFAULT 0,
  created_by bigint REFERENCES core.account,
  created_at timestamptz NOT NULL DEFAULT now(),
  sent_at timestamptz, acked_at timestamptz
);
CREATE INDEX idx_command_vin_status ON biz.command (vin, status);

CREATE TABLE biz.command_audit (                     -- append-only，永久保留
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  cmd_id text NOT NULL,
  attempt smallint NOT NULL,
  event text NOT NULL,
  actor_account_id bigint,
  detail jsonb NOT NULL DEFAULT '{}',
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_command_audit_cmd ON biz.command_audit (cmd_id, created_at);

-- ---------- tele（试点按天分区；清理=DROP PARTITION，禁止 DELETE）----------
CREATE TABLE tele.telemetry (
  vin char(17) NOT NULL,
  ts timestamptz NOT NULL,
  geom geometry(Point,4326),
  speed_kmh numeric(6,2),
  soc numeric(5,2),
  odo_km numeric(10,2),
  seq bigint,
  proto text,
  recv_ts timestamptz NOT NULL DEFAULT now(),
  quality smallint NOT NULL DEFAULT 0,
  PRIMARY KEY (vin, ts)                              -- 支撑补报去重 ON CONFLICT (vin, ts) DO NOTHING
) PARTITION BY RANGE (ts);
-- 不建空间索引（写放大太贵，§5.3）
CREATE INDEX idx_tele_vin_ts ON tele.telemetry (vin, ts DESC);

-- 试点首批分区（worker 提前预建下一周期；不装 pg_partman）
CREATE TABLE tele.telemetry_20261019 PARTITION OF tele.telemetry FOR VALUES FROM ('2026-10-19') TO ('2026-10-20');
CREATE TABLE tele.telemetry_20261020 PARTITION OF tele.telemetry FOR VALUES FROM ('2026-10-20') TO ('2026-10-21');
CREATE TABLE tele.telemetry_20261021 PARTITION OF tele.telemetry FOR VALUES FROM ('2026-10-21') TO ('2026-10-22');
CREATE TABLE tele.telemetry_20261022 PARTITION OF tele.telemetry FOR VALUES FROM ('2026-10-22') TO ('2026-10-23');
CREATE TABLE tele.telemetry_20261023 PARTITION OF tele.telemetry FOR VALUES FROM ('2026-10-23') TO ('2026-10-24');
CREATE TABLE tele.telemetry_20261024 PARTITION OF tele.telemetry FOR VALUES FROM ('2026-10-24') TO ('2026-10-25');
CREATE TABLE tele.telemetry_20261025 PARTITION OF tele.telemetry FOR VALUES FROM ('2026-10-25') TO ('2026-10-26');

-- ---------- geo ----------
CREATE TABLE geo.fence (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  name text NOT NULL,
  fence_type text NOT NULL CHECK (fence_type IN ('operating_area','no_go','alert_zone')),
  geom geometry(Polygon,4326) NOT NULL,
  org_id bigint REFERENCES core.org,                 -- 运营商获准经营区域等
  region_id char(2) REFERENCES core.region,
  active_from timestamptz, active_to timestamptz,
  status text NOT NULL DEFAULT 'active',
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_fence_geom ON geo.fence USING gist (geom);
CREATE INDEX idx_fence_org ON geo.fence (org_id, status);

COMMIT;
