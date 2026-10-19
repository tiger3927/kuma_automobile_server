-- 006 mkt：积分、优惠券、活动（9 表，Task-DataBase.md §11.4）
BEGIN;

CREATE TABLE mkt.points_rule (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  rule_code text NOT NULL,
  source_type text CHECK (source_type IN
    ('vehicle_purchase','commerce','ride','campaign','purchase')),
  region_id char(2) REFERENCES core.region, type_code text,
  ratio numeric(8,6), fixed_points bigint,
  caps jsonb NOT NULL DEFAULT '{}',
  version int NOT NULL, effective_from date NOT NULL, effective_to date,
  status text NOT NULL DEFAULT 'draft',
  approved_by bigint REFERENCES core.account,
  UNIQUE (rule_code, region_id, version)
);

CREATE TABLE mkt.points_pack (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  pack_code text UNIQUE NOT NULL, points bigint NOT NULL,
  price bigint NOT NULL, currency char(3) NOT NULL,
  validity_months smallint NOT NULL DEFAULT 24,
  region_id char(2) REFERENCES core.region,
  status text NOT NULL DEFAULT 'active'
);

CREATE TABLE mkt.points_purchase (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  purchase_no text UNIQUE NOT NULL,
  order_id bigint NOT NULL REFERENCES commerce."order",
  pack_id bigint NOT NULL REFERENCES mkt.points_pack,
  points bigint NOT NULL,
  amount bigint NOT NULL, currency char(3) NOT NULL,
  fx_rate_id bigint REFERENCES fin.fx_rate_snapshot,
  status text NOT NULL DEFAULT 'completed',
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_points_purchase_account ON mkt.points_purchase (order_id);

CREATE TABLE mkt.points_account (
  account_id bigint PRIMARY KEY REFERENCES core.account,
  earned_balance bigint NOT NULL DEFAULT 0,
  purchased_balance bigint NOT NULL DEFAULT 0,       -- 两类分开计量（§7.1 铁律 2）
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE mkt.points_batch (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  account_id bigint NOT NULL REFERENCES core.account,
  points_kind text NOT NULL CHECK (points_kind IN ('purchased','earned')),
  total bigint NOT NULL, remaining bigint NOT NULL,
  expires_at timestamptz NOT NULL,
  source_type text NOT NULL, source_ref text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_batch_spend ON mkt.points_batch (account_id, points_kind, expires_at)
  WHERE remaining > 0;                               -- 扣减按 expires_at 升序

CREATE TABLE mkt.points_ledger (                     -- append-only
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  account_id bigint NOT NULL REFERENCES core.account,
  points_kind text NOT NULL CHECK (points_kind IN ('purchased','earned')),
  delta bigint NOT NULL, balance_after bigint NOT NULL,
  source_type text NOT NULL, source_ref text NOT NULL,
  memo text, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX uq_ledger_source ON mkt.points_ledger (source_type, source_ref, points_kind);
CREATE INDEX idx_ledger_account ON mkt.points_ledger (account_id, created_at DESC);

CREATE TABLE mkt.campaign (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  name text NOT NULL, region_id char(2) REFERENCES core.region,
  budget_total bigint NOT NULL, budget_used bigint NOT NULL DEFAULT 0,
  per_user_limit smallint, start_at timestamptz, end_at timestamptz,
  status text NOT NULL DEFAULT 'draft'
);

CREATE TABLE mkt.coupon_rule (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  name text NOT NULL, type text CHECK (type IN ('fixed','percent')),
  threshold bigint, discount bigint, region_id char(2) REFERENCES core.region,
  budget_total bigint, issued_count int NOT NULL DEFAULT 0, per_user_limit smallint,
  valid_days smallint NOT NULL, status text NOT NULL DEFAULT 'draft'
);

CREATE TABLE mkt.coupon (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  rule_id bigint NOT NULL REFERENCES mkt.coupon_rule,
  account_id bigint NOT NULL REFERENCES core.account,
  status text NOT NULL DEFAULT 'unused',
  source text, used_ref text, expires_at timestamptz NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_coupon_account ON mkt.coupon (account_id, status);

COMMIT;
