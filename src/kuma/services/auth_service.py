"""认证与身份（services 层禁止 Depends/Request，保证 gateway/worker 可复用）。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import cast

from sqlalchemy.ext.asyncio import AsyncSession

from ..core.audit import write_audit
from ..core.exceptions import Conflict, Forbidden, Unauthorized, ValidationFailed
from ..core.scope import IdentityType, Principal
from ..core.security import (
    ahash_password,
    averify_password,
    create_access_token,
    create_refresh_token,
    hash_token,
)
from ..models.core import Account, UserProfile
from ..repositories.account_repo import AccountRepo

# 注册只允许自助获得 consumer；其余身份必须由审核/组织流程授予（Task-Plan.md §2.1）
SELF_SERVICE_IDENTITY = "consumer"


class AuthService:
    repo = AccountRepo()

    async def register(self, session: AsyncSession, *, username: str, password: str,
                       phone: str | None = None, email: str | None = None,
                       region_id: str | None = None, display_name: str | None = None,
                       ip: str | None = None) -> Account:
        if len(password) < 8:
            raise ValidationFailed("口令至少 8 位")
        if await self.repo.by_username(session, username):
            raise Conflict("username_taken")
        if phone and await self.repo.by_phone(session, phone):
            raise Conflict("phone_taken")

        account = Account(username=username, phone=phone, email=email,
                          password_hash=await ahash_password(password), region_id=region_id,
                          status="active", kyc_level=0)
        session.add(account)
        await session.flush()
        if display_name:
            session.add(UserProfile(account_id=account.id, display_name=display_name))
        await self.repo.add_identity(session, account.id, SELF_SERVICE_IDENTITY)
        await write_audit(session, actor=None, action="account.register",
                          resource_type="account", resource_id=account.id,
                          after={"username": username, "region_id": region_id}, ip=ip)
        return account

    async def login(self, session: AsyncSession, *, username: str, password: str,
                    identity_type: str | None = None, ip: str | None = None) -> dict:
        account = await self.repo.by_username(session, username)
        if account is None or not await averify_password(password, account.password_hash):
            raise Unauthorized("bad_credentials")
        if account.status != "active":
            raise Forbidden("account_inactive")

        identities = await self.repo.identities(session, account.id)
        if not identities:
            raise Forbidden("no_identity")
        chosen = next((i for i in identities if i.identity_type == identity_type), identities[0])

        access, ttl = create_access_token(
            account_id=account.id, identity_id=chosen.id, active_identity=chosen.identity_type)
        refresh_raw, refresh_exp = create_refresh_token()
        await self.repo.save_refresh_token(session, account.id, hash_token(refresh_raw), refresh_exp)

        account.last_login_at = datetime.now(UTC)
        await write_audit(session,
                          actor=Principal(account_id=account.id, identity_id=chosen.id,
                                          active_identity=cast(IdentityType, chosen.identity_type),
                                          identities=[i.identity_type for i in identities]),
                          action="auth.login", resource_type="account", resource_id=account.id, ip=ip)
        return {
            "access_token": access, "expires_in": ttl, "refresh_token": refresh_raw,
            "active_identity": chosen.identity_type, "identity_id": chosen.id,
            "identities": [i.identity_type for i in identities],
        }

    async def refresh(self, session: AsyncSession, *, refresh_token: str) -> dict:
        rt = await self.repo.find_refresh_token(session, hash_token(refresh_token))
        if rt is None or rt.revoked_at is not None or rt.expires_at < datetime.now(UTC):
            raise Unauthorized("refresh_invalid")
        account = await self.repo.get(session, rt.account_id)
        if account is None or account.status != "active":
            raise Unauthorized("account_inactive")
        identities = await self.repo.identities(session, account.id)
        chosen = identities[0]

        # 轮换：旧令牌立即作废（防重放）
        await self.repo.revoke_refresh_token(session, hash_token(refresh_token))
        access, ttl = create_access_token(
            account_id=account.id, identity_id=chosen.id, active_identity=chosen.identity_type)
        new_raw, new_exp = create_refresh_token()
        await self.repo.save_refresh_token(session, account.id, hash_token(new_raw), new_exp)
        return {"access_token": access, "expires_in": ttl, "refresh_token": new_raw,
                "active_identity": chosen.identity_type, "identity_id": chosen.id}

    async def switch_identity(self, session: AsyncSession, *, principal: Principal,
                              identity_type: str, ip: str | None = None) -> dict:
        """只换 active_identity，account 不变 → 积分/订单跨身份共享（§2.2 约束 3）。"""
        identities = await self.repo.identities(session, principal.account_id)
        target = next((i for i in identities if i.identity_type == identity_type), None)
        if target is None:
            raise Forbidden("identity_not_held")
        access, ttl = create_access_token(
            account_id=principal.account_id, identity_id=target.id, active_identity=identity_type)
        await write_audit(session, actor=principal, action="auth.switch_identity",
                          resource_type="account", resource_id=principal.account_id,
                          after={"active_identity": identity_type}, ip=ip)
        return {"access_token": access, "expires_in": ttl, "active_identity": identity_type,
                "identity_id": target.id}

    async def whoami(self, session: AsyncSession, principal: Principal) -> dict:
        identities = await self.repo.identities(session, principal.account_id)
        memberships = await self.repo.memberships(session, principal.account_id)
        return {
            "account_id": principal.account_id,
            "active_identity": principal.active_identity,
            "identity_id": principal.identity_id,
            "identities": [i.identity_type for i in identities],
            "kyc_level": principal.kyc_level,
            "scope": principal.scope_kind(),
            "orgs": [{"org_id": o.id, "org_type": o.org_type, "name": o.name, "code": o.code,
                      "role": m.role, "level": o.level, "path": o.path}
                     for m, o in memberships],
        }
