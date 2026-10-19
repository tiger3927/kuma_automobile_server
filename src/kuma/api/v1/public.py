"""公开只读端点（portal 官网：车型参数页、经销商查询）。

**无鉴权**——安全边界靠三层：中间件限流（`api/app.py` 的 `/api/v1/public/` = 120/min/IP）、
本层不接收任何"取回内部字段"的参数、字段白名单在 `services/public_service.py`。
不要在这里加 PrincipalDep：官网在登录前就要能浏览。
"""

from __future__ import annotations

from fastapi import APIRouter, Query

from ...core.deps import Db
from ...core.pagination import PageParams
from ...services.public_service import PublicService

router = APIRouter(prefix="/public", tags=["public"])
svc = PublicService()


@router.get("/vehicle-types")
async def vehicle_types(session: Db, region_id: str | None = Query(default=None, max_length=2),
                        product_line: str | None = None, category: str | None = None,
                        q: str | None = Query(default=None, max_length=64),
                        limit: int = 20, offset: int = 0) -> dict:
    return await svc.types(session, region_id=region_id, product_line=product_line,
                           category=category, q=q, page=PageParams(limit=limit, offset=offset))


@router.get("/vehicle-types/{type_code}")
async def vehicle_type_detail(type_code: str, session: Db) -> dict:
    return await svc.type_detail(session, type_code)


@router.get("/dealers")
async def dealers(session: Db, region_id: str | None = Query(default=None, max_length=2),
                  q: str | None = Query(default=None, max_length=64),
                  limit: int = 20, offset: int = 0) -> dict:
    return await svc.dealers(session, region_id=region_id, q=q,
                             page=PageParams(limit=limit, offset=offset))
