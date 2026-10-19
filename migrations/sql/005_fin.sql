-- 005 fin：支付、结算、分账、负债（12 表，Task-DataBase.md §11.3）
BEGIN;

CREATE TABLE fin.payment_channel (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  channel_code text UNIQUE NOT NULL,
  provider_type text NOT NULL,                       -- qr_static|wechat|alipay|card_stripe|paypal|mpesa|usdt…
  regions jsonb NOT NULL, currencies jsonb NOT NULL,
  collect_mode text NOT NULL, account_entity text NOT NULL,
  fee_rule jsonb, config_version int NOT NULL DEFAULT 1,
  status text NOT NULL DEFAULT 'off'
);

CREATE TABLE fin.pay_order (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  pay_no text UNIQUE NOT NULL,
  order_id bigint NOT NULL REFERENCES commerce."order",
  provider text CHECK (provider IN ('qr_transfer','points_offset','offline_voucher','channel')),
  channel_id bigint REFERENCES fin.payment_channel,
  amount bigint NOT NULL, currency char(3) NOT NULL,
  status text CHECK (status IN
    ('created','qr_issued','voucher_pending','verifying','paid','timeout','closed')),
  out_trade_no text UNIQUE, expires_at timestamptz, paid_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX uq_pay_active ON fin.pay_order (order_id)
  WHERE status IN ('created','qr_issued','voucher_pending','verifying');   -- 一单只允许一张进行中支付单
CREATE INDEX idx_pay_order ON fin.pay_order (order_id);

CREATE TABLE fin.payment_voucher (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  pay_order_id bigint NOT NULL REFERENCES fin.pay_order,
  transfer_no text NOT NULL,
  amount bigint, currency char(3), proof_url text,
  uploaded_by bigint NOT NULL REFERENCES core.account,
  status text NOT NULL DEFAULT 'pending',
  verified_by bigint REFERENCES core.account, verified_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX uq_transfer_no ON fin.payment_voucher (transfer_no)
  WHERE status <> 'rejected';                        -- 同一流水号跨订单重复核销 = v1 最大事故源

CREATE TABLE fin.refund (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  refund_no text UNIQUE NOT NULL,
  pay_order_id bigint NOT NULL REFERENCES fin.pay_order,
  amount bigint NOT NULL, currency char(3) NOT NULL,
  mode text CHECK (mode IN ('cash','points')),       -- 按原路，禁止交叉
  reason text, status text NOT NULL,
  processed_by bigint REFERENCES core.account,
  points_reversal_ref text,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_refund_pay ON fin.refund (pay_order_id);

CREATE TABLE fin.settlement_rule (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  rule_type text CHECK (rule_type IN ('commission','fare_split','payout')),
  subject text NOT NULL,
  region_id char(2) REFERENCES core.region, type_code text,
  ratio jsonb NOT NULL,
  version int NOT NULL, effective_from date NOT NULL, effective_to date,
  status text NOT NULL DEFAULT 'draft',
  approved_by bigint REFERENCES core.account,
  UNIQUE (rule_type, subject, region_id, type_code, version)
);

CREATE TABLE fin.settlement_period (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  period_no text UNIQUE NOT NULL, type text,
  start_date date NOT NULL, end_date date NOT NULL,
  status text NOT NULL DEFAULT 'open', closed_at timestamptz
);

CREATE TABLE fin.settlement_item (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  period_id bigint REFERENCES fin.settlement_period,
  order_ref_type text NOT NULL, order_ref_id bigint NOT NULL,
  beneficiary_type text CHECK (beneficiary_type IN ('account','org')),
  beneficiary_id bigint NOT NULL,
  role text CHECK (role IN ('driver','operator','dealer','platform','seller')),
  base_amount bigint NOT NULL,                       -- 抵扣/折扣前金额
  ratio numeric(5,4), amount bigint NOT NULL, currency char(3) NOT NULL,
  status text NOT NULL DEFAULT 'pending',
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (order_ref_type, order_ref_id, role)
);
CREATE INDEX idx_settle_beneficiary ON fin.settlement_item (beneficiary_type, beneficiary_id, status);

CREATE TABLE fin.receivable (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  subject_org_id bigint NOT NULL REFERENCES core.org,
  order_ref text NOT NULL,
  amount bigint NOT NULL, currency char(3) NOT NULL,
  due_date date NOT NULL,
  status text CHECK (status IN ('open','confirmed','overdue','closed')),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (subject_org_id, order_ref)
);
CREATE INDEX idx_receivable_status ON fin.receivable (status, due_date);

CREATE TABLE fin.fx_rate_snapshot (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  from_currency char(3) NOT NULL, to_currency char(3) NOT NULL,
  rate numeric(18,8) NOT NULL, source text NOT NULL,
  effective_at timestamptz NOT NULL,
  created_by bigint REFERENCES core.account
);
CREATE INDEX idx_fx_pair_time ON fin.fx_rate_snapshot (from_currency, to_currency, effective_at DESC);

CREATE TABLE fin.points_liability (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  snapshot_date date NOT NULL,
  account_id bigint NOT NULL REFERENCES core.account,
  booked_points bigint NOT NULL,
  currency char(3) NOT NULL,
  fx_rate_id bigint REFERENCES fin.fx_rate_snapshot,
  cash_equiv bigint,
  UNIQUE (snapshot_date, account_id)
);

CREATE TABLE fin.subsidy_ledger (                    -- append-only
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  order_ref_type text NOT NULL, order_ref_id bigint NOT NULL,
  reason text CHECK (reason IN ('points_offset','carpool_discount')),
  points_kind text,                                  -- purchased|earned（账务不同）
  amount bigint NOT NULL, currency char(3) NOT NULL,
  beneficiary_type text, beneficiary_id bigint,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_subsidy_ref ON fin.subsidy_ledger (order_ref_type, order_ref_id);

CREATE TABLE fin.fin_journal (                       -- append-only
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  entry_no text UNIQUE NOT NULL,
  biz_type text NOT NULL, biz_ref text,
  debit bigint NOT NULL, credit bigint NOT NULL, currency char(3) NOT NULL,
  memo text, created_by bigint REFERENCES core.account,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_journal_biz ON fin.fin_journal (biz_type, biz_ref);

COMMIT;
