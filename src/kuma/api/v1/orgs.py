"""组织、成员、主管理员转移。"""

from __future__ import annotations

from fastapi import APIRouter, Request

from ...core.deps import Db, PrincipalDep, client_ip
from ...schemas.auth import MemberIn, OrgCreateIn, PrimaryAdminTransferIn
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


@router.get("/{org_id}/members")
async def members(org_id: int, session: Db, principal: PrincipalDep) -> dict:
    principal.require("org", "read")
    rows = await svc.accounts.memberships(session, principal.account_id)
    return {"items": [{"org_id": o.id, "role": m.role} for m, o in rows if o.id == org_id]}
