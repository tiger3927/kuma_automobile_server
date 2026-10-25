"""组织、成员、主管理员转移。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from ...core.deps import Db, PrincipalDep, client_ip
from ...schemas.auth import MemberIn, OrgContactIn, OrgCreateIn, PrimaryAdminTransferIn
from ...services.org_service import OrgService

router = APIRouter(prefix="/orgs", tags=["org"])
svc = OrgService()


@router.post("", status_code=201)
async def create_org(body: OrgCreateIn, request: Request, session: Db,
                     principal: PrincipalDep) -> dict:
    org = await svc.create(session, principal, **body.model_dump(), ip=client_ip(request))
    return {"id": org.id, "code": org.code, "name": org.name, "org_type": org.org_type,
            "level": org.level, "path": org.path}


@router.get("")
async def list_orgs(session: Db, principal: PrincipalDep,
                    root_id: int | None = None) -> dict:
    return {"items": await svc.tree(session, principal, root_id=root_id)}


@router.post("/{org_id}/members")
async def add_member(org_id: int, body: MemberIn, request: Request, session: Db,
                     principal: PrincipalDep) -> dict:
    return await svc.add_member(session, principal, org_id, body.account_id, body.role,
                                ip=client_ip(request))


@router.post("/{org_id}/primary-admin")
async def transfer_primary(org_id: int, body: PrimaryAdminTransferIn, request: Request,
                           session: Db, principal: PrincipalDep) -> dict:
    return await svc.transfer_primary_admin(
        session, principal, org_id, body.new_account_id, body.co_approver_account_id,
        ip=client_ip(request))


@router.patch("/{org_id}")
async def update_org(org_id: int, body: OrgContactIn, request: Request, session: Db,
                     principal: PrincipalDep) -> dict[str, Any]:
    """改组织名称与联系方式（迁移 0010 加的 address/phone/lat/lng；portal 门店页要用）。"""
    return await svc.update_contact(session, principal, org_id, name=body.name,
                                    address=body.address, phone=body.phone,
                                    lat=body.lat, lng=body.lng, ip=client_ip(request))


@router.get("/{org_id}/members")
async def members(org_id: int, session: Db, principal: PrincipalDep) -> dict:
    """该组织在职成员 + 每人有效身份（**双人授权选联审人的唯一来源**，`Task-Web.md` §5-6）。

    旧实现查的是"调用者自己的身份按 org_id 过滤"——主管理员看不见自己组织的成员名单，
    前端只能盲填 account_id 再吃 `co_approver_not_platform_staff`。
    响应仍是 `{"items": [...]}` 信封，只是条目变成真实成员。
    """
    return {"items": await svc.members(session, principal, org_id)}
