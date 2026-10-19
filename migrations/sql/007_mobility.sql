-- 007 mobility：司机、合同、计价、派单、打车、拼车（14 表，Task-DataBase.md §11.5）
BEGIN;

CREATE TABLE mobility.driver_profile (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  account_id bigint UNIQUE NOT NULL REFERENCES core.account,
  license_no text, license_expiry date,
  region_id char(2) NOT NULL REFERENCES core.region,
  status text NOT NULL DEFAULT 'none',               -- none|applied|active|suspended|revoked
  score numeric(4,2) DEFAULT 5.0, total_trips int NOT NULL DEFAULT 0,
  online_status boolean NOT NULL DEFAULT false,
  last_dispatch_ts timestamptz,                      -- 公平性轮候
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_driver_region_status ON mobility.driver_profile (region_id, status);

CREATE TABLE mobility.driver_application (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  account_id bigint NOT NULL REFERENCES core.account,
  operator_org_id bigint NOT NULL REFERENCES core.org,
  region_id char(2) NOT NULL REFERENCES core.region,
  answers jsonb NOT NULL, docs jsonb,
  status text CHECK (status IN ('submitted','reviewing','approved','rejected')),
  reviewed_by bigint REFERENCES core.account, reviewed_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_driver_app_status ON mobility.driver_application (operator_org_id, status);

CREATE TABLE mobility.operator_binding (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  driver_profile_id bigint NOT NULL REFERENCES mobility.driver_profile,
  operator_org_id bigint NOT NULL REFERENCES core.org,
  status text NOT NULL DEFAULT 'active',
  effective_from timestamptz, ended_at timestamptz
);
CREATE UNIQUE INDEX uq_driver_active ON mobility.operator_binding (driver_profile_id)
  WHERE status = 'active';                           -- 司机不得双归属
CREATE INDEX idx_binding_operator ON mobility.operator_binding (operator_org_id, status);

CREATE TABLE mobility.fleet_contract (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  contract_no text UNIQUE NOT NULL,
  contract_type text CHECK (contract_type IN ('asset','franchise')),
  operator_org_id bigint NOT NULL REFERENCES core.org,
  vehicle_id bigint NOT NULL REFERENCES core.vehicle,
  owner_account_id bigint REFERENCES core.account,   -- franchise 必填
  driver_account_id bigint NOT NULL REFERENCES core.account,
  terms_version int NOT NULL,
  effective_from date NOT NULL, expires_at date,
  status text NOT NULL DEFAULT 'active',
  CHECK ((contract_type = 'franchise' AND owner_account_id IS NOT NULL)
      OR (contract_type = 'asset' AND owner_account_id IS NULL))
);
CREATE UNIQUE INDEX uq_vehicle_contract ON mobility.fleet_contract (vehicle_id)
  WHERE status = 'active';                           -- 一车一份 active 合同
CREATE INDEX idx_contract_operator ON mobility.fleet_contract (operator_org_id, status);
CREATE INDEX idx_contract_driver ON mobility.fleet_contract (driver_account_id, status);

CREATE TABLE mobility.vehicle_operation_perm (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  vehicle_id bigint NOT NULL REFERENCES core.vehicle,
  region_id char(2) NOT NULL REFERENCES core.region,
  permit_no text NOT NULL, permit_expiry date NOT NULL,
  status text NOT NULL DEFAULT 'active'
);
CREATE INDEX idx_vehperm_vehicle ON mobility.vehicle_operation_perm (vehicle_id, status);
CREATE INDEX idx_vehperm_expiry ON mobility.vehicle_operation_perm (permit_expiry) WHERE status = 'active';

CREATE TABLE mobility.tariff (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  region_id char(2) NOT NULL REFERENCES core.region,
  type_code text NOT NULL REFERENCES core.vehicle_type,
  version int NOT NULL,
  base_fare bigint NOT NULL, per_km bigint, per_min bigint, wait_per_min bigint,
  min_fare bigint, luggage_fee bigint, cargo_fee bigint,
  night jsonb, peak jsonb,
  currency char(3) NOT NULL,
  status text CHECK (status IN ('draft','pending_review','active','retired')),
  submitted_by bigint REFERENCES core.account, approved_by bigint REFERENCES core.account,
  effective_from timestamptz,
  UNIQUE (region_id, type_code, version)
);
CREATE INDEX idx_tariff_active ON mobility.tariff (region_id, type_code) WHERE status = 'active';

CREATE TABLE mobility.dispatch_policy (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  region_id char(2) NOT NULL REFERENCES core.region,
  version int NOT NULL,
  hard_rules jsonb NOT NULL, weights jsonb NOT NULL,
  offer_timeout_s smallint NOT NULL DEFAULT 15, max_retry smallint DEFAULT 3,
  enabled boolean NOT NULL DEFAULT true,
  created_by bigint REFERENCES core.account,
  status text NOT NULL DEFAULT 'active',
  UNIQUE (region_id, version)
);

CREATE TABLE mobility.ride_order (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  ride_no text UNIQUE NOT NULL,
  passenger_account_id bigint NOT NULL REFERENCES core.account,
  region_id char(2) NOT NULL REFERENCES core.region,
  type_code text NOT NULL REFERENCES core.vehicle_type,
  pool_id bigint,                                    -- 拼车时非空（007 末尾挂 FK）
  seats_requested smallint NOT NULL DEFAULT 1,
  pickup geometry(Point,4326), dropoff geometry(Point,4326),
  pickup_addr text, dropoff_addr text,
  driver_account_id bigint REFERENCES core.account,
  vehicle_id bigint REFERENCES core.vehicle,
  status text NOT NULL,
  trip_id bigint REFERENCES biz.trip,                -- 复用车联网行程，不建第二张轨迹表
  tariff_id bigint NOT NULL REFERENCES mobility.tariff,
  pricing jsonb,
  amount bigint, points_offset bigint NOT NULL DEFAULT 0, cash_amount bigint,
  currency char(3) NOT NULL,
  cancel_reason text, cancel_by text,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_ride_pickup_geom ON mobility.ride_order USING gist (pickup);
CREATE INDEX idx_ride_matching ON mobility.ride_order (region_id, status)
  WHERE status IN ('requested','matching','offered');
CREATE INDEX idx_ride_passenger ON mobility.ride_order (passenger_account_id, created_at DESC);
CREATE INDEX idx_ride_driver ON mobility.ride_order (driver_account_id, created_at DESC);

CREATE TABLE mobility.ride_offer (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  ride_order_id bigint NOT NULL REFERENCES mobility.ride_order,
  vehicle_id bigint NOT NULL REFERENCES core.vehicle,
  driver_account_id bigint NOT NULL REFERENCES core.account,
  expire_at timestamptz NOT NULL,
  status text CHECK (status IN ('pending','accepted','rejected','timeout','cancelled')),
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_offer_ride ON mobility.ride_offer (ride_order_id, created_at DESC);

CREATE TABLE mobility.dispatch_decision (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  ride_order_id bigint NOT NULL REFERENCES mobility.ride_order,
  candidate_snapshot jsonb NOT NULL, scores jsonb NOT NULL,
  chosen_vehicle_id bigint REFERENCES core.vehicle,
  policy_version int NOT NULL,
  outcome text CHECK (outcome IN ('auto','manual','manual_override','failed')),
  reason text,
  decided_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_decision_ride ON mobility.dispatch_decision (ride_order_id);

CREATE TABLE mobility.ride_pool (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  vehicle_id bigint NOT NULL REFERENCES core.vehicle,
  driver_account_id bigint NOT NULL REFERENCES core.account,
  operator_org_id bigint NOT NULL REFERENCES core.org,
  seats_total smallint NOT NULL, seats_used smallint NOT NULL DEFAULT 0,
  route_version int NOT NULL DEFAULT 1,
  trip_id bigint REFERENCES biz.trip,
  status text CHECK (status IN ('open','closing','closed')),
  started_at timestamptz, closed_at timestamptz,
  CHECK (seats_used <= seats_total)
);
CREATE UNIQUE INDEX uq_active_pool ON mobility.ride_pool (vehicle_id)
  WHERE status IN ('open','closing');                -- 一车同一时刻只有一个 pool

ALTER TABLE mobility.ride_order
  ADD CONSTRAINT fk_ride_pool FOREIGN KEY (pool_id) REFERENCES mobility.ride_pool;

CREATE TABLE mobility.pool_stop (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  pool_id bigint NOT NULL REFERENCES mobility.ride_pool,
  seq smallint NOT NULL,
  order_id bigint NOT NULL REFERENCES commerce."order",
  action text CHECK (action IN ('pickup','dropoff')),
  planned_eta timestamptz, actual_ts timestamptz,
  UNIQUE (pool_id, order_id, action)
);
CREATE INDEX idx_pool_stop_pool ON mobility.pool_stop (pool_id, seq);

CREATE TABLE mobility.ride_event (                   -- append-only
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  ride_order_id bigint NOT NULL REFERENCES mobility.ride_order,
  event text NOT NULL, payload jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_ride_event_order ON mobility.ride_event (ride_order_id, created_at);

CREATE TABLE mobility.ride_rating (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  ride_order_id bigint NOT NULL REFERENCES mobility.ride_order,
  by_account_id bigint NOT NULL REFERENCES core.account,
  role text CHECK (role IN ('passenger','driver')),
  score smallint CHECK (score BETWEEN 1 AND 5),
  tags jsonb, content text,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (ride_order_id, by_account_id)
);

COMMIT;
