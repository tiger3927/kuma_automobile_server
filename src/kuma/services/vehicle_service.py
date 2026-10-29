"""车辆主数据服务：CRUD、T-Box 绑定/更换、一机一密生成（M2 只生成不下发）、订阅。

状态机（Task-Plan.md §4.1）：produced → shipped → registered → delivered → in_service
→ (transfer) → in_service / retired。状态迁移只允许这里定义的边。
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.audit import write_audit
from ..core.exceptions import Conflict, InvalidState, NotFound, ValidationFailed
from ..core.outbox import emit
from ..core.pagination import PageParams
from ..core.scope import Principal
from ..core.security import ahash_password
from ..models.core import (
    Device,
    Identity,
    OwnershipTransfer,
    Sim,
    Subscription,
    SubscriptionPlan,
    Vehicle,
    VehicleCredential,
)
from ..repositories.base import assert_vehicle_visible
from ..repositories.vehicle_repo import VehicleRepo

VEHICLE_TRANSITIONS: dict[str, set[str]] = {
    "produced": {"shipped", "retired"},
    "shipped": {"registered", "retired"},
    "registered": {"delivered", "retired"},
    "delivered": {"in_service", "registered"},
    "in_service": {"in_service", "retired"},
    "retired": set(),
}


class VehicleService:
    repo = VehicleRepo()

    async def page(self, session: AsyncSession, principal: Principal, *, q: str | None = None,
                   status: str | None = None, type_code: str | None = None,
                   region_id: str | None = None, page: PageParams | None = None) -> dict:
        stmt = self.repo.page(principal, q=q, status=status, type_code=type_code,
                              region_id=region_id, page=page)
        rows = (await session.execute(stmt)).scalars().all()
        total = await self.repo.count(session, principal, status=status, region_id=region_id)
        return {"items": [_vehicle_dict(v) for v in rows],
                "limit": (page or PageParams()).limit, "offset": (page or PageParams()).offset,
                "total": total}

    async def get(self, session: AsyncSession, principal: Principal, vin: str) -> dict:
        vehicle = await self.repo.get_by_vin(session, vin)
        if vehicle is None:
            raise NotFound("vehicle_not_found")
        assert_vehicle_visible(principal, vehicle)
        devices = await self.repo.devices_for_vin(session, vin)
        sub = await self.repo.active_subscription(session, vin)
        return {**_vehicle_dict(vehicle),
                "devices": [{"device_sn": d.device_sn, "model": d.model,
                             "activated_at": d.activated_at} for d in devices],
                "subscription": None if sub is None else {
                    "plan_id": sub.plan_id, "status": sub.status,
                    "start_at": sub.start_at, "end_at": sub.end_at}}

    async def create(self, session: AsyncSession, principal: Principal, *, vin: str,
                     type_code: str, region_id: str, owner_type: str,
                     owner_account_id: int | None = None, owner_org_id: int | None = None,
                     plate: str | None = None, model_year: int | None = None,
                     color: str | None = None, factory_attrs: dict | None = None,
                     ip: str | None = None) -> Vehicle:
        principal.require("vehicle_master", "write")
        if len(vin) != 17:
            raise ValidationFailed("VIN 必须 17 位")
        if await self.repo.get_by_vin(session, vin):
            raise Conflict("vin_exists")
        if owner_type not in ("account", "org"):
            raise ValidationFailed("owner_type 必须是 account 或 org")
        if owner_type == "account" and not owner_account_id:
            raise ValidationFailed("缺 owner_account_id")
        if owner_type == "org" and not owner_org_id:
            raise ValidationFailed("缺 owner_org_id")

        vehicle = Vehicle(
            vin=vin, type_code=type_code, region_id=region_id, owner_type=owner_type,
            owner_account_id=owner_account_id, owner_org_id=owner_org_id, plate=plate,
            model_year=model_year, color=color, factory_attrs=factory_attrs or {},
            status="produced",
        )
        session.add(vehicle)
        await session.flush()
        await write_audit(session, actor=principal, action="vehicle.create",
                          resource_type="vehicle", resource_id=vin,
                          after=_vehicle_dict(vehicle), ip=ip)
        return vehicle

    async def change_status(self, session: AsyncSession, principal: Principal, vin: str,
                            new_status: str, ip: str | None = None) -> Vehicle:
        vehicle = await self.repo.get_by_vin(session, vin)
        if vehicle is None:
            raise NotFound("vehicle_not_found")
        assert_vehicle_visible(principal, vehicle)
        allowed = VEHICLE_TRANSITIONS.get(vehicle.status, set())
        if new_status not in allowed:
            raise InvalidState(f"{vehicle.status} → {new_status} 不允许",
                               detail={"allowed": sorted(allowed)})
        before = {"status": vehicle.status}
        vehicle.status = new_status
        await session.flush()
        await write_audit(session, actor=principal, action="vehicle.status",
                          resource_type="vehicle", resource_id=vin,
                          before=before, after={"status": new_status}, ip=ip)
        await emit(session, aggregate_type="vehicle", aggregate_id=vehicle.id,
                   event_type="status",
                   payload={"vin": vin, "from": before["status"], "to": new_status})
        return vehicle

    async def bind_device(self, session: AsyncSession, principal: Principal, vin: str,
                          device_sn: str, iccid: str | None = None,
                          model: str | None = None, ip: str | None = None) -> Device:
        """T-Box 绑定/更换（§4.3）：旧设备置 replaced_from 链，不删行。"""
        principal.require("vehicle_master", "write")
        vehicle = await self.repo.get_by_vin(session, vin)
        if vehicle is None:
            raise NotFound("vehicle_not_found")
        assert_vehicle_visible(principal, vehicle)

        old = (await session.execute(select(Device).where(Device.vin == vin))).scalars().all()

        device = Device(device_sn=device_sn, vin=vin, model=model, activated_at=datetime.now(UTC))
        session.add(device)
        await session.flush()
        for d in old:
            new = Device(device_sn=d.device_sn, vin=None, model=d.model,
                         activated_at=d.activated_at, replaced_from_id=None)
            session.add(new)
        if iccid:
            session.add(Sim(iccid=iccid, device_id=device.id))
        await write_audit(session, actor=principal, action="vehicle.bind_device",
                          resource_type="vehicle", resource_id=vin,
                          after={"device_sn": device_sn, "replaced": [d.device_sn for d in old]}, ip=ip)
        return device

    async def issue_credential(self, session: AsyncSession, principal: Principal, vin: str,
                               ip: str | None = None) -> dict:
        """一机一密下发（C5，2026-10-22 定口径）。

        **下发协议 v1**：口令由 `dealer_staff`/`platform_staff` 在交付核销时经管理端领取，
        明文**只在这次响应里出现一次**（库里只有 argon2id 哈希，事后查不到），
        由安装人员写入 T-Box。设备自助 bootstrap（出厂密钥换凭据）需要产线流程与
        `vehicle_credential` 加列，v1 不做——但**轮换与吊销必须先能用**，
        否则设备被盗就只能报废 T-Box。

        ⚠️ 旧实现 `session.add(cred)` 在已有凭据时是**主键冲突 500**（注释写着 upsert，
        实际 SQLAlchemy 不会 upsert）。这里改成"有就更新版本"。
        """
        principal.require("vehicle_master", "write")
        vehicle = await self.repo.get_by_vin(session, vin)
        if vehicle is None:
            raise NotFound("vehicle_not_found")
        assert_vehicle_visible(principal, vehicle)

        raw = secrets.token_urlsafe(24)
        cred = await session.get(VehicleCredential, vin)
        first_issue = cred is None
        if cred is None:
            cred = VehicleCredential(vin=vin, mqtt_username=vin, version=1, status="active")
            session.add(cred)
        else:
            cred.version += 1
        cred.password_hash = await ahash_password(raw)
        cred.status = "active"
        cred.issued_at = datetime.now(UTC)
        cred.revoked_at = None
        await session.flush()

        action = "vehicle.credential_issue" if first_issue else "vehicle.credential_rotate"
        event = "issued" if first_issue else "rotated"
        await write_audit(session, actor=principal, action=action,
                          resource_type="vehicle", resource_id=vin,
                          after={"version": cred.version}, ip=ip)
        # 事件里**绝不带口令**：outbox 会进 MQ、可能被多个消费者读到。
        await emit(session, aggregate_type="vehicle.credential", aggregate_id=vin,
                   event_type=event,
                   payload={"vin": vin, "version": cred.version, "status": cred.status,
                            "by": principal.account_id})
        return {"vin": vin, "mqtt_username": vin, "password": raw, "version": cred.version,
                "delivered": False,
                "note": "口令只显示一次；请立即写入 T-Box，丢失只能 rotate"}

    async def rotate_credential(self, session: AsyncSession, principal: Principal, vin: str,
                                ip: str | None = None) -> dict:
        """轮换：口令泄露/设备转手时用。旧口令**立即失效**（每次 MQTT 连接都回库校验）。"""
        principal.require("vehicle_master", "write")
        if await session.get(VehicleCredential, vin) is None:
            raise NotFound("credential_not_found")
        return await self.issue_credential(session, principal, vin, ip=ip)

    async def credential_status(self, session: AsyncSession, principal: Principal,
                                vin: str) -> dict:
        """只读状态，**不返回口令**（哈希也不返回）。"""
        principal.require("vehicle_master", "read")
        vehicle = await self.repo.get_by_vin(session, vin)
        if vehicle is None:
            raise NotFound("vehicle_not_found")
        assert_vehicle_visible(principal, vehicle)
        cred = await session.get(VehicleCredential, vin)
        if cred is None:
            return {"vin": vin, "status": "none", "version": 0}
        return {"vin": vin, "mqtt_username": cred.mqtt_username, "status": cred.status,
                "version": cred.version, "issued_at": cred.issued_at,
                "revoked_at": cred.revoked_at}

    async def revoke_credential(self, session: AsyncSession, principal: Principal, vin: str,
                                ip: str | None = None) -> dict:
        """吊销：设备被盗/报废。此后该 VIN 的 MQTT 连接会被拒（`verify_credential` 查 status）。"""
        principal.require("vehicle_master", "write")
        cred = await session.get(VehicleCredential, vin)
        if cred is None:
            raise NotFound("credential_not_found")
        if cred.status == "revoked":
            raise InvalidState("credential_already_revoked")
        cred.status = "revoked"
        cred.revoked_at = datetime.now(UTC)
        await session.flush()
        await write_audit(session, actor=principal, action="vehicle.credential_revoke",
                          resource_type="vehicle", resource_id=vin,
                          before={"status": "active"}, after={"status": "revoked"}, ip=ip)
        await emit(session, aggregate_type="vehicle.credential", aggregate_id=vin,
                   event_type="revoked",
                   payload={"vin": vin, "version": cred.version, "status": "revoked",
                            "by": principal.account_id})
        return {"vin": vin, "status": "revoked", "version": cred.version}

    async def activate_subscription(self, session: AsyncSession, principal: Principal, vin: str,
                                    plan_id: int, ip: str | None = None) -> Subscription:
        """订阅是生态里唯一与车强绑定的持续收入（§4.2）；一车同时只允许一条 active。"""
        vehicle = await self.repo.get_by_vin(session, vin)
        if vehicle is None:
            raise NotFound("vehicle_not_found")
        assert_vehicle_visible(principal, vehicle)
        if await self.repo.active_subscription(session, vin):
            raise Conflict("subscription_active")
        plan = await session.get(SubscriptionPlan, plan_id)
        if plan is None or plan.status != "active":
            raise ValidationFailed("plan 不可用")
        if plan.region_id and plan.region_id != vehicle.region_id:
            raise ValidationFailed("plan 与车辆地区不匹配")

        now = datetime.now(UTC)
        # ⚠️ 此前这里写死 `end_at=None`，等于**订阅永不过期**——
        # `auto_renew` / `end_at` 没有任何代码读，到期流转根本无从发生（C6 的根因）。
        # 套餐的 duration_days 必须落到 end_at，续费与宽限期才有依据。
        sub = Subscription(vin=vin, plan_id=plan_id, status="active", start_at=now,
                           end_at=now + timedelta(days=plan.duration_days),
                           auto_renew=True, payer_account_id=principal.account_id)
        session.add(sub)
        await session.flush()
        await write_audit(session, actor=principal, action="subscription.activate",
                          resource_type="vehicle", resource_id=vin,
                          after={"plan_id": plan_id}, ip=ip)
        await emit(session, aggregate_type="subscription", aggregate_id=sub.id,
                   event_type="activated",
                   payload={"vin": vin, "plan_id": plan_id, "status": sub.status,
                            "start_at": sub.start_at, "end_at": sub.end_at,
                            "accounts": [principal.account_id]})
        return sub

    async def subscription_detail(self, session: AsyncSession, principal: Principal,
                                  vin: str) -> dict:
        """C6：此前订阅只有创建、没有任何读取路径（`auto_renew`/`end_at` 无人读）。"""
        vehicle = await self.repo.get_by_vin(session, vin)
        if vehicle is None:
            raise NotFound("vehicle_not_found")
        assert_vehicle_visible(principal, vehicle)
        rows = (await session.execute(
            select(Subscription, SubscriptionPlan)
            .join(SubscriptionPlan, SubscriptionPlan.id == Subscription.plan_id)
            .where(Subscription.vin == vin)
            .order_by(Subscription.start_at.desc().nullslast()).limit(20)
        )).all()
        return {"vin": vin, "items": [
            {"id": s.id, "status": s.status, "start_at": s.start_at, "end_at": s.end_at,
             "auto_renew": s.auto_renew, "plan_id": s.plan_id,
             "plan_code": p.code, "plan_type": p.plan_type,
             "duration_days": p.duration_days} for s, p in rows]}

    async def cancel_subscription(self, session: AsyncSession, principal: Principal, vin: str,
                                  *, ip: str | None = None) -> dict:
        """取消 = `cancelled` 终态 + 关自动续费。**不删行**：订阅是收入与结算凭据。"""
        sub = await self.repo.active_subscription(session, vin)
        if sub is None:
            raise NotFound("subscription_not_found")
        vehicle = await self.repo.get_by_vin(session, vin)
        if vehicle is None:
            raise NotFound("vehicle_not_found")
        assert_vehicle_visible(principal, vehicle)
        before = {"status": sub.status, "end_at": sub.end_at, "auto_renew": sub.auto_renew}
        sub.status = "cancelled"
        sub.auto_renew = False
        await session.flush()
        await write_audit(session, actor=principal, action="subscription.cancel",
                          resource_type="vehicle", resource_id=vin,
                          before=before, after={"status": "cancelled", "auto_renew": False},
                          ip=ip)
        await emit(session, aggregate_type="subscription", aggregate_id=sub.id,
                   event_type="cancelled",
                   payload={"vin": vin, "status": "cancelled", "auto_renew": False,
                            "accounts": [principal.account_id]})
        return {"id": sub.id, "vin": vin, "status": sub.status}

    async def transfer_ownership(self, session: AsyncSession, principal: Principal, vin: str,
                                 *, to_type: str, to_account_id: int | None = None,
                                 to_org_id: int | None = None, ip: str | None = None) -> dict:
        """过户（§4.4）：历史 trip 归原主（trip 上已快照 owner），运营绑定自动解除。"""
        principal.require("vehicle_master", "write")
        vehicle = await self.repo.get_by_vin(session, vin)
        if vehicle is None:
            raise NotFound("vehicle_not_found")
        assert_vehicle_visible(principal, vehicle)

        from ..models.mobility import FleetContract

        released = 0
        contracts = (await session.execute(
            select(FleetContract).where(
                FleetContract.vehicle_id == vehicle.id, FleetContract.status == "active")
        )).scalars().all()
        for c in contracts:
            c.status = "terminated"
            released += 1

        # ⚠️ 必须在改字段**之前**抓 before：写在 mutation 之后读到的是新归属，
        # 审计里的"变更前"会变成变更后（sub_expiry 也犯过同样的错，已修）。
        before_owner = {"owner_type": vehicle.owner_type,
                        "owner_account_id": vehicle.owner_account_id,
                        "owner_org_id": vehicle.owner_org_id}
        session.add(OwnershipTransfer(
            vin=vin, from_type=before_owner["owner_type"],
            from_account_id=before_owner["owner_account_id"],
            from_org_id=before_owner["owner_org_id"],
            to_type=to_type, to_account_id=to_account_id, to_org_id=to_org_id,
            transferred_at=datetime.now(UTC), operator_id=principal.account_id,
        ))
        vehicle.owner_type, vehicle.owner_account_id, vehicle.owner_org_id = (
            to_type, to_account_id, to_org_id)
        await session.flush()

        # **过户必须同事务给受让账号发 `owner` 身份**（`Task-Server.md` §18.8）。
        # 不发就是"提了车看不到车"：`repositories/base.py:31` 的车辆可见性以
        # `"owner" in principal.identities` 为闸门，注册只发 `consumer`
        # （`auth_service.py:23-24`），而 `consumer` 在 `R_VEHICLE_LIVE` 上是空集
        # （`scope.py:70`）——`GET /vehicles` 空列表、WS 连不上。
        # 过户到 org（运营商/经销商车队）不发：org 侧靠 Membership + org scope 看得见。
        granted: str | None = None
        if to_type == "account" and to_account_id:
            granted = await self._grant_owner_identity(session, int(to_account_id))

        await write_audit(session, actor=principal, action="vehicle.transfer",
                          resource_type="vehicle", resource_id=vin,
                          before=before_owner,
                          after={"to_type": to_type, "to_account_id": to_account_id,
                                 "to_org_id": to_org_id, "released_contracts": released,
                                 "owner_identity": granted}, ip=ip)
        # 双方都要收到：原主失去车辆、新主获得车辆（WS 列表立刻刷新，不靠轮询）
        await emit(session, aggregate_type="vehicle", aggregate_id=vehicle.id,
                   event_type="ownership",
                   payload={"vin": vin, "from_account": before_owner["owner_account_id"],
                            "to_account": to_account_id,
                            "accounts": [a for a in (before_owner["owner_account_id"],
                                                     to_account_id) if a]})
        return {"vin": vin, "released_contracts": released, "owner_identity": granted}

    async def _grant_owner_identity(self, session: AsyncSession, account_id: int) -> str:
        """给账号补 `owner` 身份，返回 `created` / `already_active` / `reactivated`。

        三条都要处理，缺任何一条都会在真机上出现"过户成功但车主看不见车"：

        - **没有行** → 插入。`uq_identity(account_id, identity_type)`
          （`models/core.py:132`）让重复授予天然幂等，所以不需要先查后插。
        - **已有 active 行** → 什么都不做（第二次过户、或本来就有多台车）。
        - **有行但 status≠active** → **必须复活**。`resolve_principal` 只取
          `Identity.status == 'active'`（`core/deps.py:44`），留着 revoked 行
          等于"过户成功、身份在，但解析不到"——最难查的一种瞎法。
          这里只复活 `owner` 这一种身份，不碰 driver / dealer_staff：
          那些是审核与组织流程授予的能力，撤销它们有别的含义。

        **不撤销原主的 `owner` 身份**：身份是"能看自己名下车辆"的能力，不是所有权；
        原主可能还有别的车，撤销会把他那些车一起弄瞎。
        """
        inserted = (await session.execute(
            pg_insert(Identity)
            .values(account_id=account_id, identity_type="owner")
            .on_conflict_do_nothing(index_elements=["account_id", "identity_type"])
            .returning(Identity.id)
        )).first()
        if inserted is not None:
            await session.flush()
            return "created"

        status = (await session.execute(
            select(Identity.status).where(Identity.account_id == account_id,
                                          Identity.identity_type == "owner")
        )).scalar()
        if status == "active":
            return "already_active"
        await session.execute(
            update(Identity)
            .where(Identity.account_id == account_id, Identity.identity_type == "owner")
            .values(status="active"))
        await session.flush()
        return "reactivated"

    async def breakdown(self, session: AsyncSession, principal: Principal) -> list[dict]:
        return await self.repo.status_breakdown(session, principal)


def _vehicle_dict(v: Vehicle) -> dict:
    return {
        "id": v.id, "vin": v.vin, "type_code": v.type_code, "plate": v.plate,
        "status": v.status, "usage_type": v.usage_type, "region_id": v.region_id,
        "owner_type": v.owner_type, "owner_account_id": v.owner_account_id,
        "owner_org_id": v.owner_org_id, "cur_dealer_org_id": v.cur_dealer_org_id,
        "model_year": v.model_year, "color": v.color,
    }
