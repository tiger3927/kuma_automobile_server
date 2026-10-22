"""围栏接口（C4）：`geo.fence` 此前只有读、没有写。

围栏决定两件事：派单硬过滤（§8.4 运营商获准经营区）与 `fence_exit` 告警。
没有写入端点时，这两条只能靠 seed/SQL 配置——运营改不了，出问题也修不了。
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from ...core.deps import Db, PrincipalDep, client_ip
from ...schemas.ops import FenceIn, FencePatch
from ...services.fence_service import FenceService

router = APIRouter(prefix="/fences", tags=["fences"])
svc = FenceService()


@router.get("")
async def list_fences(session: Db, principal: PrincipalDep, org_id: int | None = None,
                      region_id: str | None = None, fence_type: str | None = None,
                      status: str | None = None, limit: int = 50, offset: int = 0) -> dict:
    return await svc.page(principal, org_id=org_id, region_id=region_id, fence_type=fence_type,
                          status=status, limit=limit, offset=offset)


@router.post("")
async def create_fence(body: FenceIn, request: Request, session: Db,
                       principal: PrincipalDep) -> dict:
    return await svc.create(session, principal, body, ip=client_ip(request))


@router.get("/{fence_id}")
async def fence_detail(fence_id: int, session: Db, principal: PrincipalDep) -> dict:
    return await svc.detail(principal, fence_id)


@router.patch("/{fence_id}")
async def patch_fence(fence_id: int, body: FencePatch, request: Request, session: Db,
                      principal: PrincipalDep) -> dict:
    return await svc.patch(session, principal, fence_id, body, ip=client_ip(request))


@router.post("/{fence_id}/deactivate")
async def deactivate_fence(fence_id: int, request: Request, session: Db,
                           principal: PrincipalDep) -> dict:
    """停用（不是删除）：围栏参与过派单与告警判定，物理删除会让历史无法复现。"""
    return await svc.deactivate(session, principal, fence_id, ip=client_ip(request))
