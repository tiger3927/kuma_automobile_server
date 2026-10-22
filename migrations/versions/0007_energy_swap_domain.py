"""换电与电池资产域（M15 / Task-Plan.md §16 / Task-DataBase.md §11.11）

一次迁移做四件事，顺序不能换（FK 有向）：

1. **新建 `energy` schema 与 11 张表**：型号字典（型号→度数→单价，也是站点准入键）、
   SN 级电池台账 + append-only 流转流水、换电站（含准入型号白名单）、`station_stock`
   （"可用电池数"唯一来源）、按型号按次价目与押金规则（版本化）、**换电协议**（押金是
   生效前置）、押金台账（**负债，不进收入与结算**）、换电单（幂等键 + 拒单原因码）、
   换电状态轨迹（append-only）。
2. **改已落地的 `core`**（这是本轮最贵的一条，越晚改越像数据迁移）：
   - 新增 `core.org_group` / `core.org_business`（业务线 + 牌照，表达"一家公司同时做
     经销商 / 网约车 / 换电站"，`org_type` 语义降级为主业态、CHECK 不动以兼容 M1–M2）；
   - 新增 `core.vehicle_battery_binding`——运行时"车上装的是哪块电池"的**唯一来源**；
   - `core.vehicle_type` 加 `swap_compatible` / `battery_model_code`（与型号字典构成双向声明）；
   - **撤销 `core.uq_vehicle_battery_sn`**（`002_core.sql:178` 实际存在）：换电后一块电池
     先后属于多台车，"SN 在 vehicle 表唯一"不成立；SN 唯一性由 `energy.battery` 接管。
3. **改 `fin.subsidy_ledger` 的 CHECK**：扩 `swap_offset`（换电积分抵扣由平台补贴，
   收款方按抵扣前金额收款）。DROP + ADD CONSTRAINT，不是新表。
4. **`rep` 四个只读视图**：附近站点（含库存与生效价）、押金三问、按 SN 追溯流转链。
   注意 `010_grants_appendonly.sql` 的 `GRANT ... ON ALL TABLES` 不覆盖后建对象，
   所以视图与 energy 新表都要**显式授权**（照 `0006:128-131` 的做法）。

**没有 seed**：`battery_model` 的规格值由厂家按 `Task-Plan.md` §16.2 模板给（§14.3-8），
价目与押金档位是运营商申报 + 平台审核——都不该由迁移写死数字。

Revision ID: 0007_energy_swap
Revises: 0006_alarm_cred_gov
"""
from __future__ import annotations

from alembic import op

revision = "0007_energy_swap"
down_revision = "0006_alarm_cred_gov"
branch_labels = None
depends_on = None

# 0004 的口径：不能加引号（会被当成一个含逗号的标识符）、必须带 public（PostGIS/citext 在 public）
APP_PATH = "core, biz, tele, geo, rep, commerce, fin, mkt, mobility, energy, audit, public"
APP_PATH_OLD = "core, biz, tele, geo, rep, commerce, fin, mkt, mobility, audit, public"


