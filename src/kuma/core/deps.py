"""FastAPI 依赖：会话、身份解析、权限闸门、幂等键、客户端 IP。

铁律（Task-Server.md §7）：api 层不写 SQL；这里只做"取身份"，业务判断在 services。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated, cast

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.core import Account, IdempotencyKey, Identity, Membership, Org, OrgBusiness
from .db import session_scope
from .exceptions import Conflict, Forbidden, Unauthorized
from .scope import IdentityType, OrgRef, OrgType, Principal
from .security import decode_access_token

_bearer = HTTPBearer(auto_error=False)


async def get_db() -> AsyncIterator[AsyncSession]:
    async for session in session_scope():
        yield session


Db = Annotated[AsyncSession, Depends(get_db)]


async def resolve_principal(session: AsyncSession, token: str) -> Principal:
    """每次请求重新解析 scope（不信任前端 org_id）。"""
    payload = decode_access_token(token)
    account_id = int(payload["sub"])

    account = await session.get(Account, account_id)
    if account is None or account.status != "active":
        raise Unauthorized("account_inactive")

    ids = (await session.execute(
        select(Identity).where(Identity.account_id == account_id, Identity.status == "active")
    )).scalars().all()
    if not ids:
        raise Unauthorized("no_identity")

    active_id = next((i for i in ids if i.id == payload.get("identity_id")), None)
    if active_id is None:
        # token 里的身份已失效：允许降级到该账号任一有效身份，但必须重新登录才换身份
        raise Forbidden("identity_revoked")

    rows = (await session.execute(
        select(Membership, Org)
        .join(Org, Org.id == Membership.org_id)
        .where(Membership.account_id == account_id, Membership.status == "active",
               Org.status == "active")
    )).all()

    # M15：业务线（core.org_business）——多业态 scope 的判定依据（换电按业务线，不按 org_type）。
    # 一次查完按 org 分组；没有 active 行就是没有该业务线，不做隐式默认。
    # ⚠ 依赖迁移 0007：core.org_business 不存在时这里会报错——**先跑迁移再部署代码**。
    lines: dict[int, list[str]] = {}
    org_ids = [o.id for _, o in rows]
    if org_ids:
        for org_id, line in (await session.execute(
            select(OrgBusiness.org_id, OrgBusiness.line)
            .where(OrgBusiness.org_id.in_(org_ids), OrgBusiness.status == "active")
        )).all():
            lines.setdefault(org_id, []).append(line)

    # 列在 DB 里是 text，取值域由 CHECK 约束保证（Task-DataBase.md §11）；
    # 这里 cast 成 Literal 只是把已保证的事实告诉类型系统，不是绕过校验
    return Principal(
        account_id=account_id,
        identity_id=active_id.id,
        active_identity=cast(IdentityType, active_id.identity_type),
        identities=sorted({i.identity_type for i in ids}),
        orgs=[OrgRef(org_id=o.id, org_type=cast(OrgType, o.org_type), role=m.role,
                     level=o.level, path=o.path,
                     business_lines=sorted(lines.get(o.id, [])))
              for m, o in rows],
        kyc_level=account.kyc_level,
        status=account.status,
    )


async def current_principal(
    request: Request,
    session: Db,
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)] = None,
) -> Principal:
    if creds is None:
        raise Unauthorized("missing_token")
    principal = await resolve_principal(session, creds.credentials)
    request.state.principal = principal
    return principal


PrincipalDep = Annotated[Principal, Depends(current_principal)]


def require(resource: str, action: str = "read"):
    """路由级权限闸门；数据范围由 repository 的 scope 过滤器兜住。"""

    async def _dep(principal: PrincipalDep) -> Principal:
        principal.require(resource, action)
        return principal

    return Depends(_dep)


def client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else ""


def _idempotency_header(request: Request) -> str | None:
    return request.headers.get("idempotency-key")


async def idempotent(
    request: Request,
    session: Db,
    key: Annotated[str | None, Depends(_idempotency_header)] = None,
) -> str | None:
    """写接口幂等（Task-Plan.md §3.2）：同一 Idempotency-Key 只允许处理一次。"""
    if not key:
        return None
    account = getattr(request.state, "principal", None)
    # RETURNING 而不是 rowcount：Result 上没有 rowcount，且"有没有插进去"本身就该用返回值判断
    row = (await session.execute(
        pg_insert(IdempotencyKey)
        .values(key=key, account_id=account.account_id if account else None)
        .on_conflict_do_nothing(index_elements=["key"])
        .returning(IdempotencyKey.key)
    )).first()
    if row is None:
        raise Conflict("请求重复提交", code="duplicate_request",
                       detail={"idempotency_key": key})
    return key


IdempotencyDep = Annotated[str | None, Depends(idempotent)]
