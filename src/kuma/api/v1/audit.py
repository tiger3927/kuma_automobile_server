"""审计查询接口（C7）：`R_AUDIT` 只读，查询本身也留痕。

`docs/permissions.md` 的矩阵里只有 `platform_staff` 与 `regulator` 有 `audit:read`。
权限判定放在 service（`principal.require`），这里只做参数装配——api 层不写判断逻辑。
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Request

from ...core.deps import Db, PrincipalDep, client_ip
from ...core.pagination import PageParams
from ...services.audit_service import AuditService

router = APIRouter(prefix="/audit", tags=["audit"])
svc = AuditService()


@router.get("")
async def list_audit(request: Request, session: Db, principal: PrincipalDep,
                     actor_account_id: int | None = None, action: str | None = None,
                     resource_type: str | None = None, resource_id: str | None = None,
                     since: datetime | None = None, until: datetime | None = None,
                     limit: int = 50, offset: int = 0) -> dict:
    return await svc.page(session, principal, actor_account_id=actor_account_id, action=action,
                          resource_type=resource_type, resource_id=resource_id,
                          since=since, until=until,
                          page=PageParams(limit=limit, offset=offset), ip=client_ip(request))


@router.get("/actions")
async def list_actions(session: Db, principal: PrincipalDep,
                       since: datetime | None = None, until: datetime | None = None) -> dict:
    """有哪些 action 可过滤（前端下拉数据源，不把名单写死在前端）。"""
    return await svc.actions(session, principal, since=since, until=until)
