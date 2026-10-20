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
    Account, Identity, Membership, Org, Region, Vehicle, VehicleCredential, VehicleType,
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

        fence_count = (await session.execute(select(func.count()).select_from(Fence))).scalar_one()
        if fence_count == 0:
            session.add(Fence(
                name="曼谷运营区", fence_type="operating_area", region_id="TH",
                geom="SRID=4326;POLYGON((100.3 13.6,100.8 13.6,100.8 13.95,100.3 13.95,100.3 13.6))",
            ))
        await session.flush()

        for username, identity, password in created:
            print(f"ACCOUNT {username} / {identity} / {password}")
    await dispose_engine()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--print", action="store_true", dest="print_only")
    args = p.parse_args()
    asyncio.run(seed(print_only=args.print_only))