def upgrade() -> None:
    # ══ 1. energy schema ═══════════════════════════════════════════════════
    op.execute("CREATE SCHEMA IF NOT EXISTS energy")
    # 先设默认权限再建表：迁移以 kuma_owner 执行，这样新建对象自动带 DML（照 010 的口径）
    op.execute("""
        ALTER DEFAULT PRIVILEGES FOR ROLE kuma_owner IN SCHEMA energy
          GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO kuma_app;
    """)
    op.execute("""
        ALTER DEFAULT PRIVILEGES FOR ROLE kuma_owner IN SCHEMA energy
          GRANT USAGE, SELECT ON SEQUENCES TO kuma_app;
    """)

    # ── 1.1 型号字典：既是定价键，也是准入键（Task-Plan §16.2）────────────
    op.execute("""
        CREATE TABLE energy.battery_model (
          model_code text PRIMARY KEY,
          name text NOT NULL,
          vendor text NOT NULL,
          spec_doc_media_id bigint,
          kwh_nominal numeric(6,2) NOT NULL CHECK (kwh_nominal > 0),
          kwh_accept_min numeric(6,2) NOT NULL CHECK (kwh_accept_min <= kwh_nominal),
          voltage_nominal_v int NOT NULL CHECK (voltage_nominal_v > 0),
          voltage_max_v int, voltage_cutoff_v int,
          capacity_ah int,
          chemistry text NOT NULL CHECK (chemistry IN ('lfp','nmc','nca','lto','other')),
          cell_format text CHECK (cell_format IN ('prismatic','cylindrical','pouch')),
          cycle_life_rated int,
          charge_max_c numeric(3,2), discharge_max_c numeric(3,2),
          temp_charge_min_c int, temp_charge_max_c int,
          dims_l_mm int NOT NULL, dims_w_mm int NOT NULL, dims_h_mm int NOT NULL,
          weight_kg numeric(5,2) NOT NULL CHECK (weight_kg > 0),
          needs_handling_aid boolean NOT NULL DEFAULT false,
          mounting text NOT NULL, mounting_drawing_media_id bigint,
          ip_rating text NOT NULL CHECK (ip_rating IN ('ip54','ip65','ip67','other')),
          connector_type text NOT NULL,
          connector_keying text,
          bms_proto text NOT NULL CHECK (bms_proto IN ('can_od','can_kd','custom_json','none')),
          bms_sn_field text,
          telemetry_readable boolean NOT NULL DEFAULT false,
          compatible_types jsonb NOT NULL DEFAULT '[]'
            CHECK (jsonb_typeof(compatible_types) = 'array'),
          cert_marks jsonb NOT NULL DEFAULT '[]' CHECK (jsonb_typeof(cert_marks) = 'array'),
          hazard_class text,
          recall_batch_support boolean NOT NULL DEFAULT true,
          status text NOT NULL DEFAULT 'draft'
            CHECK (status IN ('draft','active','retired')),
          version int NOT NULL DEFAULT 1,
          approved_by bigint, approved_at timestamptz,
          created_at timestamptz NOT NULL DEFAULT now(),
          updated_at timestamptz NOT NULL DEFAULT now(),
          -- 闸门前置到字典层：规格不完整就不允许 active，不靠站点现场判断（§16.2 状态规则）
          CONSTRAINT ck_battery_model_active_complete CHECK (
            status <> 'active'
            OR (spec_doc_media_id IS NOT NULL
                AND mounting_drawing_media_id IS NOT NULL
                AND jsonb_array_length(compatible_types) >= 1))
        );
    """)
    op.execute("""
        CREATE INDEX idx_battery_model_status ON energy.battery_model (status);
    """)

    # ── 1.2 换电站（运营商资产；先于 battery，因为 battery 引用它）────────
    op.execute("""
        CREATE TABLE energy.station (
          id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
          code text UNIQUE NOT NULL,
          org_id bigint NOT NULL REFERENCES core.org(id),
          name text NOT NULL,
          geom geometry(Point,4326) NOT NULL,
          address text,
          region_id char(2) REFERENCES core.region(country_code),
          business_hours jsonb NOT NULL DEFAULT '{}',
          -- 准入型号白名单（§16.1）：数组元素的引用完整性无法用 FK，由 services 校验 + 变更写审计
          allowed_models jsonb NOT NULL DEFAULT '[]'
            CHECK (jsonb_typeof(allowed_models) = 'array'),
          bay_count smallint,
          needs_handling_aid boolean NOT NULL DEFAULT false,
          status text NOT NULL DEFAULT 'active'
            CHECK (status IN ('active','paused','closed')),
          created_at timestamptz NOT NULL DEFAULT now(),
          updated_at timestamptz NOT NULL DEFAULT now()
        );
    """)
    op.execute("CREATE INDEX idx_station_geom ON energy.station USING GIST (geom);")
    op.execute("""
        CREATE INDEX idx_station_allowed_gin ON energy.station
          USING GIN (allowed_models jsonb_path_ops);
    """)
    op.execute("CREATE INDEX idx_station_org ON energy.station (org_id);")
    op.execute("CREATE INDEX idx_station_region_status ON energy.station (region_id, status);")

    # ── 1.3 SN 级单件资产（不是数量库存，不进 commerce.stock）─────────────
    op.execute("""
        CREATE TABLE energy.battery (
          id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
          battery_sn text UNIQUE NOT NULL,
          model_code text NOT NULL REFERENCES energy.battery_model(model_code),
          owner_org_id bigint NOT NULL REFERENCES core.org(id),
          batch_no text,
          kwh_actual numeric(6,2),
          accepted_at timestamptz, accept_media_id bigint,
          cycles int NOT NULL DEFAULT 0,
          soc smallint CHECK (soc BETWEEN 0 AND 100),
          station_id bigint REFERENCES energy.station(id),
          status text NOT NULL DEFAULT 'in_stock' CHECK (status IN
            ('in_stock','charging','ready','leased','returned','faulty','repair',
             'retired','lost','stolen')),
          quality_complaints smallint NOT NULL DEFAULT 0,
          created_at timestamptz NOT NULL DEFAULT now(),
          updated_at timestamptz NOT NULL DEFAULT now(),
          -- 不在车上、也不是丢失态 → 必须在某个站（"一块电池同一时刻只有一个位置"的库内表达；
          -- 车上位置不在本表存列，唯一来源是 core.vehicle_battery_binding）
          CONSTRAINT ck_battery_place CHECK (
            status IN ('leased','lost','stolen') OR station_id IS NOT NULL)
        );
    """)
    op.execute("CREATE INDEX idx_battery_model ON energy.battery (model_code);")
    op.execute("CREATE INDEX idx_battery_owner ON energy.battery (owner_org_id);")
    op.execute("CREATE INDEX idx_battery_station ON energy.battery (station_id);")
    op.execute("""
        CREATE INDEX idx_battery_ready ON energy.battery (station_id, model_code)
          WHERE status = 'ready';
    """)

    # ── 1.4 流转流水（append-only，召回/丢失/纠纷举证唯一依据）────────────
    op.execute("""
        CREATE TABLE energy.battery_flow (
          id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
          battery_sn text NOT NULL,
          from_status text, to_status text NOT NULL,
          station_id bigint, vin char(17),
          swap_order_id bigint,
          actor_account_id bigint NOT NULL, actor_role text,
          note text, media_id bigint,
          created_at timestamptz NOT NULL DEFAULT now()
        );
    """)
    op.execute("""
        CREATE INDEX idx_battery_flow_sn ON energy.battery_flow (battery_sn, created_at);
    """)
    op.execute("""
        CREATE INDEX idx_battery_flow_swap ON energy.battery_flow (swap_order_id);
    """)

    # ── 1.5 "可用电池数"唯一来源（§10-24 的能源版）────────────────────────
    op.execute("""
        CREATE TABLE energy.station_stock (
          id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
          station_id bigint NOT NULL REFERENCES energy.station(id),
          model_code text NOT NULL REFERENCES energy.battery_model(model_code),
          ready_count smallint NOT NULL DEFAULT 0 CHECK (ready_count >= 0),
          charging_count smallint NOT NULL DEFAULT 0 CHECK (charging_count >= 0),
          faulty_count smallint NOT NULL DEFAULT 0 CHECK (faulty_count >= 0),
          updated_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE (station_id, model_code)
        );
    """)
    op.execute("CREATE INDEX idx_stock_station ON energy.station_stock (station_id);")

    # ── 1.6 按型号按次定价（版本化规则表，§11.0-6 通用模式）───────────────
    op.execute("""
        CREATE TABLE energy.swap_price (
          id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
          region_id char(2) REFERENCES core.region(country_code),
          model_code text NOT NULL REFERENCES energy.battery_model(model_code),
          station_org_id bigint REFERENCES core.org(id),
          price bigint NOT NULL CHECK (price >= 0),
          currency char(3) NOT NULL,
          min_soc_pct smallint NOT NULL DEFAULT 95 CHECK (min_soc_pct BETWEEN 50 AND 100),
          version int NOT NULL,
          effective_from date NOT NULL, effective_to date,
          status text NOT NULL DEFAULT 'draft'
            CHECK (status IN ('draft','pending_review','active','retired')),
          proposed_by bigint, approved_by bigint,
          created_at timestamptz NOT NULL DEFAULT now(),
          -- 兜底规则用 NULL 表示"全区 / 全运营商"，PG 默认 UNIQUE 视 NULL 互不相等 → 必须
          -- NULLS NOT DISTINCT（§11.0-6，012 那个坑）
          UNIQUE NULLS NOT DISTINCT (region_id, model_code, station_org_id, version)
        );
    """)
    op.execute("""
        CREATE INDEX idx_swap_price_lookup ON energy.swap_price (model_code, status);
    """)

    # ── 1.7 押金规则（按型号与地区；度数越大押金越高）─────────────────────
    op.execute("""
        CREATE TABLE energy.deposit_rule (
          id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
          region_id char(2) REFERENCES core.region(country_code),
          model_code text NOT NULL REFERENCES energy.battery_model(model_code),
          amount bigint NOT NULL CHECK (amount >= 0),
          currency char(3) NOT NULL,
          waiver_min_kyc smallint NOT NULL DEFAULT 3,
          version int NOT NULL,
          effective_from date NOT NULL, effective_to date,
          status text NOT NULL DEFAULT 'draft'
            CHECK (status IN ('draft','pending_review','active','retired')),
          proposed_by bigint, approved_by bigint,
          created_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE NULLS NOT DISTINCT (region_id, model_code, version)
        );
    """)

    # ── 1.8 换电协议：押金是生效前置（§16.6）─────────────────────────────
    op.execute("""
        CREATE TABLE energy.battery_lease (
          id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
          lease_no text UNIQUE NOT NULL,
          holder_type text NOT NULL CHECK (holder_type IN ('account','org')),
          holder_account_id bigint REFERENCES core.account(id),
          holder_org_id bigint REFERENCES core.org(id),
          operator_org_id bigint NOT NULL REFERENCES core.org(id),
          model_code text NOT NULL REFERENCES energy.battery_model(model_code),
          deposit_amount_snapshot bigint NOT NULL, currency char(3) NOT NULL,
          deposit_waived boolean NOT NULL DEFAULT false,
          terms_version text NOT NULL,
          terms_accepted_at timestamptz NOT NULL,
          status text NOT NULL DEFAULT 'draft'
            CHECK (status IN ('draft','active','overdue','terminated')),
          joined_at timestamptz, ended_at timestamptz,
          created_at timestamptz NOT NULL DEFAULT now(),
          updated_at timestamptz NOT NULL DEFAULT now(),
          CONSTRAINT ck_lease_holder_account CHECK (
            (holder_type = 'account') = (holder_account_id IS NOT NULL)),
          CONSTRAINT ck_lease_holder_org CHECK (
            (holder_type = 'org') = (holder_org_id IS NOT NULL))
        );
    """)
    # 两条而不是一条：UNIQUE 索引里 NULL 互不相等，合成一条会让"同一 org 两条生效协议"漏网
    op.execute("""
        CREATE UNIQUE INDEX uq_lease_active_account ON energy.battery_lease
          (holder_account_id, operator_org_id, model_code)
          WHERE status IN ('active','overdue') AND holder_account_id IS NOT NULL;
    """)
    op.execute("""
        CREATE UNIQUE INDEX uq_lease_active_org ON energy.battery_lease
          (holder_org_id, operator_org_id, model_code)
          WHERE status IN ('active','overdue') AND holder_org_id IS NOT NULL;
    """)
    op.execute("""
        CREATE INDEX idx_lease_operator ON energy.battery_lease (operator_org_id, status);
    """)

    # ── 1.9 押金台账：负债，不是收入（§16.7 / P28）────────────────────────
    op.execute("""
        CREATE TABLE energy.deposit_ledger (
          id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
          deposit_no text UNIQUE NOT NULL,
          lease_id bigint NOT NULL REFERENCES energy.battery_lease(id),
          payer_type text NOT NULL CHECK (payer_type IN ('account','org')),
          payer_id bigint NOT NULL,
          operator_org_id bigint NOT NULL REFERENCES core.org(id),
          amount bigint NOT NULL CHECK (amount > 0), currency char(3) NOT NULL,
          status text NOT NULL DEFAULT 'open'
            CHECK (status IN ('open','held','refunded','forfeited')),
          collected_at timestamptz, collected_voucher_no text,
          proof_media_id bigint,
          refunded_at timestamptz, refund_voucher_no text,
          forfeit_reason text, forfeit_swap_order_id bigint,
          created_at timestamptz NOT NULL DEFAULT now(),
          updated_at timestamptz NOT NULL DEFAULT now()
        );
    """)
    op.execute("""
        CREATE UNIQUE INDEX uq_deposit_open ON energy.deposit_ledger (lease_id)
          WHERE status IN ('open','held');
    """)
    op.execute("""
        CREATE INDEX idx_deposit_operator ON energy.deposit_ledger (operator_org_id, status);
    """)

    # ── 1.10 换电单：即时服务单，与 mobility.ride_order 同构 ──────────────
    op.execute("""
        CREATE TABLE energy.swap_order (
          id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
          swap_no text UNIQUE NOT NULL,
          station_id bigint NOT NULL REFERENCES energy.station(id),
          operator_org_id bigint NOT NULL REFERENCES core.org(id),
          -- 下面四列可空，是因为**拒单也要落这张表**（P29 的可观测性：白名单配错、押金缺失
          -- 都要能统计）。拒单发生在闸门哪一步，就可能还没有协议 / 型号 / 价目 / 币种。
          lease_id bigint REFERENCES energy.battery_lease(id),
          account_id bigint NOT NULL REFERENCES core.account(id),
          vin char(17) NOT NULL REFERENCES core.vehicle(vin),
          model_code text REFERENCES energy.battery_model(model_code),
          out_battery_sn text NOT NULL,
          in_battery_sn text,
          price_rule_id bigint REFERENCES energy.swap_price(id),
          pricing jsonb NOT NULL DEFAULT '{}',
          amount bigint NOT NULL DEFAULT 0, cash_amount bigint NOT NULL DEFAULT 0,
          points_offset bigint NOT NULL DEFAULT 0,
          currency char(3),
          collect_mode text NOT NULL DEFAULT 'operator_offline'
            CHECK (collect_mode IN ('operator_offline')),
          pay_order_id bigint REFERENCES fin.pay_order(id),
          idempotency_key text UNIQUE NOT NULL,
          status text NOT NULL DEFAULT 'created' CHECK (status IN
            ('created','verifying','swapped','priced','settled','completed',
             'rejected','disputed','pay_timeout','aborted')),
          reject_reason text CHECK (reject_reason IN
            ('model_not_allowed','model_mismatch','battery_not_registered',
             'battery_other_operator','binding_mismatch',
             'lease_inactive','deposit_missing','soc_below','battery_faulty',
             'risk_blocked','stock_empty')),
          re_swap_of bigint REFERENCES energy.swap_order(id),
          created_at timestamptz NOT NULL DEFAULT now(),
          completed_at timestamptz
        );
    """)
    op.execute("""
        CREATE INDEX idx_swap_account ON energy.swap_order (account_id, created_at DESC);
    """)
    op.execute("""
        CREATE INDEX idx_swap_station ON energy.swap_order (station_id, created_at DESC);
    """)
    op.execute("""
        CREATE INDEX idx_swap_vin ON energy.swap_order (vin, created_at DESC);
    """)
    op.execute("""
        CREATE INDEX idx_swap_out_sn ON energy.swap_order (out_battery_sn);
    """)
    op.execute("""
        CREATE INDEX idx_swap_in_sn ON energy.swap_order (in_battery_sn);
    """)
    op.execute("""
        CREATE INDEX idx_swap_open ON energy.swap_order (station_id, status)
          WHERE status IN ('created','verifying','swapped','priced');
    """)

    # ── 1.11 换电状态轨迹（append-only，照 mobility.ride_event）───────────
    op.execute("""
        CREATE TABLE energy.swap_event (
          id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
          swap_order_id bigint NOT NULL REFERENCES energy.swap_order(id),
          event text NOT NULL,
          payload jsonb NOT NULL DEFAULT '{}',
          actor_account_id bigint,
          created_at timestamptz NOT NULL DEFAULT now()
        );
    """)
    op.execute("""
        CREATE INDEX idx_swap_event_order ON energy.swap_event (swap_order_id, created_at);
    """)

    # ══ 2. core 侧配套 ═════════════════════════════════════════════════════
    op.execute("""
        CREATE TABLE core.org_group (
          id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
          name text NOT NULL,
          country_code char(2),
          tax_id_hash text,
          status text NOT NULL DEFAULT 'active',
          created_at timestamptz NOT NULL DEFAULT now()
        );
    """)
    op.execute("""
        ALTER TABLE core.org
          ADD COLUMN IF NOT EXISTS group_id bigint REFERENCES core.org_group(id);
    """)

    # 业务线 + 牌照：org_type 保留为"主业态"（CHECK 不动，向后兼容 M1–M2 代码）
    op.execute("""
        CREATE TABLE core.org_business (
          id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
          org_id bigint NOT NULL REFERENCES core.org(id),
          line text NOT NULL CHECK (line IN
            ('dealer','ride_operator','swap_operator','manufacturing')),
          license_no text, license_expiry date,
          status text NOT NULL DEFAULT 'active',
          approved_by bigint,
          created_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE (org_id, line)
        );
    """)
    op.execute("CREATE INDEX idx_org_business_line ON core.org_business (line, status);")

    # 运行时"车上装的是哪块电池"唯一来源（§4.5 / §10-24）
    op.execute("""
        CREATE TABLE core.vehicle_battery_binding (
          id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
          vin char(17) NOT NULL REFERENCES core.vehicle(vin),
          battery_sn text NOT NULL REFERENCES energy.battery(battery_sn),
          station_id bigint, swap_order_id bigint,
          reason text NOT NULL DEFAULT 'swap'
            CHECK (reason IN ('factory','swap','recall','transfer','retire','recheck')),
          effective_from timestamptz NOT NULL DEFAULT now(),
          ended_at timestamptz
        );
    """)
    op.execute("""
        CREATE UNIQUE INDEX uq_binding_active ON core.vehicle_battery_binding (vin)
          WHERE ended_at IS NULL;
    """)
    op.execute("""
        CREATE UNIQUE INDEX uq_binding_battery_active
          ON core.vehicle_battery_binding (battery_sn) WHERE ended_at IS NULL;
    """)
    op.execute("""
        CREATE INDEX idx_binding_vin_history ON core.vehicle_battery_binding
          (vin, effective_from DESC);
    """)

    # 车型侧双向声明（任一方缺配置 → 换电被拒）
    op.execute("""
        ALTER TABLE core.vehicle_type
          ADD COLUMN IF NOT EXISTS swap_compatible boolean NOT NULL DEFAULT false,
          ADD COLUMN IF NOT EXISTS battery_model_code text
            REFERENCES energy.battery_model(model_code);
    """)

    # ⚠ 撤销出厂 SN 的唯一约束：换电后一块电池先后属于多台车（§11.9 M15 特别注意）
    op.execute("DROP INDEX IF EXISTS core.uq_vehicle_battery_sn;")
    op.execute("""
        COMMENT ON COLUMN core.vehicle.battery_sn IS
          '出厂时装的电池 SN（出厂追溯/质保用）；运行时以 core.vehicle_battery_binding 为准';
    """)

    # ══ 3. fin：换电积分抵扣补贴（DROP + ADD，不是新表）════════════════════
    op.execute("ALTER TABLE fin.subsidy_ledger DROP CONSTRAINT IF EXISTS subsidy_ledger_reason_check;")
    op.execute("""
        ALTER TABLE fin.subsidy_ledger
          ADD CONSTRAINT subsidy_ledger_reason_check
          CHECK (reason IN ('points_offset','carpool_discount','swap_offset'));
    """)

    # ══ 4. rep 只读视图（kuma_ro 唯一可访问的业务出口，§11.12）═════════════
    op.execute("""
        CREATE OR REPLACE VIEW rep.swap_price_active AS
        SELECT p.region_id, p.model_code, p.station_org_id,
               p.price, p.currency, p.min_soc_pct, p.version, p.effective_from
        FROM energy.swap_price p
        WHERE p.status = 'active'
          AND p.effective_from <= current_date
          AND (p.effective_to IS NULL OR p.effective_to >= current_date);
    """)
    # 价格与库存必须公开（与"经销商视图不含库存与价格"相反，§16.4）
    op.execute("""
        CREATE OR REPLACE VIEW rep.station_nearby AS
        SELECT s.id AS station_id, s.code, s.name, s.org_id, s.region_id, s.address,
               s.business_hours, s.allowed_models, s.bay_count, s.needs_handling_aid,
               ST_AsText(s.geom) AS geom_wkt,
               COALESCE(jsonb_agg(jsonb_build_object(
                   'model_code', ss.model_code,
                   'ready', ss.ready_count,
                   'charging', ss.charging_count,
                   'faulty', ss.faulty_count,
                   'updated_at', ss.updated_at)
                   ORDER BY ss.model_code)
                   FILTER (WHERE ss.id IS NOT NULL), '[]'::jsonb) AS stock
        FROM energy.station s
        LEFT JOIN energy.station_stock ss ON ss.station_id = s.id
        WHERE s.status = 'active'
        GROUP BY s.id;
    """)
    # 押金三问：收了多少 / 退了多少 / 现在押着多少（P28 的可核对性）
    op.execute("""
        CREATE OR REPLACE VIEW rep.deposit_position AS
        SELECT d.operator_org_id, o.name AS operator_name, d.currency,
               COUNT(*)                                                        AS deposit_rows,
               COALESCE(SUM(d.amount) FILTER (WHERE d.collected_at IS NOT NULL), 0) AS collected,
               COALESCE(SUM(d.amount) FILTER (WHERE d.status = 'refunded'),   0)    AS refunded,
               COALESCE(SUM(d.amount) FILTER (WHERE d.status IN ('open','held')), 0) AS held_now,
               COALESCE(SUM(d.amount) FILTER (WHERE d.status = 'forfeited'),   0)    AS forfeited
        FROM energy.deposit_ledger d
        JOIN core.org o ON o.id = d.operator_org_id
        GROUP BY d.operator_org_id, o.name, d.currency;
    """)
    op.execute("""
        CREATE OR REPLACE VIEW rep.battery_trace AS
        SELECT b.battery_sn, b.model_code, m.kwh_nominal, m.chemistry,
               b.batch_no, b.status, b.soc, b.cycles, b.owner_org_id,
               b.station_id AS at_station_id,
               vb.vin       AS at_vin,
               f.id AS flow_id, f.from_status, f.to_status,
               f.station_id AS flow_station_id, f.vin AS flow_vin,
               f.swap_order_id, f.actor_account_id, f.note, f.created_at AS flow_at
        FROM energy.battery b
        JOIN energy.battery_model m ON m.model_code = b.model_code
        LEFT JOIN core.vehicle_battery_binding vb
               ON vb.battery_sn = b.battery_sn AND vb.ended_at IS NULL
        LEFT JOIN energy.battery_flow f ON f.battery_sn = b.battery_sn;
    """)

    # ══ 5. 授权（010 的 ON ALL TABLES 不覆盖后建对象，必须显式）════════════
    op.execute("GRANT USAGE ON SCHEMA energy TO kuma_app;")
    op.execute("""
        GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA energy TO kuma_app;
    """)
    op.execute("GRANT USAGE ON ALL SEQUENCES IN SCHEMA energy TO kuma_app;")
    # append-only（§11.0-5）：流转流水与换电轨迹只插不改
    op.execute("""
        REVOKE UPDATE, DELETE ON energy.battery_flow FROM kuma_app;
    """)
    op.execute("""
        REVOKE UPDATE, DELETE ON energy.swap_event FROM kuma_app;
    """)
    # 视图：kuma_app 读（服务层走 rep 出跨域数据），kuma_ro 读（报表/看板）
    for view in ("swap_price_active", "station_nearby", "deposit_position", "battery_trace"):
        op.execute(f"GRANT SELECT ON rep.{view} TO kuma_app;")
        op.execute(f"GRANT SELECT ON rep.{view} TO kuma_ro;")

    # ══ 6. search_path 扩 energy（照 0004：不加引号、带 public）═════════════
    op.execute(f"ALTER ROLE kuma_app IN DATABASE kuma SET search_path = {APP_PATH}")


