"""组织（org ≠ account，Task-Plan.md §2.4）：建组织、成员、主管理员转移。

`path` 用 `/1/5/9/` 形式，子树查询走 `path LIKE '/1/5/%'`（text_pattern_ops 索引）。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.audit import write_audit
from ..core.exceptions import Conflict, Forbidden, NotFound, ValidationFailed
from ..core.scope import Principal
from ..models.core import Account, Identity, Membership, Org
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

    async def update_contact(self, session: AsyncSession, principal: Principal, org_id: int, *,
                             name: str | None = None, address: str | None = None,
                             phone: str | None = None, lat: float | None = None,
                             lng: float | None = None, ip: str | None = None) -> dict:
        """改名称与联系方式（portal 经销商页要展示地址/电话/坐标，§11-7）。"""
        principal.require("org", "write")
        org = await session.get(Org, org_id)
        if org is None:
            raise NotFound("org_not_found")
        # 平台改任何组织；其它身份只能改自己所属的组织（`org:write` 本身不带 scope 收敛）
        if not principal.is_platform and org_id not in principal.org_ids():
            raise Forbidden("org_out_of_scope")
        patch = {"name": name, "address": address, "phone": phone, "lat": lat, "lng": lng}
        given = {k: v for k, v in patch.items() if v is not None}
        if not given:
            raise ValidationFailed("没有要改的字段", code="empty_patch")
        if (lat is None) != (lng is None):
            raise ValidationFailed("lat/lng 必须成对给", code="geo_pair")
        before = {k: getattr(org, k) for k in given}
        for k, v in given.items():
            setattr(org, k, v)
        await session.flush()
        await write_audit(session, actor=principal, action="org.update_contact",
                          resource_type="org", resource_id=org_id,
                          before=before, after=given, ip=ip)
        return {"org_id": org_id, **{k: getattr(org, k) for k in given}}

    async def members(self, session: AsyncSession, principal: Principal,
                      org_id: int) -> list[dict]:
        """组织在职成员（**联审人候选的唯一来源**，`Task-Web.md` §5-6）。

        带上每人**当前有效身份**：双人授权要求联审人是 `platform_staff`
        （`dual_control.require_co_approver` 会拒非平台身份），前端没有这个字段
        就只能让人盲填 account_id 然后吃 403。
        不返回 phone/email——同事之间需要认人，不需要彼此的联系方式。
        """
        principal.require("org", "read")
        org = await session.get(Org, org_id)
        if org is None:
            raise NotFound("org_not_found")
        if not (principal.is_platform or principal.is_regulator
                or org_id in principal.org_ids()):
            raise Forbidden("org_out_of_scope")
        rows = (await session.execute(
            select(Membership, Account)
            .join(Account, Account.id == Membership.account_id)
            .where(Membership.org_id == org_id, Membership.status == "active")
            .order_by(Membership.role, Account.id)
        )).all()
        account_ids = [a.id for _, a in rows]
        identities: dict[int, list[str]] = {}
        if account_ids:
            id_rows = (await session.execute(
                select(Identity.account_id, Identity.identity_type)
                .where(Identity.account_id.in_(account_ids), Identity.status == "active")
            )).all()
            for acc_id, idt in id_rows:
                identities.setdefault(acc_id, []).append(idt)
        return [{"account_id": a.id, "username": a.username, "role": m.role,
                 "kyc_level": a.kyc_level, "identities": sorted(identities.get(a.id, []))}
                for m, a in rows]

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
