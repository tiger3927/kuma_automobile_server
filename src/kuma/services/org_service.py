"""组织（org ≠ account，Task-Plan.md §2.4）：建组织、成员、主管理员转移。

`path` 用 `/1/5/9/` 形式，子树查询走 `path LIKE '/1/5/%'`（text_pattern_ops 索引）。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.audit import write_audit
from ..core.exceptions import Conflict, Forbidden, NotFound, ValidationFailed
from ..core.scope import Principal
from ..models.core import Membership, Org
from ..repositories.account_repo import AccountRepo

ORG_TYPES = ("manufacturer", "dealer", "operator", "government")


class OrgService:
    accounts = AccountRepo()

    async def create(self, session: AsyncSession, principal: Principal, *, org_type: str,
                     code: str, name: str, region_id: str | None = None,
                     parent_code: str | None = None, primary_account_id: int | None = None,
                     ip: str | None = None) -> Org:
        principal.require("org", "write")
        if org_type not in ORG_TYPES:
            raise ValidationFailed("org_type 非法")
        if await self.accounts.org_by_code(session, code):
            raise Conflict("org_code_exists")

        parent: Org | None = None
        if parent_code:
            parent = await self.accounts.org_by_code(session, parent_code)
            if parent is None:
                raise NotFound("parent_org_not_found")
            if parent.org_type == "operator" and org_type != "operator":
                raise ValidationFailed("运营商子组织只能是运营商")

        primary = primary_account_id or principal.account_id
        org = Org(org_type=org_type, code=code, name=name, region_id=region_id,
                  parent_org_id=parent.id if parent else None,
                  level=(parent.level + 1) if parent else 1,
                  path="", primary_account_id=primary)
        session.add(org)
        await session.flush()
        org.path = (parent.path if parent else "") + f"{org.id}/"

        session.add(Membership(account_id=primary, org_id=org.id, role="primary_admin"))
        if primary != principal.account_id:
            session.add(Membership(account_id=principal.account_id, org_id=org.id, role="admin"))
        await session.flush()
        await write_audit(session, actor=principal, action="org.create", resource_type="org",
                          resource_id=org.id, after={"org_type": org_type, "code": code,
                                                     "primary_account_id": primary}, ip=ip)
        return org

    async def tree(self, session: AsyncSession, principal: Principal,
                   root_id: int | None = None) -> list[dict]:
        principal.require("org", "read")
        stmt = select(Org).where(Org.status == "active")
        if principal.is_platform or principal.is_regulator:
            pass
        else:
            orgs = principal.org_ids()
            if not orgs:
                return []
            stmt = stmt.where(Org.id.in_(orgs))
        if root_id:
            root = await session.get(Org, root_id)
            if root is None:
                raise NotFound("org_not_found")
            stmt = select(Org).where(Org.path.like(f"{root.path}%"), Org.status == "active")
        rows = (await session.execute(stmt.order_by(Org.path))).scalars().all()
        return [{"id": o.id, "code": o.code, "name": o.name, "org_type": o.org_type,
                 "level": o.level, "path": o.path, "parent_org_id": o.parent_org_id,
                 "primary_account_id": o.primary_account_id} for o in rows]

    async def add_member(self, session: AsyncSession, principal: Principal, org_id: int,
                         account_id: int, role: str, ip: str | None = None) -> dict:
        if role == "primary_admin":
            raise ValidationFailed("主管理员必须走 /orgs/{id}/primary-admin 转移流程")
        if role not in ("admin", "agent", "auditor"):
            raise ValidationFailed("role 非法")
        org = await session.get(Org, org_id)
        if org is None:
            raise NotFound("org_not_found")
        if org.org_type != principal_active_org_type(principal) and not principal.is_platform:
            raise Forbidden("org_type_mismatch")
        if not principal.is_primary_admin(org_id) and not principal.is_platform:
            raise Forbidden("need_primary_admin")

        existing = (await session.execute(
            select(Membership).where(Membership.account_id == account_id,
                                     Membership.org_id == org_id)
        )).scalar_one_or_none()
        if existing and existing.status == "active":
            raise Conflict("already_member")
        if existing:
            existing.status, existing.role = "active", role
        else:
            session.add(Membership(account_id=account_id, org_id=org_id, role=role))
        await session.flush()
        await write_audit(session, actor=principal, action="org.add_member", resource_type="org",
                          resource_id=org_id, after={"account_id": account_id, "role": role}, ip=ip)
        return {"org_id": org_id, "account_id": account_id, "role": role}

    async def transfer_primary_admin(self, session: AsyncSession, principal: Principal,
                                     org_id: int, new_account_id: int,
                                     co_approver_account_id: int | None,
                                     ip: str | None = None) -> dict:
        """主管理员转移：双人授权 + 审计（§2.4）。"""
        if not principal.is_primary_admin(org_id) and not principal.is_platform:
            raise Forbidden("need_primary_admin")
        if principal.is_platform and co_approver_account_id is None:
            raise Forbidden("dual_control_required")

        org = await session.get(Org, org_id)
        if org is None:
            raise NotFound("org_not_found")
        target = (await session.execute(
            select(Membership).where(Membership.org_id == org_id,
                                     Membership.account_id == new_account_id,
                                     Membership.status == "active")
        )).scalar_one_or_none()
        if target is None:
            raise ValidationFailed("新主管理员必须是该组织在职成员")

        old = (await session.execute(
            select(Membership).where(Membership.org_id == org_id, Membership.role == "primary_admin",
                                     Membership.status == "active")
        )).scalars().all()
        for m in old:
            m.role = "admin"
        target.role = "primary_admin"
        org.primary_account_id = new_account_id
        await session.flush()
        await write_audit(session, actor=principal, action="org.transfer_primary_admin",
                          resource_type="org", resource_id=org_id,
                          before={"primary_account_id": org.primary_account_id},
                          after={"new_account_id": new_account_id,
                                 "co_approver": co_approver_account_id}, ip=ip)
        return {"org_id": org_id, "primary_account_id": new_account_id}


def principal_active_org_type(principal: Principal) -> str:
    return {
        "dealer_staff": "dealer",
        "manufacturer_staff": "manufacturer",
        "operator_staff": "operator",
        "regulator": "government",
    }.get(principal.active_identity, "")
