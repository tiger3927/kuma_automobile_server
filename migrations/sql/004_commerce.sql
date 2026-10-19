-- 004 commerce：商城与订单（12 表，Task-DataBase.md §11.2）
BEGIN;

CREATE TABLE commerce.product (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  product_type text NOT NULL CHECK (product_type IN
    ('vehicle','used_vehicle','part','service','points_pack','points_item','coupon')),
  category text, name text NOT NULL,
  type_code text REFERENCES core.vehicle_type,
  manufacturer_org_id bigint REFERENCES core.org,
  msrp bigint, currency char(3) NOT NULL,
  region_id char(2) REFERENCES core.region,
  status text NOT NULL DEFAULT 'draft',
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_product_region_type ON commerce.product (region_id, product_type, status);

CREATE TABLE commerce.sku (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  product_id bigint NOT NULL REFERENCES commerce.product,
  code text UNIQUE NOT NULL,
  attrs jsonb NOT NULL DEFAULT '{}',
  price bigint NOT NULL, currency char(3) NOT NULL,
  status text NOT NULL DEFAULT 'active'
);
CREATE INDEX idx_sku_product ON commerce.sku (product_id);

CREATE TABLE commerce.vehicle_listing (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  vin char(17) UNIQUE NOT NULL REFERENCES core.vehicle (vin),
  sku_id bigint REFERENCES commerce.sku,
  dealer_org_id bigint NOT NULL REFERENCES core.org,
  condition text NOT NULL CHECK (condition IN ('new','used')),
  sale_type text NOT NULL CHECK (sale_type IN ('retail','consign','trade_in')),
  asking_price bigint, currency char(3),
  report_id bigint,                                  -- 二手车 → commerce.used_car_report（M14 前可空）
  status text NOT NULL DEFAULT 'listed',             -- listed|locked|sold|withdrawn
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_listing_dealer ON commerce.vehicle_listing (dealer_org_id, status);

CREATE TABLE commerce.stock (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  sku_id bigint NOT NULL REFERENCES commerce.sku,
  dealer_org_id bigint NOT NULL REFERENCES core.org,
  qty int NOT NULL CHECK (qty >= 0),
  UNIQUE (sku_id, dealer_org_id)
);
CREATE INDEX idx_stock_dealer ON commerce.stock (dealer_org_id);

CREATE TABLE commerce.cart_item (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  account_id bigint NOT NULL REFERENCES core.account,
  sku_id bigint NOT NULL REFERENCES commerce.sku,
  qty smallint NOT NULL DEFAULT 1,
  UNIQUE (account_id, sku_id)
);

CREATE TABLE commerce."order" (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  order_no text UNIQUE NOT NULL,
  account_id bigint NOT NULL REFERENCES core.account,
  order_type text CHECK (order_type IN
    ('vehicle','used_vehicle','part','service','points_pack','points_item','coupon')),
  dealer_org_id bigint REFERENCES core.org,
  operator_org_id bigint REFERENCES core.org,
  region_id char(2) NOT NULL REFERENCES core.region,
  currency char(3) NOT NULL,
  amount_cash bigint NOT NULL,
  amount_points bigint NOT NULL DEFAULT 0,
  collect_mode text CHECK (collect_mode IN ('platform_online','dealer_offline','operator_offline')),
  status text NOT NULL,
  idempotency_key text UNIQUE,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz
);
CREATE INDEX idx_order_account ON commerce."order" (account_id, created_at DESC);
CREATE INDEX idx_order_dealer ON commerce."order" (dealer_org_id, created_at DESC);
CREATE INDEX idx_order_open ON commerce."order" (region_id, status)
  WHERE status NOT IN ('completed','cancelled','refunded');

CREATE TABLE commerce.order_item (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  order_id bigint NOT NULL REFERENCES commerce."order",
  sku_id bigint NOT NULL REFERENCES commerce.sku,
  product_type text NOT NULL,
  vin char(17) REFERENCES core.vehicle (vin),
  qty smallint NOT NULL DEFAULT 1,
  unit_price bigint NOT NULL, points_cost bigint NOT NULL DEFAULT 0
);
CREATE INDEX idx_order_item_order ON commerce.order_item (order_id);
CREATE INDEX idx_order_item_vin ON commerce.order_item (vin);

CREATE TABLE commerce.order_deposit (
  order_id bigint PRIMARY KEY REFERENCES commerce."order",
  amount bigint NOT NULL, currency char(3) NOT NULL,
  refund_deadline timestamptz NOT NULL,              -- paid+48h 无理由退
  status text NOT NULL DEFAULT 'pending',            -- pending|paid|refunded|forfeited
  paid_at timestamptz, forfeited_at timestamptz
);

CREATE TABLE commerce.fulfillment (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  order_id bigint NOT NULL REFERENCES commerce."order",
  type text CHECK (type IN ('pickup','delivery','digital')),
  dealer_org_id bigint REFERENCES core.org,
  pickup_code text UNIQUE,
  status text NOT NULL, delivered_at timestamptz,
  confirmed_by bigint REFERENCES core.account
);
CREATE INDEX idx_fulfill_order ON commerce.fulfillment (order_id);
CREATE INDEX idx_fulfill_dealer ON commerce.fulfillment (dealer_org_id, status);

CREATE TABLE commerce.aftersale (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  aftersale_no text UNIQUE NOT NULL,
  order_id bigint NOT NULL REFERENCES commerce."order",
  type text CHECK (type IN ('refund','return','repair','warranty')),
  reason text, status text NOT NULL, resolution jsonb,
  handler_id bigint REFERENCES core.account,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_aftersale_order ON commerce.aftersale (order_id);

CREATE TABLE commerce.used_car_report (              -- append-only，链式防篡改
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  vin char(17) NOT NULL REFERENCES core.vehicle (vin),
  snapshot jsonb NOT NULL,
  odo_total_km int NOT NULL,
  data_window_start timestamptz, data_window_end timestamptz,
  hash text NOT NULL, prev_hash text,
  generated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_ucr_vin ON commerce.used_car_report (vin, generated_at DESC);

CREATE TABLE commerce.product_review (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  order_id bigint NOT NULL REFERENCES commerce."order",
  account_id bigint NOT NULL REFERENCES core.account,
  sku_id bigint NOT NULL REFERENCES commerce.sku,
  score smallint NOT NULL CHECK (score BETWEEN 1 AND 5),
  content text, status text NOT NULL DEFAULT 'visible',
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (order_id, account_id)
);

-- 后补 FK（表齐了再挂，避免建表顺序死锁）
ALTER TABLE commerce.vehicle_listing
  ADD CONSTRAINT fk_listing_report FOREIGN KEY (report_id) REFERENCES commerce.used_car_report;

COMMIT;
