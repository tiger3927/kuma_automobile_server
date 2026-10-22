"""积分账本的读写（`mkt.points_*` + `fin.subsidy_ledger`）。

**并发与顺序是这里的唯一难点**，两条硬规矩：

1. **锁顺序固定：先 `points_account` 行，再 `points_batch` 行**。
   任何要扣积分的路径都按这个顺序拿锁；反序就会和别的扣减路径互相死锁
   （换电那边同一课：`energy_repo.lock_batteries` 按 SN 升序锁两块，道理一样）。
   账户行是"一人一把锁"，天然串行化同一账号的并发消耗；批次行按 `expires_at`
   升序锁，保证两条路径拿批次的顺序也一致。
2. **`points_ledger` 是 append-only**（库里对 `kuma_app` REVOKE UPDATE/DELETE），
   所以"退回"只能插一条**正数**流水；`balance_after` 记的是插完之后的快照值，
   对账时 Σ(delta) 必须等于 `points_account` 的两列（§7.6 三方比对）。

`uq_ledger_source (source_type, source_ref, points_kind)` 是幂等的最后一道防线：
同一笔业务单对同一类积分只能记一次流水，重复扣/重复发在库里直接被拒——
services 层的幂等检查只是让它变成可读的错误，不是正确性的来源。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

ACCOUNT_COLS = "account_id, earned_balance, purchased_balance, frozen, updated_at"
BATCH_COLS = ("id, account_id, points_kind, total, remaining, expires_at, "
              "source_type, source_ref")
LEDGER_COLS = ("id, account_id, points_kind, delta, balance_after, source_type, source_ref, "
               "memo, created_at")
RULE_COLS = ("id, rule_code, biz_line, region_id, currency, points_per_unit, unit_scale, "
             "max_offset_pct, min_amount, version, effective_from, effective_to, status, "
             "proposed_by, approved_by")


class PointsRepo:
    # ══ 账户与批次 ═════════════════════════════════════════════════════════
    async def get_account(self, session: AsyncSession, account_id: int) -> dict[str, Any] | None:
        row = (await session.execute(text(
            f"SELECT {ACCOUNT_COLS} FROM mkt.points_account WHERE account_id = :id"
        ), {"id": account_id})).mappings().one_or_none()
        return dict(row) if row else None

    async def lock_account(self, session: AsyncSession, account_id: int) -> dict[str, Any]:
        """拿到（必要时创建）账户行并加行锁——**扣积分的第一步，永远是它**。

        `INSERT … ON CONFLICT DO NOTHING` 再 `SELECT … FOR UPDATE`：
        不能只 SELECT FOR UPDATE，因为新账号还没有账户行，会锁不到任何东西。
        """
        await session.execute(text(
            "INSERT INTO mkt.points_account (account_id) VALUES (:id) "
            "ON CONFLICT (account_id) DO NOTHING"
        ), {"id": account_id})
        row = (await session.execute(text(
            f"SELECT {ACCOUNT_COLS} FROM mkt.points_account WHERE account_id = :id FOR UPDATE"
        ), {"id": account_id})).mappings().one()
        return dict(row)

    async def lock_batches(self, session: AsyncSession, account_id: int) -> list[dict[str, Any]]:
        """可花批次，按 `expires_at` **升序**并加锁（§7.6：先扣快过期的，对用户有利）。

        过期批次在这里就被排除（`expires_at > now()`）——**过期不是"扣失败"，
        而是根本不进候选集**，所以不会出现"扣了一块已过期的积分"。
        """
        rows = (await session.execute(text(f"""
            SELECT {BATCH_COLS} FROM mkt.points_batch
             WHERE account_id = :id AND remaining > 0 AND expires_at > now()
             ORDER BY expires_at, id
               FOR UPDATE
        """), {"id": account_id})).mappings().all()
        return [dict(r) for r in rows]

    async def spend_batches(self, session: AsyncSession, *, updates: list[tuple[int, int]]
                            ) -> None:
        """按批次扣减（`updates = [(batch_id, 扣多少)]`）。

        `remaining >= :amt` 是最后一道防线：算错或并发漏锁时，扣超会更新 0 行，
        services 层据此报错，而不是把 `remaining` 做成负数。
        """
        for batch_id, amount in updates:
            got = (await session.execute(text("""
                UPDATE mkt.points_batch SET remaining = remaining - :amt
                 WHERE id = :id AND remaining >= :amt RETURNING id
            """), {"id": batch_id, "amt": amount})).scalar()
            if got is None:
                raise RuntimeError(f"批次 {batch_id} 扣减 {amount} 失败（余额或并发问题）")

    async def restore_batches(self, session: AsyncSession, *, account_id: int,
                              points_kind: str, amount: int, source_type: str,
                              source_ref: str, expires_at: datetime) -> int:
        """退回积分：优先回补原批次（由调用方给 updates），否则**新开一个批次**。

        原批次可能已过期或被扣光——这时不能"复活"旧批次（那会让过期规则失效），
        只能新开一批并给一个新的到期时间。返回新批次 id。
        """
        row = (await session.execute(text("""
            INSERT INTO mkt.points_batch (account_id, points_kind, total, remaining,
                                          expires_at, source_type, source_ref)
            VALUES (:id, :kind, :amt, :amt, :exp, :st, :sr)
            RETURNING id
        """), {"id": account_id, "kind": points_kind, "amt": amount, "exp": expires_at,
               "st": source_type, "sr": source_ref})).mappings().one()
        await session.flush()
        return int(row["id"])

    async def set_balances(self, session: AsyncSession, *, account_id: int,
                           earned: int, purchased: int) -> None:
        """更新余额快照（调用方必须已持账户行锁）。"""
        await session.execute(text(
            "UPDATE mkt.points_account SET earned_balance = :e, purchased_balance = :p, "
            "updated_at = now() WHERE account_id = :id"
        ), {"id": account_id, "e": earned, "p": purchased})
        await session.flush()

    async def set_frozen(self, session: AsyncSession, *, account_id: int, frozen: bool
                         ) -> None:
        await session.execute(text(
            "UPDATE mkt.points_account SET frozen = :f, updated_at = now() "
            "WHERE account_id = :id"
        ), {"id": account_id, "f": frozen})
        await session.flush()

    # ══ 流水（append-only）══════════════════════════════════════════════════
    async def insert_ledger(self, session: AsyncSession, *, account_id: int, points_kind: str,
                            delta: int, balance_after: int, source_type: str, source_ref: str,
                            memo: str | None = None) -> dict[str, Any]:
        row = (await session.execute(text(f"""
            INSERT INTO mkt.points_ledger (account_id, points_kind, delta, balance_after,
                                           source_type, source_ref, memo)
            VALUES (:account_id, :points_kind, :delta, :balance_after,
                    :source_type, :source_ref, :memo)
            RETURNING {LEDGER_COLS}
        """), {"account_id": account_id, "points_kind": points_kind, "delta": delta,
               "balance_after": balance_after, "source_type": source_type,
               "source_ref": source_ref, "memo": memo})).mappings().one()
        await session.flush()
        return dict(row)

    async def has_ledger(self, session: AsyncSession, *, source_type: str, source_ref: str,
                         points_kind: str | None = None) -> bool:
        """幂等预查（真正的兜底是 `uq_ledger_source`）。"""
        sql = "SELECT 1 FROM mkt.points_ledger WHERE source_type = :st AND source_ref = :sr"
        p: dict[str, Any] = {"st": source_type, "sr": source_ref}
        if points_kind is not None:
            sql += " AND points_kind = :k"
            p["k"] = points_kind
        return (await session.execute(text(sql + " LIMIT 1"), p)).scalar() is not None

    async def ledger_of(self, session: AsyncSession, account_id: int, *, limit: int = 50,
                        offset: int = 0) -> list[dict[str, Any]]:
        rows = (await session.execute(text(f"""
            SELECT {LEDGER_COLS} FROM mkt.points_ledger
             WHERE account_id = :id ORDER BY created_at DESC, id DESC LIMIT :limit OFFSET :offset
        """), {"id": account_id, "limit": limit, "offset": offset})).mappings().all()
        return [dict(r) for r in rows]

    async def expiring_soon(self, session: AsyncSession, account_id: int, *, days: int = 30
                            ) -> int:
        """`days` 天内到期的可花积分（用户端"过期提醒"，§7.6 到期前 30 天推送）。"""
        return int((await session.execute(text("""
            SELECT COALESCE(SUM(remaining), 0) FROM mkt.points_batch
             WHERE account_id = :id AND remaining > 0 AND expires_at > now()
               AND expires_at <= now() + make_interval(days => :d)
        """), {"id": account_id, "d": days})).scalar() or 0)

    # ══ 抵扣规则（版本化，站级/区域/全区兜底同 swap_price）═══════════════════
    async def effective_offset_rule(self, session: AsyncSession, *, biz_line: str,
                                    currency: str, region_id: str | None,
                                    ) -> dict[str, Any] | None:
        rows = (await session.execute(text(f"""
            SELECT {RULE_COLS} FROM mkt.points_offset_rule
             WHERE biz_line = :bl AND currency = :cur AND status = 'active'
               AND effective_from <= current_date
               AND (effective_to IS NULL OR effective_to >= current_date)
               AND (region_id IS NULL OR region_id = :region)
             ORDER BY (region_id IS NULL), version DESC
             LIMIT 1
        """), {"bl": biz_line, "cur": currency, "region": region_id})).mappings().all()
        return dict(rows[0]) if rows else None

    async def insert_offset_rule(self, session: AsyncSession, *, rule_code: str, biz_line: str,
                                 region_id: str | None, currency: str, points_per_unit: int,
                                 unit_scale: int, max_offset_pct: int, min_amount: int | None,
                                 version: int, effective_from: date, effective_to: date | None,
                                 status: str, proposed_by: int) -> dict[str, Any]:
        row = (await session.execute(text(f"""
            INSERT INTO mkt.points_offset_rule (rule_code, biz_line, region_id, currency,
                points_per_unit, unit_scale, max_offset_pct, min_amount, version,
                effective_from, effective_to, status, proposed_by)
            VALUES (:rule_code, :biz_line, :region_id, :currency, :points_per_unit,
                    :unit_scale, :max_offset_pct, :min_amount, :version, :effective_from,
                    :effective_to, :status, :proposed_by)
            RETURNING {RULE_COLS}
        """), {"rule_code": rule_code, "biz_line": biz_line, "region_id": region_id,
               "currency": currency, "points_per_unit": points_per_unit,
               "unit_scale": unit_scale, "max_offset_pct": max_offset_pct,
               "min_amount": min_amount, "version": version, "effective_from": effective_from,
               "effective_to": effective_to, "status": status, "proposed_by": proposed_by,
               })).mappings().one()
        await session.flush()
        return dict(row)

    async def set_offset_rule_status(self, session: AsyncSession, *, rule_id: int, status: str,
                                     approver: int) -> dict[str, Any] | None:
        row = (await session.execute(text(
            "UPDATE mkt.points_offset_rule SET status = :s, approved_by = :by "
            "WHERE id = :id RETURNING " + RULE_COLS
        ), {"s": status, "by": approver, "id": rule_id})).mappings().one_or_none()
        await session.flush()
        return dict(row) if row else None

    # ══ 平台补贴（§10-14：抵扣不影响收款方应收，差额平台补）══════════════════
    async def insert_subsidy(self, session: AsyncSession, *, order_ref_type: str,
                             order_ref_id: int, reason: str, points_kind: str | None,
                             amount: int, currency: str, beneficiary_type: str | None,
                             beneficiary_id: int | None) -> dict[str, Any]:
        row = (await session.execute(text("""
            INSERT INTO fin.subsidy_ledger (order_ref_type, order_ref_id, reason, points_kind,
                amount, currency, beneficiary_type, beneficiary_id)
            VALUES (:order_ref_type, :order_ref_id, :reason, :points_kind,
                    :amount, :currency, :beneficiary_type, :beneficiary_id)
            RETURNING id, order_ref_type, order_ref_id, reason, points_kind, amount, currency,
                      beneficiary_type, beneficiary_id, created_at
        """), {"order_ref_type": order_ref_type, "order_ref_id": order_ref_id,
               "reason": reason, "points_kind": points_kind, "amount": amount,
               "currency": currency, "beneficiary_type": beneficiary_type,
               "beneficiary_id": beneficiary_id})).mappings().one()
        await session.flush()
        return dict(row)

    async def subsidies_of(self, session: AsyncSession, *, order_ref_type: str,
                           order_ref_id: int) -> list[dict[str, Any]]:
        rows = (await session.execute(text("""
            SELECT id, reason, points_kind, amount, currency, beneficiary_type, beneficiary_id
              FROM fin.subsidy_ledger
             WHERE order_ref_type = :t AND order_ref_id = :id ORDER BY id
        """), {"t": order_ref_type, "id": order_ref_id})).mappings().all()
        return [dict(r) for r in rows]
