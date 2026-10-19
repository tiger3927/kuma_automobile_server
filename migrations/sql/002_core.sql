-- 002 core：登录体系、组织分级、车辆主数据、媒体（21 表，Task-DataBase.md §11.1）
-- 建表顺序已按 FK 依赖排列（org 在 membership 之前）
BEGIN;

CREATE TABLE core.region (
  country_code char(2) PRIMARY KEY,
  currency char(3) NOT NULL,
  timezone text NOT NULL,
  features jsonb NOT NULL DEFAULT '{}',
  status text NOT NULL DEFAULT 'active'
);

CREATE TABLE core.account (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  username citext UNIQUE NOT NULL,
  phone text UNIQUE,
  email citext UNIQUE,
  password_hash text,
  status text NOT NULL DEFAULT 'active',
  kyc_level smallint NOT NULL DEFAULT 0,
  region_id char(2) REFERENCES core.region,
  last_login_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE core.org (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  org_type text NOT NULL CHECK (org_type IN ('manufacturer','dealer','operator','government')),
  code text UNIQUE NOT NULL,
  name text NOT NULL,
  parent_org_id bigint REFERENCES core.org,
  level smallint NOT NULL DEFAULT 1,
  path text NOT NULL,
  region_id char(2) REFERENCES core.region,
  primary_account_id bigint REFERENCES core.account,
  status text NOT NULL DEFAULT 'active',
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_org_path ON core.org (path text_pattern_ops);
CREATE INDEX idx_org_parent ON core.org (parent_org_id);
CREATE INDEX idx_org_type_status ON core.org (org_type, status);
CREATE INDEX idx_org_name_trgm ON core.org USING gin (name gin_trgm_ops);
CREATE UNIQUE INDEX uq_org_primary_account ON core.org (primary_account_id);   -- 一人只主一个组织（§11.7）

CREATE TABLE core.identity (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  account_id bigint NOT NULL REFERENCES core.account,
  identity_type text NOT NULL CHECK (identity_type IN
    ('consumer','owner','driver','dealer_staff','manufacturer_staff',
     'operator_staff','regulator','platform_staff')),
  status text NOT NULL DEFAULT 'active',
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (account_id, identity_type)
);
CREATE INDEX idx_identity_account ON core.identity (account_id);

CREATE TABLE core.membership (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  account_id bigint NOT NULL REFERENCES core.account,
  org_id bigint NOT NULL REFERENCES core.org,
  role text NOT NULL CHECK (role IN ('primary_admin','admin','agent','auditor')),
  status text NOT NULL DEFAULT 'active',
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (account_id, org_id, role)
);
CREATE INDEX idx_membership_org ON core.membership (org_id);
CREATE UNIQUE INDEX uq_membership_primary ON core.membership (org_id)
  WHERE role = 'primary_admin' AND status = 'active';   -- 组织只有一个在职主管理员（§11.7）

CREATE TABLE core.dealer_profile (
  org_id bigint PRIMARY KEY REFERENCES core.org,
  tier smallint NOT NULL DEFAULT 1,
  msrp_min_pct numeric(5,4), msrp_max_pct numeric(5,4),
  commission_override numeric(5,4)
);

CREATE TABLE core.operator_profile (
  org_id bigint PRIMARY KEY REFERENCES core.org,
  license_no text, license_expiry date,
  fare_template_owner boolean NOT NULL DEFAULT true
);

CREATE TABLE core.user_profile (
  account_id bigint PRIMARY KEY REFERENCES core.account,
  display_name text, avatar_url text, locale text,
  id_type text, id_number_hash text
);
CREATE INDEX idx_user_profile_name_trgm ON core.user_profile USING gin (display_name gin_trgm_ops);

CREATE TABLE core.kyc_record (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  account_id bigint NOT NULL REFERENCES core.account,
  level smallint NOT NULL,
  id_type text, id_number_hash text, docs jsonb,
  status text NOT NULL DEFAULT 'pending',
  reviewed_by bigint REFERENCES core.account,
  reviewed_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX uq_kyc_approved ON core.kyc_record (account_id, level) WHERE status = 'approved';

CREATE TABLE core.refresh_token (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  account_id bigint NOT NULL REFERENCES core.account,
  token_hash text UNIQUE NOT NULL,
  device_id text, expires_at timestamptz NOT NULL, revoked_at timestamptz
);
CREATE INDEX idx_refresh_account ON core.refresh_token (account_id);

CREATE TABLE core.otp_code (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  target text NOT NULL, purpose text NOT NULL, code_hash text NOT NULL,
  expires_at timestamptz NOT NULL, used_at timestamptz, attempts smallint DEFAULT 0
);
CREATE INDEX idx_otp_target ON core.otp_code (target, purpose, expires_at DESC);

CREATE TABLE core.idempotency_key (
  key text PRIMARY KEY,
  account_id bigint REFERENCES core.account,
  response_hash text,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE core.vehicle_type (
  type_code text PRIMARY KEY,
  name text NOT NULL,
  product_line text NOT NULL CHECK (product_line IN ('tricycle','minibus','sedan','pickup')),
  category text NOT NULL,
  seats smallint NOT NULL,
  passenger_seats smallint NOT NULL,
  doors smallint,
  load_capacity_kg int, cargo_len_mm int, cargo_wid_mm int, cargo_hgt_mm int,
  length_mm int, width_mm int, height_mm int, wheelbase_mm int, min_ground_clearance_mm int,
  curb_weight_kg int, gvwr_kg int,
  power_type text NOT NULL DEFAULT 'ev' CHECK (power_type IN ('ev','ice','hybrid')),
  motor_power_kw numeric(6,2), peak_power_kw numeric(6,2), motor_count smallint,
  battery_kwh numeric(6,2), battery_type text,
  range_km int, range_condition text,
  top_speed_kmh smallint, climbing_grade_pct smallint,
  charge_fast_h numeric(4,1), charge_slow_h numeric(4,1),
  drive_type text,
  fuel_l_100km numeric(5,2),
  specs jsonb NOT NULL DEFAULT '{}',
  features jsonb NOT NULL DEFAULT '[]',
  protocol_profile text NOT NULL,
  regions jsonb NOT NULL DEFAULT '[]',
  status text NOT NULL DEFAULT 'active'
);

CREATE TABLE core.vehicle (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  vin char(17) UNIQUE NOT NULL,
  type_code text NOT NULL REFERENCES core.vehicle_type,
  plate text, model text, trim text,
  model_year smallint,
  color text, color_code text,
  manufacture_date date, factory_code text,
  battery_sn text, motor_sn text,
  battery_kwh_actual numeric(6,2),
  odo_at_delivery_km int,
  factory_attrs jsonb NOT NULL DEFAULT '{}',
  owner_type text NOT NULL CHECK (owner_type IN ('account','org')),
  owner_account_id bigint REFERENCES core.account,
  owner_org_id bigint REFERENCES core.org,
  usage_type text NOT NULL DEFAULT 'retail',
  region_id char(2) REFERENCES core.region,
  cur_dealer_org_id bigint REFERENCES core.org,
  status text NOT NULL DEFAULT 'produced',
  created_at timestamptz NOT NULL DEFAULT now(),
  CHECK ((owner_type = 'account' AND owner_account_id IS NOT NULL)
      OR (owner_type = 'org' AND owner_org_id IS NOT NULL))
);
CREATE INDEX idx_vehicle_type_status ON core.vehicle (type_code, status);
CREATE INDEX idx_vehicle_owner_account ON core.vehicle (owner_account_id);
CREATE INDEX idx_vehicle_owner_org ON core.vehicle (owner_org_id);
CREATE INDEX idx_vehicle_dealer ON core.vehicle (cur_dealer_org_id, status);
CREATE INDEX idx_vehicle_plate_trgm ON core.vehicle USING gin (plate gin_trgm_ops);
CREATE UNIQUE INDEX uq_vehicle_battery_sn ON core.vehicle (battery_sn) WHERE battery_sn IS NOT NULL;
CREATE UNIQUE INDEX uq_vehicle_motor_sn ON core.vehicle (motor_sn) WHERE motor_sn IS NOT NULL;

CREATE TABLE core.device (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  device_sn text UNIQUE NOT NULL,
  vin char(17) REFERENCES core.vehicle (vin),
  model text, activated_at timestamptz, replaced_from_id bigint REFERENCES core.device
);
CREATE INDEX idx_device_vin ON core.device (vin);

CREATE TABLE core.sim (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  iccid text UNIQUE NOT NULL, carrier text,
  device_id bigint REFERENCES core.device
);
CREATE INDEX idx_sim_device ON core.sim (device_id);

CREATE TABLE core.vehicle_credential (
  vin char(17) PRIMARY KEY REFERENCES core.vehicle (vin),
  mqtt_username text UNIQUE NOT NULL,
  password_hash text NOT NULL,
  version smallint NOT NULL DEFAULT 1,
  status text NOT NULL DEFAULT 'active',
  issued_at timestamptz, revoked_at timestamptz
);

CREATE TABLE core.subscription_plan (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  code text UNIQUE NOT NULL,
  region_id char(2) REFERENCES core.region,
  plan_type text CHECK (plan_type IN ('basic','value_added','operator')),
  name text, price bigint NOT NULL, currency char(3) NOT NULL,
  duration_days int NOT NULL, features jsonb,
  version int NOT NULL DEFAULT 1,
  status text NOT NULL DEFAULT 'active'
);

CREATE TABLE core.subscription (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  vin char(17) NOT NULL REFERENCES core.vehicle (vin),
  plan_id bigint NOT NULL REFERENCES core.subscription_plan,
  status text NOT NULL DEFAULT 'active',
  start_at timestamptz, end_at timestamptz, auto_renew boolean DEFAULT true,
  payer_account_id bigint REFERENCES core.account
);
CREATE UNIQUE INDEX uq_sub_active ON core.subscription (vin) WHERE status = 'active';
CREATE INDEX idx_sub_payer ON core.subscription (payer_account_id);

CREATE TABLE core.ownership_transfer (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  vin char(17) NOT NULL REFERENCES core.vehicle (vin),
  from_type text, from_account_id bigint, from_org_id bigint,
  to_type text, to_account_id bigint, to_org_id bigint,
  transferred_at timestamptz NOT NULL,
  operator_id bigint REFERENCES core.account,
  attachment jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_transfer_vin ON core.ownership_transfer (vin, transferred_at DESC);

CREATE TABLE core.media (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  owner_type text NOT NULL CHECK (owner_type IN
    ('vehicle_type','vehicle','product','sku','order','aftersale',
     'driver_profile','org','account')),
  owner_id bigint NOT NULL,
  role text NOT NULL,
  file_key text NOT NULL,
  mime text, width_px int, height_px int, size_bytes bigint,
  sha256 char(64),
  sort smallint NOT NULL DEFAULT 0,
  uploaded_by bigint REFERENCES core.account,
  status text NOT NULL DEFAULT 'active',
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_media_owner ON core.media (owner_type, owner_id, sort);
CREATE UNIQUE INDEX uq_media_slot ON core.media (owner_type, owner_id, role, sort) WHERE status = 'active';

COMMIT;
