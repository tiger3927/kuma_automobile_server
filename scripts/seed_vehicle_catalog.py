"""把 12 个整车车型写进"车型字典 + 商城目录"（幂等，可重复跑）。

    python scripts/seed_vehicle_catalog.py          # 写入/更新
    python scripts/seed_vehicle_catalog.py --dry-run  # 只打印将写入的内容

两处落点、各管一件事（别混）：

- `core.vehicle_type`：**车型身份**（type_code/产品线/座位/区域）。`commerce.lead.intent_type_code`
  是这张表的 FK，询价要靠它归属车型；VIN 上报也认这里的车型。
- `commerce.product`：**可售商品**（指导价 `msrp`、营销参数 `attrs.specs`、车型图 `image_url`）。
  整车**不建 SKU**（`commerce.sku.price` 是 NOT NULL，而资料里没有成交价）——成交走询价，
  所以目录里这类商品的 `from_price` 是 NULL、`msrp` 才是要展示的"指导价"。

价格口径：`msrp` 是**指导价**（`attrs.price_kind = 'guideline'`），单位是**最小币种单位**
（THB 的 satang，฿189,000 → 18900000）。这些数字是 2026-10-30 按同级车型市场区间给的
展示用指导价，**不是报价也不是成交价**；要改价直接改下面的 `MSRP_THB` 再跑一遍本脚本。

图片：`/vehicles/<code>.jpg`，源图在 `素材/资料/` 经 ComfyUI 美化的棚拍图（`资料/ReadME.md` 第七节），
portal 与 guest 各自的 `public/vehicles/` 下都放了同名副本（`image_url` 存站内绝对路径，
guest 侧由前端加 `/app` 前缀，见 `web/guest/src/views/ShopVehicleDetail.vue`）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

from sqlalchemy import text

from src.kuma.core.db import dispose_engine, tx

#: 指导价（THB 元 → 脚本里写元，落库时 ×100 转 satang）
MSRP_THB: dict[str, int] = {
    "kuma310": 189_000,
    "jlyz": 69_000,
    "jlpb": 79_000,
    "lb01": 139_000,
    "lbtibb": 49_000,
    "lbtpg": 59_000,
    "cleaner": 89_000,
    "sideflip": 289_000,
    "washer500": 129_000,
    "sweeper": 459_000,
    "cr1": 690_000,
    "minibus": 1_290_000,
}

#: 12 个车型。`specs` 照抄 `资料/ReadME.md`（null = 照片未拍到，前端显示"未公布"，**不猜值**）
VEHICLES: list[dict[str, Any]] = [
    {
        "code": "kuma310", "type_code": "KUMA-310", "name": "电动载客三轮 KUMA 310",
        "model": "KUMA 310", "line": "passenger", "category": "passenger_tricycle",
        "product_line": "tricycle", "seats": 3, "passenger_seats": 3,
        "image_kind": "real", "hero": True,
        "specs": [
            {"k": "size", "v": "2700×1400×1800 mm"},
            {"k": "wheelbase", "v": "2200 mm"},
            {"k": "clearance", "v": "180 mm"},
            {"k": "motor", "v": "5 kW 持续 · 100 N·m 峰值扭矩（汽车级扁平铜线永磁同步，五合一集成）"},
            {"k": "driveMode", "v": "Eco / Power / Climb / 泊车辅助"},
            {"k": "battery", "v": "SBS 4.6 / 6.9 / 9.2 kWh · CTB 8 kWh"},
            {"k": "range", "v": "CTB 140 km · SBS 80 / 120 / 160 km（认证）"},
            {"k": "topSpeed", "v": "55 km/h"},
            {"k": "grade", "v": "40%"},
            {"k": "charging", "v": "CTB 3 h · 80% < 2 h（车载充电器）"},
            {"k": "ip", "v": "IP67"},
            {"k": "brakes", "v": "鼓 / 鼓（选配 碟 / 碟）"},
            {"k": "tyres", "v": "120/80 R12 子午线真空胎"},
            {"k": "suspension", "v": "后轮独立弹簧悬挂 + 防倾杆"},
            {"k": "cluster", "v": "7\" TFT 1080P"},
            {"k": "extras", "v": "一键启动 · 手套箱 · 车内灯 · USB Type A · 双汽车级 LED · 再生制动"},
        ],
    },
    {
        "code": "jlyz", "type_code": "JL-YZ", "name": "电动货运三轮 JL 远征",
        "model": "JL 远征", "line": "cargo", "category": "cargo_tricycle",
        "product_line": "tricycle", "seats": 1, "passenger_seats": 0,
        "specs": [
            {"k": "size", "v": "1600×1100 mm"},
            {"k": "motor", "v": "1000 W 磁驱电控"},
            {"k": "suspension", "v": "37 外置弹簧"},
            {"k": "axle", "v": "160 全浮式换挡"},
            {"k": "tyres", "v": "前 375-12 / 后 400-12"},
        ],
    },
    {
        "code": "jlpb", "type_code": "JL-BP", "name": "电动货运三轮 JL 半蓬",
        "model": "JL 半蓬", "line": "cargo", "category": "cargo_tricycle",
        "product_line": "tricycle", "seats": 1, "passenger_seats": 0,
        "specs": [
            {"k": "size", "v": "1600×1100 mm"},
            {"k": "motor", "v": "1000 W 磁驱电控"},
            {"k": "suspension", "v": "37 外置弹簧"},
            {"k": "axle", "v": None},
            {"k": "tyres", "v": "前 375-12 / 后 400-12"},
        ],
    },
    {
        "code": "lb01", "type_code": "LB-01", "name": "电动载客三轮 嘟嘟车 LB-01",
        "model": "LB-01", "line": "passenger", "category": "passenger_tricycle",
        "product_line": "tricycle", "seats": 3, "passenger_seats": 3,
        "specs": [
            {"k": "size", "v": "2550×1350×1800 mm"},
            {"k": "motor", "v": "60/72 V 4500 W"},
            {"k": "battery", "v": "72 V 160 Ah 锂电"},
            {"k": "curb", "v": "375 kg"},
            {"k": "load", "v": "350 kg"},
            {"k": "range", "v": "150 km"},
        ],
    },
    {
        "code": "lbtibb", "type_code": "LB-TTB", "name": "休闲载客三轮 LB-TTB",
        "model": "LB-TTB", "line": "passenger", "category": "passenger_tricycle",
        "product_line": "tricycle", "seats": 3, "passenger_seats": 3,
        "specs": [
            {"k": "size", "v": "1570×795×1050 mm"},
            {"k": "motor", "v": "650 W"},
            {"k": "battery", "v": "60 V 32 Ah"},
            {"k": "brakes", "v": "前 110 / 后 130 鼓刹"},
            {"k": "tyres", "v": "300-8 铝圈钢丝胎"},
        ],
    },
    {
        "code": "lbtpg", "type_code": "LB-TPG", "name": "蓬篷载客三轮 LB-TPG",
        "model": "LB-TPG", "line": "passenger", "category": "passenger_tricycle",
        "product_line": "tricycle", "seats": 3, "passenger_seats": 3, "image_kind": "gen",
        "specs": [
            {"k": "size", "v": "2110×905×1740 mm"},
            {"k": "motor", "v": "650 W"},
            {"k": "battery", "v": "60/72 V 32 Ah"},
            {"k": "controller", "v": "60/72 V 12 管"},
            {"k": "brakes", "v": "前碟 / 后 130 鼓"},
            {"k": "tyres", "v": "300-10 钢圈真空胎"},
        ],
    },
    {
        "code": "cleaner", "type_code": "SAN-CLEANER", "name": "快速保洁车",
        "model": "", "line": "sanitation", "category": "cleaner",
        "product_line": "sanitation", "seats": 1, "passenger_seats": 0,
        "specs": [
            {"k": "bed", "v": "1190×810×700 mm"},
            {"k": "topSpeed", "v": "30 km/h"},
            {"k": "curb", "v": "141 kg"},
            {"k": "motor", "v": "550–1200 W"},
            {"k": "load", "v": "225 kg"},
            {"k": "volume", "v": "500 L"},
        ],
    },
    {
        "code": "sideflip", "type_code": "SAN-SIDEFLIP", "name": "三轮侧翻桶清运车",
        "model": "", "line": "sanitation", "category": "bin_lifter",
        "product_line": "sanitation", "seats": 1, "passenger_seats": 0,
        "specs": [
            {"k": "bed", "v": "2200×1400×1400 mm"},
            {"k": "topSpeed", "v": "30 km/h"},
            {"k": "curb", "v": "900 kg"},
            {"k": "motor", "v": "4000 W"},
            {"k": "load", "v": "1200 kg"},
            {"k": "volume", "v": "3600 L"},
        ],
    },
    {
        "code": "washer500", "type_code": "SAN-WASHER", "name": "500L 电动高压冲洗车",
        "model": "", "line": "sanitation", "category": "washer",
        "product_line": "sanitation", "seats": 1, "passenger_seats": 0, "image_kind": "gen",
        "specs": [
            {"k": "bed", "v": "1500×1100×950 mm"},
            {"k": "topSpeed", "v": "30 km/h"},
            {"k": "curb", "v": "417 kg"},
            {"k": "motor", "v": "1500–2200 W"},
            {"k": "load", "v": "375 kg"},
            {"k": "volume", "v": "500 L"},
        ],
    },
    {
        "code": "sweeper", "type_code": "SAN-SWEEPER", "name": "电动纯吸式清扫车",
        "model": "", "line": "sanitation", "category": "sweeper",
        "product_line": "sanitation", "seats": 1, "passenger_seats": 0,
        "specs": [
            {"k": "size", "v": "3600×1500×1900 mm"},
            {"k": "topSpeed", "v": "30 km/h"},
            {"k": "curb", "v": "1350 kg"},
            {"k": "motor", "v": "4000 W"},
            {"k": "load", "v": "1200 kg"},
            {"k": "volume", "v": "1200 L"},
        ],
    },
    {
        "code": "cr1", "type_code": "AUTO-CR1", "name": "无人扫地车 CR-1",
        "model": "CR-1", "line": "autonomous", "category": "robot_sweeper",
        "product_line": "robot", "seats": 1, "passenger_seats": 0,
        "specs": [
            {"k": "size", "v": "1182×1780×1067 mm"},
            {"k": "coverage", "v": "8000 m²/h"},
            {"k": "cleanWidth", "v": "1.6 m"},
            {"k": "tank", "v": "20 L"},
            {"k": "bins", "v": "200 L"},
            {"k": "topSpeed", "v": "8 km/h"},
            {"k": "ip", "v": "IP ≥ 55"},
            {"k": "sensing", "v": "4+1D 双目相机 · 2 防水双目深度相机 · 10 超声波雷达"},
            {"k": "compute", "v": "NVIDIA K1020 + RK3588 + 16 GB"},
        ],
    },
    {
        "code": "minibus", "type_code": "AUTO-MINIBUS", "name": "无人小巴车",
        "model": "", "line": "autonomous", "category": "van",
        "product_line": "minibus", "seats": 6, "passenger_seats": 6,
        "specs": [
            {"k": "level", "v": "L4"},
            {"k": "seats", "v": 6},
            {"k": "sensing", "v": "自动避障 · 自动停靠"},
            {"k": "extras", "v": "无驾驶舱大视野窗 · 语音交互 · 多媒体导航"},
            {"k": "size", "v": None},
            {"k": "battery", "v": None},
            {"k": "range", "v": None},
        ],
    },
]

REGION = "TH"
CURRENCY = "THB"
PROTOCOL_PROFILE = "v1"  # 与现有车型字典取值一致（车端协议档，不对外暴露）


async def _upsert_type(session, v: dict[str, Any]) -> None:
    await session.execute(text("""
        INSERT INTO core.vehicle_type
            (type_code, name, product_line, category, seats, passenger_seats,
             power_type, protocol_profile, regions, specs, features, status)
        VALUES (:tc, :name, :pl, :cat, :seats, :pax, 'ev', :pp, :regions, '{}', :features, 'active')
        ON CONFLICT (type_code) DO UPDATE SET
            name = EXCLUDED.name, product_line = EXCLUDED.product_line,
            category = EXCLUDED.category, seats = EXCLUDED.seats,
            passenger_seats = EXCLUDED.passenger_seats,
            regions = EXCLUDED.regions, status = 'active'
    """), {"tc": v["type_code"], "name": v["name"], "pl": v["product_line"],
           "cat": v["category"], "seats": v["seats"], "pax": v["passenger_seats"],
           "pp": PROTOCOL_PROFILE, "regions": json.dumps([REGION]),
           "features": json.dumps([v["line"]])})


async def _upsert_product(session, v: dict[str, Any]) -> str:
    attrs = {
        "code": v["code"], "model": v.get("model") or "",
        "line": v["line"], "hero": bool(v.get("hero")),
        "image_kind": v.get("image_kind", "real"),
        "price_kind": "guideline",
        "specs": v["specs"],
    }
    row = (await session.execute(text("""
        SELECT id FROM commerce.product
         WHERE product_type = 'vehicle' AND attrs->>'code' = :code
    """), {"code": v["code"]})).first()
    params = {
        "name": v["name"], "cat": v["line"], "tc": v["type_code"],
        "msrp": MSRP_THB[v["code"]] * 100, "cur": CURRENCY, "region": REGION,
        "img": f"/vehicles/{v['code']}.jpg", "attrs": json.dumps(attrs),
    }
    if row is None:
        await session.execute(text("""
            INSERT INTO commerce.product
                (product_type, category, name, type_code, msrp, currency, region_id,
                 status, image_url, attrs)
            VALUES ('vehicle', :cat, :name, :tc, :msrp, :cur, :region, 'active', :img, :attrs)
        """), params)
        return "insert"
    await session.execute(text("""
        UPDATE commerce.product
           SET category = :cat, name = :name, type_code = :tc, msrp = :msrp,
               currency = :cur, region_id = :region, image_url = :img,
               attrs = :attrs, status = 'active'
         WHERE id = :id
    """), {**params, "id": int(row[0])})
    return "update"


async def run(dry_run: bool) -> None:
    if dry_run:
        for v in VEHICLES:
            print(json.dumps({"type_code": v["type_code"], "name": v["name"],
                              "msrp_thb": MSRP_THB[v["code"]],
                              "specs": len(v["specs"])}, ensure_ascii=False))
        return
    async with tx() as session:
        for v in VEHICLES:
            await _upsert_type(session, v)
            action = await _upsert_product(session, v)
            print(f"{action:6s} {v['type_code']:14s} {v['name']} "
                  f"指导价 ฿{MSRP_THB[v['code']]:,}")
        total = (await session.execute(text(
            "SELECT COUNT(*) FROM commerce.product WHERE product_type = 'vehicle'"))).scalar()
        print(f"\nvehicle products in catalog: {total}")
    await dispose_engine()


def main() -> None:
    ap = argparse.ArgumentParser(description="seed 12 个整车车型进车型字典与商城目录")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不写库")
    args = ap.parse_args()
    asyncio.run(run(args.dry_run))


if __name__ == "__main__":
    sys.exit(main())
