"""潜客意向取数与写入（`commerce.lead`）。

风格跟 `kyc_repo` / `purchase_repo` 一致：裸 `text()` + 命名绑定 + `.mappings()`，
列名集中在 `LEAD_COLS`。**不在这里判权限**——services 判"能不能"，这里只把
"看得见哪些"作为参数拼进 WHERE（`Task-Server.md` §7 铁律：api 不写 SQL，
repository 不判身份）。

三条库级事实要记住：

- `lead_has_contact`（迁移 `0012`）：`account_id` 与 `contact_phone` 至少一个非空。
  公开端点漏了手机号会在 INSERT 时撞 23502/23514，`exceptions._integrity_mapping`
  翻译成 422 `constraint_violation`——**services 必须提前拒**，别把约束错误当业务错误返回。
- `uq_lead_fresh_phone`（部分唯一，`status='new' AND contact_phone IS NOT NULL`）：
  同一手机号 + 同一车型的重复意向会被 23505 拒。services 要把它翻译成
  `lead_duplicate`（409），否则前端只能看到笼统的 `duplicate`。
- `assigned_dealer_org_id` 可以为 NULL（未分派）。经销商看板按它过滤，
  **未分派的线索对经销商不可见**——这是有意的：没派就是没主，不能让所有经销商
  都看到同一条线索互相抢。平台侧按 `status='new'` 的未分派队列补派。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

LEAD_COLS = ("id, lead_no, account_id, region_id, contact_name, contact_phone, "
             "intent_type_code, intent_dealer_org_id, assigned_dealer_org_id, "
             "source, status, followed_by, note, payload, created_at, updated_at")


def _strip_total(rows: Sequence[Any]) -> tuple[list[dict[str, Any]], int]:
    """把 `count(*) OVER() AS total` 摘成分页元数据（与 `kyc_repo._strip_total` 同理由）。"""
    items: list[dict[str, Any]] = []
    total = 0
    for r in rows:
        d = dict(r)
        total = int(d.pop("total"))
        items.append(d)
    return items, total


class LeadRepo:
    async def insert(self, session: AsyncSession, *, lead_no: str, account_id: int | None,
                     region_id: str, contact_name: str, contact_phone: str | None,
                     intent_type_code: str | None, intent_dealer_org_id: int | None,
                     assigned_dealer_org_id: int | None, source: str, note: str | None,
                     payload: dict[str, Any]) -> dict[str, Any]:
        """新建意向。`payload` 必须 `json.dumps`（`text()` 绑定不带列类型，
        asyncpg 收到 dict 直接 DataError——`Task-Server.md` §7 铁律 7）。"""
        row = (await session.execute(text(f"""
            INSERT INTO commerce.lead
                (lead_no, account_id, region_id, contact_name, contact_phone,
                 intent_type_code, intent_dealer_org_id, assigned_dealer_org_id,
                 source, note, payload)
            VALUES (:lead_no, :account_id, :region_id, :contact_name, :contact_phone,
                    :intent_type_code, :intent_dealer_org_id, :assigned_dealer_org_id,
                    :source, :note, CAST(:payload AS jsonb))
            RETURNING {LEAD_COLS}
        """), {"lead_no": lead_no, "account_id": account_id, "region_id": region_id,
               "contact_name": contact_name, "contact_phone": contact_phone,
               "intent_type_code": intent_type_code,
               "intent_dealer_org_id": intent_dealer_org_id,
               "assigned_dealer_org_id": assigned_dealer_org_id,
               "source": source, "note": note, "payload": json.dumps(payload)})).mappings().one()
        await session.flush()
        return dict(row)

    async def by_id(self, session: AsyncSession, lead_id: int) -> dict[str, Any] | None:
        row = (await session.execute(text(f"""
            SELECT {LEAD_COLS} FROM commerce.lead WHERE id = :id
        """), {"id": lead_id})).mappings().one_or_none()
        return dict(row) if row else None

    async def page(self, session: AsyncSession, *, owner_account_id: int | None,
                   dealer_org_ids: list[int] | None, status: str | None,
                   region_id: str | None, unassigned: bool, q: str | None,
                   limit: int, offset: int) -> tuple[list[dict[str, Any]], int]:
        """按调用方给的**范围参数**翻页（身份判断在 services：
        本人传 `owner_account_id`、经销商传 `dealer_org_ids`、平台两个都不传）。

        `unassigned=True` 只看未分派队列（平台补派用）。
        可选条件按房内写法拼进 WHERE，不写 `(:p IS NULL OR col = :p)`——
        asyncpg 对"参数自己判 NULL"推不出类型（`kyc_repo.queue_page` 同一教训）。
        """
        where: list[str] = []
        p: dict[str, Any] = {"lim": max(1, min(limit, 200)), "off": max(0, offset)}
        if owner_account_id is not None:
            where.append("account_id = :acc")
            p["acc"] = owner_account_id
        if dealer_org_ids is not None:
            if not dealer_org_ids:
                return [], 0          # 没有任何 org 归属：空集，不放行
            where.append("assigned_dealer_org_id = ANY(:dealers)")
            p["dealers"] = dealer_org_ids
        if unassigned:
            where.append("assigned_dealer_org_id IS NULL")
        if status:
            where.append("status = :status")
            p["status"] = status
        if region_id:
            where.append("region_id = :region")
            p["region"] = region_id
        if q:
            where.append("(contact_name ILIKE :q OR contact_phone ILIKE :q OR lead_no ILIKE :q)")
            p["q"] = f"%{q}%"
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        rows = (await session.execute(text(f"""
            SELECT {LEAD_COLS}, count(*) OVER() AS total
              FROM commerce.lead
             {clause}
             ORDER BY created_at DESC, id DESC
             LIMIT :lim OFFSET :off
        """), p)).mappings().all()
        return _strip_total(list(rows))

    async def set_status(self, session: AsyncSession, *, lead_id: int, status: str,
                         followed_by: int, note: str | None) -> dict[str, Any] | None:
        """落状态流转。`updated_at = now()` 显式写（库里没有触发器，别指望自动更新）。

        返回 None = 这一行不存在或已被别人改过，services 按 `lead_not_found` /
        `lead_changed` 处理——并发跟进是同一条线索上最常见的竞争。
        """
        extra = ", note = :note" if note is not None else ""
        if note is not None:
            p: dict[str, Any] = {"id": lead_id, "s": status, "by": followed_by, "note": note}
        else:
            p = {"id": lead_id, "s": status, "by": followed_by}
        row = (await session.execute(text(f"""
            UPDATE commerce.lead
               SET status = :s, followed_by = :by, updated_at = now(){extra}
             WHERE id = :id
             RETURNING {LEAD_COLS}
        """), p)).mappings().one_or_none()
        await session.flush()
        return dict(row) if row else None

    async def reassign(self, session: AsyncSession, *, lead_id: int,
                       dealer_org_id: int) -> dict[str, Any] | None:
        """重新分派（只有平台能调，裁决在 services）。"""
        row = (await session.execute(text(f"""
            UPDATE commerce.lead
               SET assigned_dealer_org_id = :org, updated_at = now()
             WHERE id = :id
             RETURNING {LEAD_COLS}
        """), {"id": lead_id, "org": dealer_org_id})).mappings().one_or_none()
        await session.flush()
        return dict(row) if row else None

    # ── 校验与分派用 ───────────────────────────────────────────────────────────
    async def account_region(self, session: AsyncSession, account_id: int) -> str | None:
        """登录用户的地区（`core.account.region_id`）。

        登录侧的意向必须落在**账号自己的地区**，不接受前端传 `region_id`：
        否则一个人可以给任意地区的看板灌线索（`Principal` 里没有 region，只能查）。
        """
        return (await session.execute(text(
            "SELECT region_id FROM core.account WHERE id = :id"
        ), {"id": account_id})).scalar()

    async def vehicle_type_exists(self, session: AsyncSession, type_code: str) -> bool:
        """车型是否存在（`core.vehicle_type.type_code`）。

        **只判存在、不判 status**：对一款已下架的车表达意向是合理的（等复产、买二手），
        拒掉它只会让用户在表单里乱填一个还在售的码。FK 本身也会挡，但那是原始
        `IntegrityError` → 全局处理器只能给笼统的 `reference_not_found`；
        公开端点无鉴权、表单填错车型码是常态，所以在这里提前给明确业务码。
        """
        return bool((await session.execute(text(
            "SELECT 1 FROM core.vehicle_type WHERE type_code = :c"
        ), {"c": type_code})).scalar())

    async def active_dealer(self, session: AsyncSession, org_id: int) -> dict[str, Any] | None:
        """这个 org 是不是**在职经销商**（分派目标必须是，否则线索进黑洞）。"""
        row = (await session.execute(text("""
            SELECT id, name, region_id, lat, lng FROM core.org
             WHERE id = :id AND org_type = 'dealer' AND status = 'active'
        """), {"id": org_id})).mappings().one_or_none()
        return dict(row) if row else None

    async def nearest_dealer(self, session: AsyncSession, *, region_id: str,
                             lat: float, lng: float) -> dict[str, Any] | None:
        """就近取一家有坐标的在职经销商（**平方度距离**，不是大圆距离）。

        为什么不用 PostGIS：`core.org` 的坐标是迁移 0010 加的 `double precision` +
        范围 CHECK，注释写死了"门店附近查询在 v1 量级下够用，不给 core 表增加 PostGIS 依赖"。
        平方度排序在同一纬度带内与真实距离单调一致，用来**分派**足够；
        要给用户显示"多少公里"请用 `shared/geo` 那套口径，别把这个数当距离返回。

        只按 `region_id` 收窄，不校验该经销商有没有该品类授权——授权与库存属于 M8 的
        `stock` / `vehicle_listing`（现在还没有写入方）。分派错了平台可以改派，
        比"因为查不到授权就不分派"更能让线索被跟进。
        """
        row = (await session.execute(text("""
            SELECT id, name, region_id, lat, lng,
                   ((lat - :lat) * (lat - :lat) + (lng - :lng) * (lng - :lng)) AS d2
              FROM core.org
             WHERE org_type = 'dealer' AND status = 'active'
               AND region_id = :region AND lat IS NOT NULL AND lng IS NOT NULL
             ORDER BY d2
             LIMIT 1
        """), {"lat": lat, "lng": lng, "region": region_id})).mappings().one_or_none()
        return dict(row) if row else None

    async def recent_duplicate(self, session: AsyncSession, *, contact_phone: str,
                               intent_type_code: str | None) -> dict[str, Any] | None:
        """同手机号 + 同车型是否已有 `new` 态意向（给前端一个明确原因，而不是撞约束）。"""
        where = ["lower(contact_phone) = lower(:phone)", "status = 'new'"]
        p: dict[str, Any] = {"phone": contact_phone}
        if intent_type_code:
            where.append("intent_type_code = :tc")
            p["tc"] = intent_type_code
        else:
            where.append("intent_type_code IS NULL")
        row = (await session.execute(text(f"""
            SELECT id, lead_no, created_at FROM commerce.lead
             WHERE {' AND '.join(where)}
             ORDER BY id DESC LIMIT 1
        """), p)).mappings().one_or_none()
        return dict(row) if row else None
