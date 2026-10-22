"""告警规则化 + 凭据下发状态 + 监管视图（C3 / C5 / C8，2026-10-22）

一次迁移三件事，都是"代码已就位、缺 DDL"：

1. **`biz.alarm` 加 `snooze_until` / `updated_at`**：告警要支持静默与关闭/重开。
   `status` 列本来就没有 CHECK（取值域 `open|ack|auto_closed` 只是注释），
   所以新增状态值不需要改表，只有"静默到几点"这个事实需要新列。
2. **新建 `biz.alarm_rule`**：阈值此前硬编码在 `tasks/alarm_rules.py:20-21`
   （`SPEED_LIMIT_KMH=80` / `HARSH_BRAKE_DROP=25`）。限速是**地区差异**，
   而 §12.1 明令禁止代码写死国家——阈值必须能按 `region_code` 配。
3. **`core.vehicle_credential` 加下发列**：此前只有"生成"（`vehicle_service.issue_credential`），
   下发侧 0 行代码，`issue_credential` 自己返回 `"delivered": False`。
4. **三个 `rep.gov_*` 视图**：gov 端点此前只有 trip/sales 两个视图可读；
   按 §3.2「跨域展示走 rep」的规矩，监管侧新读的三张跨 schema 数据也做成视图。

Revision ID: 0006_alarm_cred_gov
Revises: 0005_truncate_staging
"""
from __future__ import annotations

from alembic import op

