"""KYC 取数与写入（`core.kyc_record` + `core.account.kyc_level`）。

`core.kyc_record` 从基线 `002_core.sql:90` 就在，但**此前全库零写入方**
（只被 GDPR 导出读、匿名化改状态）——所以本仓是它的第一份 SQL。
风格跟 `purchase_repo` 一致：裸 `text()` + 命名绑定 + `.mappings()`，列名集中在 `KYC_COLS`。

两条库级事实要记住：

- `uq_kyc_approved (account_id, level) WHERE status='approved'`（`002_core.sql:100`）：
  同账号同等级**只能有一条 approved**。并发双批不会造出两条通过记录，
  第二次会撞 23505 → services 翻译成 `kyc_already_approved`。
- `kyc_level` 在 `core.account` 上，语义是"已授予的最高等级"。
  买积分闸门读的就是它（`purchase_repo.account_region`），**不读 JWT 里的快照**
  （E7 缺陷②：令牌签发后等级变了，用快照等于用旧数据放行）。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

KYC_COLS = ("id, account_id, level, id_type, id_number_hash, docs, status, "
            "reviewed_by, reviewed_at, created_at")


def _strip_total(rows: Sequence[Any]) -> tuple[list[dict[str, Any]], int]:
    """把 `count(*) OVER() AS total` 摘成分页元数据（与 `purchase_repo._strip_total` 同理由）。"""
    items: list[dict[str, Any]] = []
    total = 0
    for r in rows:
        d = dict(r)
        total = int(d.pop("total"))
        items.append(d)
    return items, total


class KycRepo:
    # ── 本人侧 ────────────────────────────────────────────────────────────────
    async def records_of_account(self, session: AsyncSession, account_id: int, *,
                                 limit: int, offset: int) -> tuple[list[dict[str, Any]], int]:
        rows = (await session.execute(text(f"""
            SELECT {KYC_COLS}, count(*) OVER() AS total
              FROM core.kyc_record
             WHERE account_id = :id
             ORDER BY id DESC LIMIT :lim OFFSET :off
        """), {"id": account_id, "lim": limit, "off": offset})).mappings().all()
        return _strip_total(list(rows))

    async def open_or_approved_at_level(self, session: AsyncSession, account_id: int,
                                        level: int) -> dict[str, Any] | None:
        """同等级上是否已有 `pending`（在审）或 `approved`（已过）的记录。

        只看这两态：`rejected` 就是要允许重提的（补件），`revoked` 是 GDPR 匿名化留下的
        空壳（证件号哈希已清），重新申请必须能新建一行。
        """
        row = (await session.execute(text(f"""
            SELECT {KYC_COLS} FROM core.kyc_record
             WHERE account_id = :id AND level = :lv AND status IN ('pending','approved')
             ORDER BY id DESC LIMIT 1
        """), {"id": account_id, "lv": level})).mappings().one_or_none()
        return dict(row) if row else None

    async def insert(self, session: AsyncSession, *, account_id: int, level: int,
                     id_type: str, id_number_hash: str | None,
                     docs: dict[str, Any]) -> dict[str, Any]:
        """新建待审记录。`docs` 必须 `json.dumps`（`text()` 绑定不带列类型，
        asyncpg 收到 dict 直接 DataError——`Task-Server.md` §7 铁律 7）。"""
        row = (await session.execute(text(f"""
            INSERT INTO core.kyc_record (account_id, level, id_type, id_number_hash, docs, status)
            VALUES (:account_id, :level, :id_type, :id_number_hash, CAST(:docs AS jsonb),
                    'pending')
            RETURNING {KYC_COLS}
        """), {"account_id": account_id, "level": level, "id_type": id_type,
               "id_number_hash": id_number_hash, "docs": json.dumps(docs),
               })).mappings().one()
        await session.flush()
        return dict(row)

    # ── 平台侧：队列与审核 ─────────────────────────────────────────────────────
    async def record_by_id(self, session: AsyncSession, kyc_id: int) -> dict[str, Any] | None:
        row = (await session.execute(text(f"""
            SELECT k.{', k.'.join(KYC_COLS.split(', '))},
                   a.username, a.phone, a.region_id, a.kyc_level AS account_kyc_level
              FROM core.kyc_record k
              JOIN core.account a ON a.id = k.account_id
             WHERE k.id = :id
        """), {"id": kyc_id})).mappings().one_or_none()
        return dict(row) if row else None

    async def queue_page(self, session: AsyncSession, *, status: str, region_id: str | None,
                         level: int | None, limit: int, offset: int
                         ) -> tuple[list[dict[str, Any]], int]:
        """审核队列（按状态翻页，先到先审）。

        join `core.account` 是为了让审核人不用二次查询就知道"这是谁、哪个地区、现在几级"——
        账号现有等级高于申请等级时应当直接拒（重复申请/刷等级）。

        **可选条件按房内写法拼进 WHERE**（只在传了时才加），不写
        `(:region IS NULL OR a.region_id = :region)`：asyncpg 对"参数自己判 NULL"
        推不出类型，实测报 `could not determine data type of parameter $2`
        （`points_repo` / `energy_repo` 一律把**列**放在 `IS NULL` 左边，同一个道理）。
        """
        where = ["k.status = :status"]
        p: dict[str, Any] = {"status": status, "lim": max(1, min(limit, 200)),
                             "off": max(0, offset)}
        if region_id:
            where.append("a.region_id = :region")
            p["region"] = region_id
        if level:
            where.append("k.level = :level")
            p["level"] = level
        rows = (await session.execute(text(f"""
            SELECT k.{', k.'.join(KYC_COLS.split(', '))},
                   a.username, a.phone, a.region_id, a.kyc_level AS account_kyc_level,
                   count(*) OVER() AS total
              FROM core.kyc_record k
              JOIN core.account a ON a.id = k.account_id
             WHERE {' AND '.join(where)}
             ORDER BY k.created_at, k.id
             LIMIT :lim OFFSET :off
        """), p)).mappings().all()
        return _strip_total(list(rows))

    async def set_review(self, session: AsyncSession, *, kyc_id: int, status: str,
                         reviewer: int, docs: dict[str, Any]) -> dict[str, Any] | None:
        """落审核结论。`WHERE status='pending'` 是并发防线：
        两个平台员工同时点通过，第二个拿到 0 行 → services 报 `kyc_reviewed`。"""
        row = (await session.execute(text(f"""
            UPDATE core.kyc_record
               SET status = :s, reviewed_by = :by, reviewed_at = now(),
                   docs = CAST(:docs AS jsonb)
             WHERE id = :id AND status = 'pending'
             RETURNING {KYC_COLS}
        """), {"s": status, "by": reviewer, "docs": json.dumps(docs),
               "id": kyc_id})).mappings().one_or_none()
        await session.flush()
        return dict(row) if row else None

    async def raise_account_level(self, session: AsyncSession, account_id: int,
                                  level: int) -> int | None:
        """把 `core.account.kyc_level` 抬到 `level`（**只升不降**）。

        `WHERE kyc_level < :lv` 而不是无条件 UPDATE：降级必须是显式动作
        （GDPR 匿名化才把等级清 0），审核通过不该有"顺手把别人降下去"的能力。
        返回 None = 已是同级或更高（不是错误，调用方按"无需变更"处理）。
        """
        got = (await session.execute(text("""
            UPDATE core.account SET kyc_level = :lv
             WHERE id = :id AND kyc_level < :lv RETURNING kyc_level
        """), {"lv": level, "id": account_id})).scalar()
        await session.flush()
        return None if got is None else int(got)

    # ── 校验用 ─────────────────────────────────────────────────────────────────
    async def media_of_account(self, session: AsyncSession, account_id: int,
                               media_ids: list[int]) -> list[int]:
        """这些 media 是不是**该账号自己的 active 文件**。

        证件照片必须先经 `POST /media`（`owner_type='account'`，`media_service` 已保证
        只能传自己的、字节头要对）再拿 id 来提交 KYC。这里复核归属，
        否则任何人都能把别人的照片挂到自己实名单上。
        """
        if not media_ids:
            return []
        rows = (await session.execute(text("""
            SELECT id FROM core.media
             WHERE owner_type = 'account' AND owner_id = :acc AND status = 'active'
               AND id = ANY(:ids)
        """), {"acc": account_id, "ids": media_ids})).scalars().all()
        return list(rows)
