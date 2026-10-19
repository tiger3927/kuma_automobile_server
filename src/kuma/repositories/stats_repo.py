"""报表读取：跨域展示一律走 `rep` 视图（Task-Plan.md §3.2），业务代码不散落跨 schema join。

`kuma_ro` 只能读 `rep`；应用侧用 `kuma_app` 读同一批视图，导出类任务将来切 `kuma_ro` DSN。
"""

from __future__ import annotations

from sqlalchemy import text

from ..core.db import get_engine


class StatsRepo:
    async def gov_trips(self, *, region_id: str | None = None, limit: int = 500) -> list[dict]:
        engine = get_engine()
        sql = "SELECT * FROM rep.gov_trip_view"
        params: dict = {"limit": min(limit, 5000)}
        if region_id:
            sql += " WHERE region_id = :region_id"
            params["region_id"] = region_id
        sql += " ORDER BY start_ts DESC LIMIT :limit"
        async with engine.connect() as conn:
            rows = (await conn.execute(text(sql), params)).mappings().all()
        return [dict(r) for r in rows]

    async def sales_by_region(self) -> list[dict]:
        engine = get_engine()
        async with engine.connect() as conn:
            rows = (await conn.execute(text("SELECT * FROM rep.sales_region_view"))).mappings().all()
        return [dict(r) for r in rows]

    async def telemetry_hourly(self, vin: str | None = None, limit: int = 500) -> list[dict]:
        engine = get_engine()
        sql = "SELECT * FROM rep.telemetry_hourly"
        params: dict = {"limit": min(limit, 5000)}
        if vin:
            sql += " WHERE vin = :vin"
            params["vin"] = vin
        sql += " ORDER BY hour DESC LIMIT :limit"
        async with engine.connect() as conn:
            rows = (await conn.execute(text(sql), params)).mappings().all()
        return [dict(r) for r in rows]
