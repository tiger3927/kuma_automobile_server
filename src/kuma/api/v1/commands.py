"""下行控制（M7 验收：离线拒发、行车禁断电、重试与超时、审计不可篡改）。"""

from __future__ import annotations

from fastapi import APIRouter, Request

from ...core.deps import Db, IdempotencyDep, PrincipalDep, client_ip
from ...core.pagination import PageParams
from ...schemas.ops import CommandIn, ConfirmIn
from ...services.command_service import CommandService, issue_confirm

router = APIRouter(prefix="/commands", tags=["command"])
svc = CommandService()


@router.post("/confirm", status_code=201)
async def confirm(body: ConfirmIn, principal: PrincipalDep) -> dict:
    """高危指令的二次确认令牌（一次性，5 分钟）。"""
    principal.require("command_down", "write")
    token = await issue_confirm(body.vin, body.cmd_type)
    return {"confirm_token": token, "expires_in": 300}


@router.post("", status_code=202)
async def issue(body: CommandIn, request: Request, session: Db, principal: PrincipalDep,
                _idem: IdempotencyDep) -> dict:
    return await svc.issue(session, principal, vin=body.vin, cmd_type=body.cmd_type,
                           payload=body.payload, confirm_token=body.confirm_token,
                           co_approver_account_id=body.co_approver_account_id,
                           ip=client_ip(request))


@router.get("")
async def list_commands(session: Db, principal: PrincipalDep, vin: str | None = None,
                        status: str | None = None, limit: int = 20, offset: int = 0) -> dict:
    return await svc.page(session, principal, vin=vin, status=status,
                          page=PageParams(limit=limit, offset=offset))


@router.get("/{cmd_id}/audit")
async def audit_trail(cmd_id: str, session: Db, principal: PrincipalDep) -> dict:
    return await svc.trail(session, principal, cmd_id)
