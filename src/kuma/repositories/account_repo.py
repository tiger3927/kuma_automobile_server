"""账号、叠加身份、组织成员关系、刷新令牌。"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import or_, select

from ..core.pagination import PageParams
from ..models.core import Account, Identity, Membership, Org, RefreshToken
from .base import count_of

IDENTITY_TYPES = (
    "consumer", "owner", "driver", "dealer_staff", "manufacturer_staff",
    "operator_staff", "regulator", "platform_staff",
)


class AccountRepo:
    async def by_username(self, session, username: str) -> Account | None:
        return (await session.execute(
            select(Account).where(Account.username == username)
        )).scalar_one_or_none()

    async def by_phone(self, session, phone: str) -> Account | None:
        return (await session.execute(select(Account).where(Account.phone == phone))).scalar_one_or_none()

    async def get(self, session, account_id: int) -> Account | None:
        return await session.get(Account, account_id)

    async def identities(self, session, account_id: int, *, active_only: bool = True) -> Sequence[Identity]:
        stmt = select(Identity).where(Identity.account_id == account_id)
        if active_only:
            stmt = stmt.where(Identity.status == "active")
        return (await session.execute(stmt.order_by(Identity.identity_type))).scalars().all()

    async def add_identity(self, session, account_id: int, identity_type: str) -> Identity:
        if identity_type not in IDENTITY_TYPES:
            raise ValueError(f"未知身份 {identity_type}")
        identity = Identity(account_id=account_id, identity_type=identity_type)
        session.add(identity)
        await session.flush()
        return identity

    async def memberships(self, session, account_id: int) -> Sequence[tuple[Membership, Org]]:
        rows = (await session.execute(
            select(Membership, Org).join(Org, Org.id == Membership.org_id)
            .where(Membership.account_id == account_id, Membership.status == "active")
        )).all()
        return [(m, o) for m, o in rows]

    async def org_by_code(self, session, code: str) -> Org | None:
        return (await session.execute(select(Org).where(Org.code == code))).scalar_one_or_none()

    async def orgs_page(self, session, *, org_type: str, region_id: str | None = None,
                        q: str | None = None, page: PageParams | None = None
                        ) -> tuple[Sequence[Org], int]:
        """公开目录用（portal 经销商查询）：只列 active，返回 (行, 总数)。"""
        stmt = select(Org).where(Org.org_type == org_type, Org.status == "active")
        if region_id:
            stmt = stmt.where(Org.region_id == region_id)
        if q:
            like = f"%{q}%"
            stmt = stmt.where(or_(Org.name.ilike(like), Org.code.ilike(like)))
        total = await count_of(session, stmt)
        p = page or PageParams()
        rows = (await session.execute(
            stmt.order_by(Org.region_id.nulls_last(), Org.name).limit(p.limit).offset(p.offset)
        )).scalars().all()
        return rows, total

    async def save_refresh_token(self, session, account_id: int, token_hash: str,
                                 expires_at, device_id: str | None = None):
        rt = RefreshToken(account_id=account_id, token_hash=token_hash,
                          expires_at=expires_at, device_id=device_id)
        session.add(rt)
        await session.flush()
        return rt

    async def find_refresh_token(self, session, token_hash: str) -> RefreshToken | None:
        return (await session.execute(
            select(RefreshToken).where(RefreshToken.token_hash == token_hash)
        )).scalar_one_or_none()

    async def revoke_refresh_token(self, session, token_hash: str) -> bool:
        rt = await self.find_refresh_token(session, token_hash)
        if rt is None or rt.revoked_at is not None:
            return False
        from datetime import UTC, datetime

        rt.revoked_at = datetime.now(UTC)
        await session.flush()
        return True
