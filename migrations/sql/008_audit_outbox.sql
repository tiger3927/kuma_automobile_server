-- 008 audit：审计日志（按月分区）与 outbox（Task-DataBase.md §11.6）
BEGIN;

CREATE TABLE audit.audit_log (
  created_at timestamptz NOT NULL DEFAULT now(),
  actor_account_id bigint,
  actor_identity text,
  action text NOT NULL,
  resource_type text NOT NULL, resource_id text,
  before jsonb, after jsonb, query jsonb,            -- regulator 查询条件必存
  ip inet
) PARTITION BY RANGE (created_at);

CREATE TABLE audit.audit_log_202610 PARTITION OF audit.audit_log
  FOR VALUES FROM ('2026-10-01') TO ('2026-11-01');
CREATE TABLE audit.audit_log_202611 PARTITION OF audit.audit_log
  FOR VALUES FROM ('2026-11-01') TO ('2026-12-01');
CREATE TABLE audit.audit_log_202612 PARTITION OF audit.audit_log
  FOR VALUES FROM ('2026-12-01') TO ('2027-01-01');

CREATE INDEX idx_audit_res ON audit.audit_log (resource_type, resource_id, created_at DESC);
CREATE INDEX idx_audit_actor ON audit.audit_log (actor_account_id, created_at DESC);
CREATE INDEX idx_audit_query ON audit.audit_log USING gin (query jsonb_path_ops);

CREATE TABLE audit.outbox (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  aggregate_type text NOT NULL, aggregate_id bigint NOT NULL,
  event_type text NOT NULL, payload jsonb NOT NULL,
  status text NOT NULL DEFAULT 'pending',
  retry_count smallint NOT NULL DEFAULT 0,
  published_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_outbox_pending ON audit.outbox (status, created_at) WHERE status = 'pending';
CREATE INDEX idx_outbox_agg ON audit.outbox (aggregate_type, aggregate_id);

COMMIT;
