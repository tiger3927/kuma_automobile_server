"""新建 `commerce.lead`——潜客意向表（`Task-Server.md` §18.3，guest 角色 A 的落点）

## 为什么是**新表**而不是复用现有表

`Task-Plan.md` §9.1 把 guest 定为四类人共用一个端，第一类就是**潜在客户**（注册了但还没买车，
在比价、找经销商）。而 `Task-DataBase.md` §11.2 的 `commerce` 12 张表里**没有这张表**——
不是"有表无代码"，是压根没有。所以这条链的唯一库侧前置就是这张表。

它刻意不依赖订单骨架（`commerce.order`）：意向登记发生在买车链的最上游，此时既没有 SKU、
也没有 VIN、也没有收款方式。把 lead 挂到订单上，等于让"官网能不能带来意向"这件事
必须等 M8/M9 整条链跑通才能验证——而它恰恰是最便宜、最能提前验证商业假设的一片。

## 三个字段的取舍

- **`account_id` 可空**：portal 官网在登录前就要能收意向（`Task-Web.md` §7.4 的未登录入口）。
  但配一条 `CHECK (account_id IS NOT NULL OR contact_phone IS NOT NULL)`：
  两者皆空的线索既无法回访也无法归因到任何转化统计，留着只是垃圾，不如在入口就拒。
- **`intent_dealer_org_id` 与 `assigned_dealer_org_id` 分开**：前者是用户自选
  （"我想去这家店"），后者是系统/平台分派结果。两者不一致是常态
  （就近但没该品类授权、或用户指定但没库存），合成一个字段就看不出"派错了"这件事。
- **`payload jsonb`**：来源页、utm、留言、提交时的坐标。不给这些留位置的话，
  运营只能靠改表来加一个来源，而来源渠道恰恰是这张表最主要的分析维度。

## 索引按"谁要查它"建，不预建

- `idx_lead_dealer (assigned_dealer_org_id, status, created_at DESC)`：经销商跟进看板
  （`Task-Plan.md` §9.3）——这是这张表能不能闭环的关键查询，没索引就是全表扫。
- `idx_lead_account ... WHERE account_id IS NOT NULL`：用户侧"我登记过的意向"。
- `uq_lead_fresh_phone`（**部分唯一**）：同一手机号对同一车型在 `new` 态只能有一条。
  没有它，公开端点会被同一人反复提交刷满，经销商看板第一天就没人信。
  `intent_type_code` 用 `COALESCE(...,'')` 是因为 PG 唯一索引里 NULL 互不相等，
  不兜住的话"没选车型的重复提交"就绕过了这道防线。
  ⚠ 这是**防刷不是防错**：用户换号或先 phone 后注册都会绕过，别把它当业务唯一键。

## 授权不用写

`migrations/sql/010_grants_appendonly.sql:11-12` 已给 `commerce` 设了默认权限
（`ALTER DEFAULT PRIVILEGES FOR ROLE kuma_owner IN SCHEMA commerce ... TO kuma_app`），
本表由 `kuma_owner` 创建 ⇒ `kuma_app` 自动带 SELECT/INSERT/UPDATE/DELETE，
序列也在 `010:15-16` 的默认权限里。与迁移 0008 同一结论（`0008_points_offset.py:27-28`）。

Revision ID: 0012_commerce_lead
Revises: 0011_rep_grants_for_kuma_app
"""
from __future__ import annotations

from alembic import op

revision = "0012_commerce_lead"
down_revision = "0011_rep_grants"
branch_labels = None
depends_on = None

#: 与 `core/scope.py` 的 lead 矩阵、`schemas/commerce.py` 的 Literal 逐字一致
LEAD_STATUS = ("new", "contacted", "visited", "won", "lost")
LEAD_SOURCE = ("portal", "app", "dealer", "exhibition", "campaign")


def _in_check(col: str, values: tuple[str, ...]) -> str:
    vals = ", ".join(f"'{v}'" for v in values)
    return f"CHECK ({col} IN ({vals}))"


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS commerce.lead (
            id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            lead_no text UNIQUE NOT NULL,
            account_id bigint REFERENCES core.account,
            region_id char(2) NOT NULL REFERENCES core.region,
            contact_name text NOT NULL,
            contact_phone text,
            intent_type_code text REFERENCES core.vehicle_type,
            intent_dealer_org_id bigint REFERENCES core.org,
            assigned_dealer_org_id bigint REFERENCES core.org,
            source text NOT NULL DEFAULT 'portal',
            status text NOT NULL DEFAULT 'new',
            followed_by bigint REFERENCES core.account,
            note text,
            payload jsonb NOT NULL DEFAULT '{}',
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT lead_has_contact
                CHECK (account_id IS NOT NULL OR contact_phone IS NOT NULL)
        )
    """)
    op.execute(f"ALTER TABLE commerce.lead ADD CONSTRAINT lead_status_check "
               f"{_in_check('status', LEAD_STATUS)}")
    op.execute(f"ALTER TABLE commerce.lead ADD CONSTRAINT lead_source_check "
               f"{_in_check('source', LEAD_SOURCE)}")

    op.execute("""
        CREATE INDEX idx_lead_dealer
            ON commerce.lead (assigned_dealer_org_id, status, created_at DESC)
    """)
    op.execute("""
        CREATE INDEX idx_lead_account
            ON commerce.lead (account_id, created_at DESC)
            WHERE account_id IS NOT NULL
    """)
    op.execute("""
        CREATE UNIQUE INDEX uq_lead_fresh_phone
            ON commerce.lead (lower(contact_phone), COALESCE(intent_type_code, ''))
            WHERE status = 'new' AND contact_phone IS NOT NULL
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_lead_fresh_phone")
    op.execute("DROP INDEX IF EXISTS idx_lead_account")
    op.execute("DROP INDEX IF EXISTS idx_lead_dealer")
    op.execute("DROP TABLE IF EXISTS commerce.lead")
