"""认证与身份路由（M1 验收：登录/刷新/切身份/越权 403）。"""

from __future__ import annotations

from fastapi import APIRouter, Request

from ...core.deps import Db, IdempotencyDep, PrincipalDep, client_ip
from ...schemas.auth import (
    LoginIn,
    RefreshIn,
    RegisterIn,
    SwitchIdentityIn,
)
from ...services.auth_service import AuthService

router = APIRouter(prefix="/auth", tags=["auth"])
svc = AuthService()


@router.post("/register", status_code=201)
async def register(body: RegisterIn, request: Request, session: Db, _idem: IdempotencyDep) -> dict:
    account = await svc.register(session, **body.model_dump(), ip=client_ip(request))
    return {"account_id": account.id, "username": account.username, "identities": ["consumer"]}


@router.post("/login")
async def login(body: LoginIn, request: Request, session: Db) -> dict:
    return await svc.login(session, username=body.username, password=body.password,
                           identity_type=body.identity_type, ip=client_ip(request))


@router.post("/refresh")
async def refresh(body: RefreshIn, session: Db) -> dict:
    return await svc.refresh(session, refresh_token=body.refresh_token)


@router.post("/switch-identity")
async def switch_identity(body: SwitchIdentityIn, request: Request, session: Db,
                          principal: PrincipalDep) -> dict:
    return await svc.switch_identity(session, principal=principal,
                                     identity_type=body.identity_type, ip=client_ip(request))


@router.get("/me")
async def me(session: Db, principal: PrincipalDep) -> dict:
    return await svc.whoami(session, principal)