revision = "0006_alarm_cred_gov"
down_revision = "0005_truncate_staging"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── 1. 告警：静默与状态流转 ─────────────────────────────────
    op.execute("""
        ALTER TABLE biz.alarm
          ADD COLUMN IF NOT EXISTS snooze_until timestamptz,
          ADD COLUMN IF NOT EXISTS updated_at timestamptz NOT NULL DEFAULT now();
    """)
    # 静默到期扫描（worker 的 alarm_snooze 任务）
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_alarm_snooze
          ON biz.alarm (snooze_until) WHERE status = 'silenced';
    """)
    # 同类抑制查询：WHERE vin=? AND type=? AND status IN (...) AND ts BETWEEN ...
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_alarm_vin_type_status_ts
          ON biz.alarm (vin, type, status, ts DESC);
    """)

    # ── 2. 告警规则表 ──────────────────────────────────────────
    op.execute("""
        CREATE TABLE IF NOT EXISTS biz.alarm_rule (
          id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
          type text NOT NULL,
          region_code char(2) REFERENCES core.region (country_code),
          enabled boolean NOT NULL DEFAULT true,
          level smallint NOT NULL DEFAULT 2,
          params jsonb NOT NULL DEFAULT '{}',
          dedupe_window_s int NOT NULL DEFAULT 600,
          updated_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE NULLS NOT DISTINCT (type, region_code)
        );
    """)
    # PG15+ 的 `NULLS NOT DISTINCT` 才允许 "全局默认行"（region_code IS NULL）参与唯一约束；
    # 用普通 UNIQUE 的话，多条全局默认行会互不冲突，规则就会重复生效。
    op.execute("GRANT SELECT, INSERT, UPDATE ON biz.alarm_rule TO kuma_app")
    op.execute("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA biz TO kuma_app")

    # 默认规则 = 迁移前硬编码的那几个值，行为不变（先等价搬迁，再谈调参）
    op.execute("""
        INSERT INTO biz.alarm_rule (type, region_code, level, params, dedupe_window_s) VALUES
          ('overspeed',       NULL, 2, '{"speed_kmh": 80}',        600),
          ('harsh_brake',     NULL, 2, '{"drop_kmh": 25}',         300),
          ('fence_exit',      NULL, 3, '{}',                       900),
          ('offline',         NULL, 2, '{"offline_s": 600}',      1800),
          ('mileage_anomaly', NULL, 3, '{"diff_ratio": 0.15}',    3600)
        ON CONFLICT ON CONSTRAINT alarm_rule_type_region_code_key DO NOTHING;
    """)

    # ── 3. 凭据下发状态 ────────────────────────────────────────
    op.execute("""
        ALTER TABLE core.vehicle_credential
          ADD COLUMN IF NOT EXISTS deliver_status text NOT NULL DEFAULT 'none',
          ADD COLUMN IF NOT EXISTS delivered_at timestamptz,
          ADD COLUMN IF NOT EXISTS claimed_at timestamptz;
    """)
    # deliver_status 取值域：none | pending | delivered | claimed | failed
    # （不加 CHECK：加 CHECK 会让"新增一个中间态"变成一次 DDL 迁移，而这张表的状态
    #   在 M9 交付链里还会继续细化。取值域由代码与 docs/mqtt-security.md 约束。）

    # ── 4. 监管视图 ───────────────────────────────────────────
    op.execute("""
        CREATE OR REPLACE VIEW rep.gov_vehicle_view AS
        SELECT v.vin, v.plate, v.type_code, v.status AS vehicle_status, v.region_id,
               v.owner_type, v.owner_account_id, v.owner_org_id,
               o.name AS owner_org_name,
               r.currency, r.timezone,
               c.mqtt_username, c.version AS cred_version, c.status AS cred_status,
               c.deliver_status, c.issued_at,
               s.status AS sub_status, s.end_at AS sub_end_at
        FROM core.vehicle v
        LEFT JOIN core.org o ON o.id = v.owner_org_id
        LEFT JOIN core.region r ON r.country_code = v.region_id
        LEFT JOIN core.vehicle_credential c ON c.vin = v.vin
        LEFT JOIN core.subscription s ON s.vin = v.vin AND s.status = 'active';
    """)
    op.execute("""
        CREATE OR REPLACE VIEW rep.gov_alarm_view AS
        SELECT a.id, a.vin, a.ts, a.type, a.level, a.status, a.snooze_until, a.payload,
               v.plate, v.region_id, v.owner_org_id, o.name AS owner_org_name
        FROM biz.alarm a
        JOIN core.vehicle v ON v.vin = a.vin
        LEFT JOIN core.org o ON o.id = v.owner_org_id;
    """)
    op.execute("""
        CREATE OR REPLACE VIEW rep.gov_command_view AS
        SELECT c.cmd_id, c.vin, c.cmd_type, c.status, c.attempt,
               c.created_at, c.sent_at, c.acked_at,
               v.plate, v.region_id, v.owner_org_id,
               la.actor_account_id AS last_actor, la.event AS last_event,
               la.created_at AS last_event_at
        FROM biz.command c
        JOIN core.vehicle v ON v.vin = c.vin
        LEFT JOIN LATERAL (
          SELECT actor_account_id, event, created_at
          FROM biz.command_audit ca
          WHERE ca.cmd_id = c.cmd_id
          ORDER BY ca.id DESC LIMIT 1
        ) la ON true;
    """)
    # 010 的 `GRANT SELECT ON ALL TABLES IN SCHEMA rep` 只覆盖当时已存在的对象，
    # 新视图必须显式授权，否则 kuma_app 读不到、kuma_ro 也读不到。
    op.execute("GRANT SELECT ON rep.gov_vehicle_view, rep.gov_alarm_view, rep.gov_command_view TO kuma_app")
    op.execute("GRANT SELECT ON rep.gov_vehicle_view, rep.gov_alarm_view, rep.gov_command_view TO kuma_ro")


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS rep.gov_command_view")
    op.execute("DROP VIEW IF EXISTS rep.gov_alarm_view")
    op.execute("DROP VIEW IF EXISTS rep.gov_vehicle_view")
    op.execute("ALTER TABLE core.vehicle_credential "
               "DROP COLUMN IF EXISTS claimed_at, DROP COLUMN IF EXISTS delivered_at, "
               "DROP COLUMN IF EXISTS deliver_status")
    op.execute("DROP TABLE IF EXISTS biz.alarm_rule")
    op.execute("DROP INDEX IF EXISTS idx_alarm_vin_type_status_ts")
    op.execute("DROP INDEX IF EXISTS idx_alarm_snooze")
    op.execute("ALTER TABLE biz.alarm DROP COLUMN IF EXISTS updated_at, "
               "DROP COLUMN IF EXISTS snooze_until")
