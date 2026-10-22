"""车辆主数据服务：CRUD、T-Box 绑定/更换、一机一密生成（M2 只生成不下发）、订阅。

状态机（Task-Plan.md §4.1）：produced → shipped → registered → delivered → in_service
→ (transfer) → in_service / retired。状态迁移只允许这里定义的边。
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.audit import write_audit
from ..core.exceptions import Conflict, InvalidState, NotFound, ValidationFailed
from ..core.pagination import PageParams
from ..core.scope import Principal
from ..core.security import ahash_password
from ..models.core import (
    Device,
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
        """一机一密：M2 只生成，**下发在 M9 交付核销 + 订阅生效之后**（§8.3）。

        返回明文口令仅一次；库里只存哈希。
        """
        principal.require("vehicle_master", "write")
        vehicle = await self.repo.get_by_vin(session, vin)
        if vehicle is None:
            raise NotFound("vehicle_not_found")
        assert_vehicle_visible(principal, vehicle)

        raw = secrets.token_urlsafe(24)
        existing = await session.get(VehicleCredential, vin)
        version = (existing.version + 1) if existing else 1
        cred = VehicleCredential(
            vin=vin, mqtt_username=vin, password_hash=await ahash_password(raw),
            version=version, status="active", issued_at=datetime.now(UTC), revoked_at=None,
        )
        session.add(cred)  # 主键冲突 → upsert
        await session.flush()
        await write_audit(session, actor=principal, action="vehicle.credential_issue",
                          resource_type="vehicle", resource_id=vin,
                          after={"version": version}, ip=ip)
        return {"vin": vin, "mqtt_username": vin, "password": raw, "version": version,
                "delivered": False, "note": "凭据已生成，交付核销 + 订阅生效后才下发"}

    async def revoke_credential(self, session: AsyncSession, principal: Principal, vin: str,
                                ip: str | None = None) -> None:
        principal.require("vehicle_master", "write")
        cred = await session.get(VehicleCredential, vin)
        if cred is None:
            raise NotFound("credential_not_found")
        cred.status = "revoked"
        cred.revoked_at = datetime.now(UTC)
        await session.flush()
        await write_audit(session, actor=principal, action="vehicle.credential_revoke",
                          resource_type="vehicle", resource_id=vin, ip=ip)

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

        session.add(OwnershipTransfer(
            vin=vin, from_type=vehicle.owner_type,
            from_account_id=vehicle.owner_account_id, from_org_id=vehicle.owner_org_id,
            to_type=to_type, to_account_id=to_account_id, to_org_id=to_org_id,
            transferred_at=datetime.now(UTC), operator_id=principal.account_id,
        ))
        vehicle.owner_type, vehicle.owner_account_id, vehicle.owner_org_id = (
            to_type, to_account_id, to_org_id)
        await session.flush()
        await write_audit(session, actor=principal, action="vehicle.transfer",
                          resource_type="vehicle", resource_id=vin,
                          before={"owner": vehicle.owner_account_id},
                          after={"to_type": to_type, "released_contracts": released}, ip=ip)
        return {"vin": vin, "released_contracts": released}

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
