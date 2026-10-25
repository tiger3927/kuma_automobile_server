"""演示数据（幂等）：车型、车辆、T-Box 凭据、组织与账号、运营区围栏。

    python scripts/seed_demo.py            # 建/补齐
    python scripts/seed_demo.py --print    # 只打印现有账号口令提示
口令是随机生成并**打印一次**，用于本地/演示环境（生产禁用）。
"""

from __future__ import annotations

import argparse
import asyncio
import secrets

from sqlalchemy import func, select

from src.kuma.core.db import dispose_engine, tx
from src.kuma.core.security import hash_password
from src.kuma.models.core import (
    Account,
    Identity,
    Membership,
    Org,
    OrgBusiness,
    Region,
    SubscriptionPlan,
    Vehicle,
    VehicleCredential,
    VehicleType,
)
from src.kuma.models.geo import Fence

DEMO_TYPES = [
    ("TRI-CARGO-1", "酷马货运三轮", "tricycle", "cargo_tricycle", 1, 0, 250, "TH"),
    ("TRI-PAX-3", "酷马载客三轮", "tricycle", "passenger_tricycle", 3, 3, 150, "TH"),
    ("MINI-VAN-8", "酷马小客车", "minibus", "van", 8, 8, 1000, "ZA"),
]
DEMO_ACCOUNTS = [
    ("demo_owner", "owner"), ("demo_driver", "driver"),
    ("demo_dealer", "dealer_staff"), ("demo_operator", "operator_staff"),
    ("demo_regulator", "regulator"), ("demo_platform", "platform_staff"),
]
# 订阅套餐（TH 首发）。price 用整数最小货币单位（萨当，×100）：9900 = ฿99.00。
# `features` 是能力开关，不是文案——下行控制/围栏告警/派单这些闸门按它判定。
# 演示账号的**组织归属**：只有 `identity` 没有 `membership` 的账号，登录后
# `principal.orgs` 是空 → `scope_kind()` 落到 `self`、`org_ids_for_line()` 恒空，
# 经销商/运营商的列表页永远查不出东西（2026-10-26 实测：库里 membership=0、org_business=0）。
# `role` 受 CHECK 约束（`002_core.sql:61`）且每个 org 只能有一个在职 `primary_admin`
# （`002_core.sql:67` 的部分唯一索引），所以这里按"缺就补"写。
DEMO_MEMBERSHIPS = [
    ("demo_dealer", "D-TH-001", "primary_admin"),
    ("demo_operator", "OP-TH-001", "primary_admin"),
]
# 业务线是**线上事实**（牌照与线下准入的镜像），没有隐式默认：没申报就拿不到换电 scope
# （`core/scope.py:183-192`）。取值受 CHECK 约束（`0007:409-410`）。
DEMO_BUSINESS_LINES = [
    ("D-TH-001", "dealer", None),
    ("OP-TH-001", "swap_operator", "DEMO-SWAP-TH-001"),
]
DEMO_PLANS: list[dict] = [
    dict(code="TH-BASIC-M", region_id="TH", plan_type="basic", name="基础服务（月）",
         price=9900, currency="THB", duration_days=30,
         features={"remote_control": False}, version=1, status="active"),
    dict(code="TH-BASIC-Y", region_id="TH", plan_type="basic", name="基础服务（年）",
         price=99000, currency="THB", duration_days=365,
         features={"remote_control": False}, version=1, status="active"),
    dict(code="TH-VA-M", region_id="TH", plan_type="value_added", name="增值服务（月）",
         price=19900, currency="THB", duration_days=30,
         features={"remote_control": True, "fence_alert": True, "mileage_report": True},
         version=1, status="active"),
    dict(code="TH-OPERATOR-M", region_id="TH", plan_type="operator", name="运营商车队套餐（月）",
         price=29900, currency="THB", duration_days=30,
         features={"remote_control": True, "fence_alert": True, "dispatch": True,
                   "fleet_dashboard": True},
         version=1, status="active"),
]


