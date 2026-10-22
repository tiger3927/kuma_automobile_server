"""告警写入与查询（(vin,ts,type) 唯一 → 重复上报天然幂等）。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import Select, literal, select, text, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert

from ..core.pagination import PageParams
from ..core.scope import Principal
from ..models.biz import Alarm, AlarmAck, AlarmRule
from ..models.core import Vehicle
from .base import vehicle_scope

# 未了结状态：抑制窗口只看这些。closed/auto_closed 是终态，不影响新告警。
OPENISH = ("open", "ack", "silenced")


class AlarmRepo:
    async def raise_alarm(self, session, *, vin: str, ts: datetime, type_: str,
                          level: int = 2, payload: dict | None = None,
                          dedupe_window_s: int = 600) -> int | None:
        """ON CONFLICT DO NOTHING + **同类抑制**：返回新建告警的 id（被抑制/冲突时 None）。

        返回 id 而不是 bool，是为了让调用方能在**同一事务**里写 outbox 事件
        （`services/alarm_service.raise_alarm` 是唯一入口，见 C1）。

        只靠 `UNIQUE(vin,ts,type)` 挡不住"同一辆车 5 分钟内超速 30 次"——每条 `ts` 不同，
        会刷出 30 条告警把运营淹没，也把 WS 推送刷爆。抑制窗口来自
        `biz.alarm_rule.dedupe_window_s`（2026-10-22 起，此前没有任何抑制）。
        """
        # 必须是 `INSERT ... SELECT ... WHERE NOT EXISTS`：PG 的 `INSERT ... VALUES` 
        # **不支持 WHERE**（mypy 直接报 `"Insert" has no attribute "where"`），
        # 而"先 SELECT 再 INSERT"会在并发下多写一条。from_select 一条语句搞定抑制。
        cols = ["vin", "ts", "type", "level", "payload", "status"]
        sel = select(
            literal(vin).label("vin"),
            literal(ts).label("ts"),
            literal(type_).label("type"),
            literal(level).label("level"),
            literal(payload or {}, JSONB).label("payload"),
            literal("open").label("status"),
        )
        if dedupe_window_s > 0:
            blocked = select(Alarm.id).where(
                Alarm.vin == vin,
                Alarm.type == type_,
                Alarm.status.in_(OPENISH),
                Alarm.ts >= ts - timedelta(seconds=dedupe_window_s),
                Alarm.ts <= ts,
            ).exists()
            sel = sel.where(~blocked)
        stmt = pg_insert(Alarm).from_select(cols, sel)
        row = (await session.execute(
            stmt.on_conflict_do_nothing(index_elements=["vin", "ts", "type"]).returning(Alarm.id)
        )).first()
        return int(row[0]) if row else None

    async def rule(self, session, type_: str, region_code: str | None = None) -> AlarmRule | None:
        """取阈值规则：地区覆盖优先，其次全局默认（`region_code IS NULL`）。"""
        if region_code:
            local = (await session.execute(
                select(AlarmRule).where(AlarmRule.type == type_, AlarmRule.region_code == region_code)
            )).scalar_one_or_none()
            if local is not None:
                return local
        return (await session.execute(
            select(AlarmRule).where(AlarmRule.type == type_, AlarmRule.region_code.is_(None))
        )).scalar_one_or_none()

    async def set_status(self, session, *, alarm_id: int, to_status: str,
                         snooze_until: datetime | None = None) -> Alarm | None:
        """状态落库（合法性由 service 层的迁移表判定，这里只写）。"""
        alarm = await session.get(Alarm, alarm_id)
        if alarm is None:
            return None
        alarm.status = to_status
        alarm.snooze_until = snooze_until
        alarm.updated_at = datetime.now(UTC)
        await session.flush()
        return alarm

    async def expired_snoozes(self, session, *, limit: int = 500) -> list[Alarm]:
        """静默到期、该回到 open 的告警（worker 的 `alarm_snooze` 任务用）。"""
        return (await session.execute(
            select(Alarm).where(Alarm.status == "silenced", Alarm.snooze_until < datetime.now(UTC))
            .limit(limit)
        )).scalars().all()

    async def auto_close(self, session, *, vins: list[str], type_: str) -> list[tuple[int, str]]:
        """条件自然消失时自动了结（目前用于 offline 告警：车恢复上报就关）。

        只碰 `open`/`ack`——已被人工静默或关闭的不覆盖，人的判断优先于自动逻辑。
        返回 `(alarm_id, vin)` 而不是条数：调用方要为每条告警写 outbox 事件（C1）。
        """
        if not vins:
            return []
        rows = (await session.execute(
            update(Alarm)
            .where(Alarm.vin.in_(vins), Alarm.type == type_, Alarm.status.in_(("open", "ack")))
            .values(status="auto_closed", snooze_until=None, updated_at=datetime.now(UTC))
            .returning(Alarm.id, Alarm.vin)
        )).all()
        return [(int(r[0]), str(r[1])) for r in rows]

    def page(self, principal: Principal, *, vin: str | None = None,   # 见 vehicle_repo：方法别叫 list
             status: str | None = None, type_: str | None = None,
             page: PageParams | None = None) -> Select:
        stmt = select(Alarm).where(Alarm.vin.in_(
            select(Vehicle.vin).where(Vehicle.id.in_(vehicle_scope(principal)))
        ))
        if vin:
            stmt = stmt.where(Alarm.vin == vin)
        if status:
            stmt = stmt.where(Alarm.status == status)
        if type_:
            stmt = stmt.where(Alarm.type == type_)
        p = page or PageParams()
        return stmt.order_by(Alarm.ts.desc()).limit(p.limit).offset(p.offset)

    async def ack(self, session, *, alarm_id: int, account_id: int, note: str | None) -> Alarm | None:
        alarm = await session.get(Alarm, alarm_id)
        if alarm is None or alarm.status != "open":
            return None
        alarm.status = "ack"
        session.add(AlarmAck(alarm_id=alarm_id, ack_by=account_id, ack_at=datetime.now(UTC), note=note))
        await session.flush()
        return alarm

    async def open_counts_by_vin(self, session, principal: Principal) -> list[dict]:
        stmt: Select = (
            select(Alarm.vin, Alarm.type, text("count(*)").label("n"))
            .where(Alarm.status == "open", Alarm.vin.in_(
                select(Vehicle.vin).where(Vehicle.id.in_(vehicle_scope(principal)))))
            .group_by(Alarm.vin, Alarm.type)
            .order_by(text("n DESC")).limit(100)
        )
        return [dict(vin=r.vin, type=r.type, n=r.n) for r in (await session.execute(stmt)).all()]
