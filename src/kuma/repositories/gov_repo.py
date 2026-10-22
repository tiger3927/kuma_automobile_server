"""监管侧读取（C8，2026-10-22）。

按 §3.2 的规矩，跨 schema 的展示类读取一律走 `rep` 视图（视图定义在迁移 0006），
这里只负责过滤与分页——**不做任何 join**，join 逻辑在视图里，可被 DBA 单独审计。

所有方法都强制 `limit` 上限：监管查询是只读扫描，512MB 的 PG 上不能让人拖着全表跑。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import text

from ..core.db import get_engine

MAX_LIMIT = 1000


def _clamp(limit: int) -> int:
    return max(1, min(limit, MAX_LIMIT))


class GovRepo:
    async def trips(self, *, region_id: str | None = None, vin: str | None = None,
                    since: datetime | None = None, until: datetime | None = None,
                    limit: int = 100, offset: int = 0) -> list[dict]:
        """行程（odo 口径，`rep.gov_trip_view` 里已 join 好 `method='odo'` 的里程）。"""
        sql = "SELECT * FROM rep.gov_trip_view WHERE true"
        params: dict = {"limit": _clamp(limit), "offset": max(0, offset)}
        if region_id:
            sql += " AND region_id = :region_id"
            params["region_id"] = region_id
        if vin:
            sql += " AND vin = :vin"
            params["vin"] = vin
        if since:
            sql += " AND start_ts >= :since"
            params["since"] = since
        if until:
            sql += " AND start_ts < :until"
            params["until"] = until
        sql += " ORDER BY start_ts DESC LIMIT :limit OFFSET :offset"
        return await _rows(sql, params)

    async def vehicles(self, *, region_id: str | None = None, status: str | None = None,
                       vin: str | None = None, sub_status: str | None = None,
                       limit: int = 50, offset: int = 0) -> list[dict]:
        sql = "SELECT * FROM rep.gov_vehicle_view WHERE true"
        params: dict = {"limit": _clamp(limit), "offset": max(0, offset)}
        if region_id:
            sql += " AND region_id = :region_id"
            params["region_id"] = region_id
        if status:
            sql += " AND vehicle_status = :status"
            params["status"] = status
        if vin:
            sql += " AND vin = :vin"
            params["vin"] = vin
        if sub_status:
            sql += " AND sub_status = :sub_status"
            params["sub_status"] = sub_status
        sql += " ORDER BY vin LIMIT :limit OFFSET :offset"
        return await _rows(sql, params)

    async def alarms(self, *, region_id: str | None = None, type_: str | None = None,
                     status: str | None = None, since: datetime | None = None,
                     until: datetime | None = None, limit: int = 100,
                     offset: int = 0) -> list[dict]:
        sql = "SELECT * FROM rep.gov_alarm_view WHERE true"
        params: dict = {"limit": _clamp(limit), "offset": max(0, offset)}
        if region_id:
            sql += " AND region_id = :region_id"
            params["region_id"] = region_id
        if type_:
            sql += " AND type = :type"
            params["type"] = type_
        if status:
            sql += " AND status = :status"
            params["status"] = status
        if since:
            sql += " AND ts >= :since"
            params["since"] = since
        if until:
            sql += " AND ts < :until"
            params["until"] = until
        sql += " ORDER BY ts DESC LIMIT :limit OFFSET :offset"
        return await _rows(sql, params)

    async def commands(self, *, region_id: str | None = None, status: str | None = None,
                       vin: str | None = None, since: datetime | None = None,
                       until: datetime | None = None, limit: int = 100,
                       offset: int = 0) -> list[dict]:
        sql = "SELECT * FROM rep.gov_command_view WHERE true"
        params: dict = {"limit": _clamp(limit), "offset": max(0, offset)}
        if region_id:
            sql += " AND region_id = :region_id"
            params["region_id"] = region_id
        if status:
            sql += " AND status = :status"
            params["status"] = status
        if vin:
            sql += " AND vin = :vin"
            params["vin"] = vin
        if since:
            sql += " AND created_at >= :since"
            params["since"] = since
        if until:
            sql += " AND created_at < :until"
            params["until"] = until
        sql += " ORDER BY created_at DESC LIMIT :limit OFFSET :offset"
        return await _rows(sql, params)


async def _rows(sql: str, params: dict) -> list[dict]:
    engine = get_engine()
    async with engine.connect() as conn:
        rows = (await conn.execute(text(sql), params)).mappings().all()
    return [dict(r) for r in rows]