async def seed(*, print_only: bool) -> None:
    async with tx() as session:
        regions = (await session.execute(select(Region))).scalars().all()
        print(f"region={len(regions)}")
        if print_only:
            return

        for code, name, line, cat, seats, pax, load, region in DEMO_TYPES:
            if not await session.get(VehicleType, code):
                session.add(VehicleType(
                    type_code=code, name=name, product_line=line, category=cat, seats=seats,
                    passenger_seats=pax, load_capacity_kg=load, power_type="ev",
                    battery_kwh=10.5, range_km=120, top_speed_kmh=60,
                    protocol_profile="v1", regions=[region], specs={"demo": True},
                ))
        await session.flush()

        created: list[tuple[str, str, str]] = []
        for username, identity in DEMO_ACCOUNTS:
            existing = (await session.execute(
                select(Account).where(Account.username == username))).scalar_one_or_none()
            if existing:
                continue
            password = secrets.token_urlsafe(12)
            account = Account(username=username, password_hash=hash_password(password),
                              status="active", kyc_level=2, region_id="TH")
            session.add(account)
            await session.flush()
            session.add(Identity(account_id=account.id, identity_type=identity))
            created.append((username, identity, password))

        org_count = (await session.execute(select(func.count()).select_from(Org))).scalar_one()
        if org_count == 0:
            dealer = Org(org_type="dealer", code="D-TH-001", name="曼谷演示经销商",
                         region_id="TH", level=1, path="")
            operator = Org(org_type="operator", code="OP-TH-001", name="曼谷演示运营商",
                           region_id="TH", level=1, path="")
            session.add_all([dealer, operator])
            await session.flush()
            dealer.path, operator.path = f"{dealer.id}/", f"{operator.id}/"

        # 组织归属 + 业务线：缺就补（老库跑一遍 seed 即自愈）
        for username, org_code, role in DEMO_MEMBERSHIPS:
            acct = (await session.execute(
                select(Account).where(Account.username == username))).scalar_one_or_none()
            org = (await session.execute(
                select(Org).where(Org.code == org_code))).scalar_one_or_none()
            if acct is None or org is None:
                print(f"membership skip {username}@{org_code}：账号或组织不存在")
                continue
            has = (await session.execute(select(Membership).where(
                Membership.account_id == acct.id, Membership.org_id == org.id
            ))).scalar_one_or_none()
            if has is None:
                session.add(Membership(account_id=acct.id, org_id=org.id,
                                       role=role, status="active"))
                print(f"membership +{username}@{org_code} ({role})")

        for org_code, line, license_no in DEMO_BUSINESS_LINES:
            org = (await session.execute(
                select(Org).where(Org.code == org_code))).scalar_one_or_none()
            if org is None:
                print(f"org_business skip {org_code}/{line}：组织不存在")
                continue
            has = (await session.execute(select(OrgBusiness).where(
                OrgBusiness.org_id == org.id, OrgBusiness.line == line
            ))).scalar_one_or_none()
            if has is None:
                session.add(OrgBusiness(org_id=org.id, line=line, license_no=license_no,
                                        status="active"))
                print(f"org_business +{org_code}/{line}")

        vehicles = (await session.execute(select(Vehicle).limit(5))).scalars().all()
        if not vehicles:
            dealer = (await session.execute(select(Org).where(Org.code == "D-TH-001"))).scalar_one()
            for i, (code, *_rest) in enumerate(DEMO_TYPES):
                # core.vehicle.vin 是 character(17)：前缀 4 + 13 = 17，多一位就
                # `value too long for type character(17)`（2026-10-20 实测，这脚本此前从没跑通过）
                vin = f"DEMO{i:013d}"
                v = Vehicle(vin=vin, type_code=code, region_id="TH", owner_type="org",
                            owner_org_id=dealer.id, cur_dealer_org_id=dealer.id,
                            plate=f"กข{i:04d}", status="in_service", usage_type="retail")
                session.add(v)
                await session.flush()
                session.add(VehicleCredential(vin=vin, mqtt_username=vin,
                                              password_hash=hash_password(secrets.token_urlsafe(16)),
                                              version=1, status="active"))

        # `org_id` 必须有：派单硬过滤与 `fence_exit` 都按 `org_id` 匹配围栏，
        # 之前 seed 出来的"曼谷运营区"org_id 是 NULL → `inside_operating_area()`
        # 永远匹配不到，围栏看着存在但从不生效（2026-10-22 实测）。
        # 这里写成"缺就补"，老库跑一遍 seed 即自愈，不用单独补迁移。
        operator = (await session.execute(
            select(Org).where(Org.code == "OP-TH-001"))).scalar_one_or_none()
        demo_fence = (await session.execute(
            select(Fence).where(Fence.name == "曼谷运营区"))).scalars().first()
        if demo_fence is None:
            session.add(Fence(
                name="曼谷运营区", fence_type="operating_area", region_id="TH",
                org_id=operator.id if operator else None,
                geom="SRID=4326;POLYGON((100.3 13.6,100.8 13.6,100.8 13.95,100.3 13.95,100.3 13.6))",
            ))
        elif demo_fence.org_id is None and operator is not None:
            demo_fence.org_id = operator.id

        # 订阅套餐：`core.subscription_plan` 之前是**空表**，
        # 于是"创建订阅"根本没有可选套餐——订阅功能整条链路不可用（2026-10-22 实测）。
        # 金额按 Task-DataBase.md §11 规则 3：整数最小货币单位（THB 萨当，×100）。
        # 按 code 逐条补，不看总数：库里可能已有别的来源的套餐（测试残留、人工配置）。
        have = set((await session.execute(select(SubscriptionPlan.code))).scalars().all())
        missing = [SubscriptionPlan(**kw) for kw in DEMO_PLANS if kw["code"] not in have]
        if missing:
            session.add_all(missing)
            print(f"subscription_plan +{len(missing)}")
        await session.flush()

        for username, identity, password in created:
            print(f"ACCOUNT {username} / {identity} / {password}")
    await dispose_engine()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--print", action="store_true", dest="print_only")
    args = p.parse_args()
    asyncio.run(seed(print_only=args.print_only))