def downgrade() -> None:
    op.execute(f"ALTER ROLE kuma_app IN DATABASE kuma SET search_path = {APP_PATH_OLD}")

    for view in ("battery_trace", "deposit_position", "station_nearby", "swap_price_active"):
        op.execute(f"DROP VIEW IF EXISTS rep.{view};")

    # fin CHECK 退回两值（若库里已有 swap_offset 行，这一步会失败——这是有意的：
    # 先清数据再回滚，不能让回滚悄悄把补贴记录变成非法值）
    op.execute("ALTER TABLE fin.subsidy_ledger DROP CONSTRAINT IF EXISTS subsidy_ledger_reason_check;")
    op.execute("""
        ALTER TABLE fin.subsidy_ledger
          ADD CONSTRAINT subsidy_ledger_reason_check
          CHECK (reason IN ('points_offset','carpool_discount'));
    """)

    # core 回滚
    op.execute("DROP TABLE IF EXISTS core.vehicle_battery_binding;")
    op.execute("""
        ALTER TABLE core.vehicle_type
          DROP COLUMN IF EXISTS battery_model_code,
          DROP COLUMN IF EXISTS swap_compatible;
    """)
    # ⚠ 不能无条件重建：换电后 core.vehicle.battery_sn 会出现重复值，直接建唯一索引必失败。
    #   回滚前必须按 vehicle_battery_binding 把出厂 SN 回填去重（§11.9 M15 特别注意 ①）。
    op.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_vehicle_battery_sn
          ON core.vehicle (battery_sn) WHERE battery_sn IS NOT NULL;
    """)
    op.execute("DROP TABLE IF EXISTS core.org_business;")
    op.execute("ALTER TABLE core.org DROP COLUMN IF EXISTS group_id;")
    op.execute("DROP TABLE IF EXISTS core.org_group;")

    # energy 整域删除（CASCADE 带走表与索引）。默认权限必须在删 schema **之前**收回：
    # schema 一旦不存在，`ALTER DEFAULT PRIVILEGES ... IN SCHEMA energy` 会直接报错。
    op.execute("""
        ALTER DEFAULT PRIVILEGES FOR ROLE kuma_owner IN SCHEMA energy
          REVOKE ALL ON TABLES FROM kuma_app;
    """)
    op.execute("""
        ALTER DEFAULT PRIVILEGES FOR ROLE kuma_owner IN SCHEMA energy
          REVOKE ALL ON SEQUENCES FROM kuma_app;
    """)
    op.execute("DROP SCHEMA IF EXISTS energy CASCADE;")
