"""公开目录（portal 官网）：车型参数页 + 经销商查询。无鉴权、只读。

三条硬约束：
1. **字段白名单在本文件定死**——`protocol_profile`（车端协议档）、`regions`（投放配置）、`status`
   以及 org 的任何账号关联（`primary_account_id`）一律不出公网；新增字段默认不公开。
2. **限流在中间件**（`api/app.py` 的 `/api/v1/public/` = 120/min/IP），这里不重复实现。
3. **缓存 60s 且必须带 TTL**（复用别人的 redis 容器，`core/redis_client.set_ttl` 是唯一写入口）；
   redis 不可用时直接回源——公开端点宁可多打一次 PG，也不能 500。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Coroutine
from contextlib import suppress
from decimal import Decimal
from typing import Any

from ..core.exceptions import NotFound
from ..core.pagination import PageParams
from ..core.redis_client import get_redis, kkey, set_ttl
from ..models.core import Org, VehicleType
from ..repositories.account_repo import AccountRepo
from ..repositories.vehicle_repo import VehicleRepo

CACHE_TTL_S = 60

# 厂家愿意挂在官网上的产品参数（Task-Plan.md §9.1 买车页要展示的就是这些）
PUBLIC_TYPE_FIELDS: tuple[str, ...] = (
    "type_code", "name", "product_line", "category", "seats", "passenger_seats", "doors",
    "load_capacity_kg", "cargo_len_mm", "cargo_wid_mm", "cargo_hgt_mm",
    "length_mm", "width_mm", "height_mm", "wheelbase_mm", "min_ground_clearance_mm",
    "curb_weight_kg", "gvwr_kg", "power_type", "motor_power_kw", "peak_power_kw",
    "motor_count", "battery_kwh", "battery_type", "range_km", "range_condition",
    "top_speed_kmh", "climbing_grade_pct", "charge_fast_h", "charge_slow_h",
    "drive_type", "fuel_l_100km", "specs", "features",
)


def _jsonable(value: Any) -> Any:
    """Numeric 列出来是 Decimal，统一转 float，免得响应序列化各端不一致。"""
    return float(value) if isinstance(value, Decimal) else value


def type_public(row: VehicleType) -> dict[str, Any]:
    return {field: _jsonable(getattr(row, field)) for field in PUBLIC_TYPE_FIELDS}


def dealer_public(row: Org) -> dict[str, Any]:
    """门店公开信息。地址 / 电话 / 坐标是**迁移 0010 新加的列**（§11-7：之前无列可出，
    官网只能显示名称 + 国家，也不能在这里拼假数据）。

    **坐标只在两者都有时才出**：只出一个 `lat` 会让前端把这家店画到 (lat, 0)，
    比不画更糟——用户会以为赤道线上真有个门店。
    """
    out: dict[str, Any] = {
        "code": row.code, "name": row.name, "org_type": row.org_type,
        "region_id": row.region_id, "level": row.level,
        "address": row.address, "phone": row.phone,
    }
    if row.lat is not None and row.lng is not None:
        out["lat"] = row.lat
        out["lng"] = row.lng
    return out


async def _cached(key: str, load: Callable[[], Coroutine[Any, Any, dict[str, Any]]]) -> dict[str, Any]:
    cache_key = kkey("public", key)
    with suppress(Exception):
        hit = await get_redis().get(cache_key)
        if hit:
            return dict(json.loads(hit))
    data = await load()
    with suppress(Exception):
        await set_ttl(cache_key, json.dumps(data, default=str), CACHE_TTL_S)
    return data


class PublicService:
    vehicles = VehicleRepo()
    accounts = AccountRepo()

    async def types(self, session, *, region_id: str | None = None,
                    product_line: str | None = None, category: str | None = None,
                    q: str | None = None, page: PageParams | None = None) -> dict[str, Any]:
        p = page or PageParams()
        key = f"types:{region_id}:{product_line}:{category}:{q}:{p.limit}:{p.offset}"

        async def load() -> dict[str, Any]:
            rows, total = await self.vehicles.types_page(
                session, region_id=region_id, product_line=product_line,
                category=category, q=q, page=p)
            return {"items": [type_public(t) for t in rows],
                    "limit": p.limit, "offset": p.offset, "total": total}

        return await _cached(key, load)

    async def type_detail(self, session, type_code: str) -> dict[str, Any]:
        key = f"type:{type_code}"

        async def load() -> dict[str, Any]:
            row = await self.vehicles.type_by_code(session, type_code)
            if row is None:
                raise NotFound("vehicle_type_not_found")     # 不缓存 404：抛错前不会写缓存
            return type_public(row)

        return await _cached(key, load)

    async def dealers(self, session, *, region_id: str | None = None, q: str | None = None,
                      page: PageParams | None = None) -> dict[str, Any]:
        p = page or PageParams()
        key = f"dealers:{region_id}:{q}:{p.limit}:{p.offset}"

        async def load() -> dict[str, Any]:
            rows, total = await self.accounts.orgs_page(
                session, org_type="dealer", region_id=region_id, q=q, page=p)
            return {"items": [dealer_public(o) for o in rows],
                    "limit": p.limit, "offset": p.offset, "total": total}

        return await _cached(key, load)
