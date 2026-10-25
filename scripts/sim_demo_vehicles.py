"""演示遥测：让库里已注册的车沿曼谷路线上报（不经 MQTT）。

**为什么不走 MQTT**：MQTT 是一机一密（`core.vehicle_credential`，口令是 argon2 哈希、
签发时打印一次就不可恢复），演示环境常常没有口令。本脚本复用**同一条入库管道**——
`TelemetryService.ingest`（补报窗口 → deadband → COPY 落库 → Redis 最新状态 → 开行程），
只把 MQTT 那一跳换成直调，并补上真实链路里由 `mqtt/inbound.py` 发的 `publish_vin`
（WS 推送不依赖落库，`Task-Server.md` §12-9）。

用法（爱商汇服务器，conda `kuma`）：
    cd /data/kuma_automobile_server && set -a && . deploy/runtime.env && set +a
    PYTHONPATH=. python scripts/sim_demo_vehicles.py --history-minutes 90 --seconds 180
    PYTHONPATH=. python scripts/sim_demo_vehicles.py --vins DEMO0000000000001 --seconds 0

三条注意：
1. **会开行程**（`telemetry_service._open_trips`）：真实上报同样会，这是口径不是副作用。
2. **只给 `core.vehicle` 里存在的 VIN 造数据**。`load_sim.py` 造的 `SIM…` VIN 不在主数据里，
   `fleet/live`（走 `core.vehicle` + Redis）与 `rep.fleet_snapshot`（join `core.vehicle`）
   都看不见它们——地图上一个点都不会有。
3. 它不校验 MQTT ACL、也不走单连接速率闸门（§8.7），**仅限演示/开发环境**。
"""

from __future__ import annotations

import argparse
import asyncio
import math
import random
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from src.kuma.core import redis_client
from src.kuma.core.db import dispose_engine, tx
from src.kuma.models.core import Vehicle
from src.kuma.realtime.broadcaster import publish_vin
from src.kuma.repositories.state_repo import StateRepo
from src.kuma.repositories.telemetry_repo import TelemetryPoint
from src.kuma.services.telemetry_service import TelemetryService

# 曼谷三条闭合路线（WGS-84：lat, lon）。选真实地标是为了让 OSM 底图上"车在路上"——
# 随机漂移的点一眼就是假数据，演示时无法判断是地图坏了还是数据坏了。
ROUTES: tuple[tuple[tuple[float, float], ...], ...] = (
    # 老城/暹罗内环：Siam → Chit Lom → Khao San → 老城东侧 → Siam
    ((13.7456, 100.5341), (13.7464, 100.5395), (13.7540, 100.5370), (13.7586, 100.4974),
     (13.7570, 100.5030), (13.7510, 100.5100), (13.7466, 100.5220)),
    # 是隆/沙吞：Silom → Sala Daeng → Lumpini 公园南 → Sathon → Rama IV → Silom
    ((13.7240, 100.5180), (13.7235, 100.5210), (13.7300, 100.5380), (13.7220, 100.5290),
     (13.7175, 100.5230), (13.7180, 100.5150)),
    # 阿索克一线：Asok → Nana → Phrom Phong → Thong Lo → Ekkamai → 回程
    ((13.7385, 100.5610), (13.7355, 100.5540), (13.7300, 100.5680), (13.7240, 100.5800),
     (13.7200, 100.5880), (13.7280, 100.5750), (13.7350, 100.5660)),
)


def _seg_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    """两点间近似距离（米）：经度按中点纬度收缩，否则路线长度算不对，车速就是假的。"""
    dlat = (b[0] - a[0]) * 111_000.0
    dlon = (b[1] - a[1]) * 111_000.0 * math.cos(math.radians((a[0] + b[0]) / 2.0))
    return math.hypot(dlat, dlon)


@dataclass(slots=True)
class Route:
    """闭合折线，按"沿线累计米数"取点。"""

    pts: tuple[tuple[float, float], ...]
    cum: list[float] = field(default_factory=list)
    length: float = 0.0

    def __post_init__(self) -> None:
        n = len(self.pts)
        self.cum = [0.0]
        for i in range(n):
            self.cum.append(self.cum[-1] + _seg_m(self.pts[i], self.pts[(i + 1) % n]))
        self.length = self.cum[-1]

    def at(self, dist_m: float) -> tuple[float, float]:
        n = len(self.pts)
        d = dist_m % self.length
        for i in range(n):
            if d <= self.cum[i + 1] or i == n - 1:
                seg = self.cum[i + 1] - self.cum[i]
                r = 0.0 if seg <= 0 else min(1.0, (d - self.cum[i]) / seg)
                a, b = self.pts[i], self.pts[(i + 1) % n]
                return (a[0] + (b[0] - a[0]) * r, a[1] + (b[1] - a[1]) * r)
        return self.pts[0]


@dataclass(slots=True)
class Sim:
    """一台车的模拟状态。"""

    vin: str
    route: Route
    dist_m: float
    odo_km: float
    soc: float
    seq: int = 0
    base_speed: float = 26.0

    def advance(self, dt_s: float, ts: datetime) -> TelemetryPoint:
        """推进 dt_s 秒并产出一个遥测点。

        速度带抖动，且约每 8 次停一下（红灯 / 上下客）：停车点会被 deadband 挡掉不落库，
        但仍刷新 Redis 状态——这是真实链路的既有行为，演示时应当看得见。
        """
        stopped = random.random() < 0.12
        speed = 0.0 if stopped else self.base_speed * random.uniform(0.7, 1.25)
        self.dist_m += speed / 3.6 * dt_s
        self.odo_km += speed / 3600.0 * dt_s
        self.soc = max(0.0, self.soc - speed / 3600.0 * 0.9)
        if self.soc < 18.0:          # 三轮车是换电：到下限不是"充电"，是换一块
            self.soc = random.uniform(88.0, 96.0)
        lat, lon = self.route.at(self.dist_m)
        self.seq += 1
        return TelemetryPoint(
            vin=self.vin, ts=ts, lat=round(lat, 6), lon=round(lon, 6),
            speed_kmh=round(speed, 1), soc=round(self.soc, 1), odo_km=round(self.odo_km, 1),
            seq=self.seq, proto="sim", recv_ts=datetime.now(UTC), quality=0,
        )


async def pick_vins(vins: list[str] | None) -> list[str]:
    async with tx() as session:
        if vins:
            rows = (await session.execute(
                select(Vehicle.vin).where(Vehicle.vin.in_(vins)).order_by(Vehicle.id)
            )).scalars().all()
            missing = set(vins) - set(rows)
            if missing:
                print(f"跳过不在 core.vehicle 的 VIN（主数据没有 = 地图不会显示）：{sorted(missing)}")
        else:
            rows = (await session.execute(
                select(Vehicle.vin).where(Vehicle.status == "in_service").order_by(Vehicle.id)
            )).scalars().all()
    return list(rows)


async def build_sims(vins: list[str], speed: float) -> list[Sim]:
    """里程表与 SOC 尽量从现有 Redis 状态续上——重复跑不该把 odo 跳回随机值。"""
    state = StateRepo()
    sims: list[Sim] = []
    for i, vin in enumerate(vins):
        route = Route(ROUTES[i % len(ROUTES)])
        prev = await state.get(vin) or {}
        sims.append(Sim(
            vin=vin, route=route,
            dist_m=random.uniform(0.0, route.length),
            odo_km=float(prev.get("odo_km") or random.uniform(1200.0, 24000.0)),
            soc=float(prev.get("soc") or random.uniform(45.0, 92.0)),
            base_speed=speed,
        ))
    return sims


async def backfill(sims: list[Sim], minutes: float, interval: float) -> None:
    """回填历史点：轨迹回放与里程报表一打开就有东西看。

    历史点**不 publish**：WS 推的是"现在"，把 90 分钟前的点灌给订阅者只会让地图乱跳。
    也**不填 `last`**：deadband 的"上一次"来自 gateway 的实时缓存，回填时填它会把该存的
    历史点挡掉，回放线变成断的。
    """
    now = datetime.now(UTC)
    steps = max(1, int(minutes * 60 / interval))
    for sim in sims:
        t = now - timedelta(minutes=minutes)
        pts: list[TelemetryPoint] = []
        for _ in range(steps):
            pts.append(sim.advance(interval, t))
            t += timedelta(seconds=interval)
        stats = await TelemetryService().ingest(pts)
        print(f"backfill vin={sim.vin} {stats.as_dict()}")


async def realtime(sims: list[Sim], seconds: float, interval: float) -> None:
    end = datetime.now(UTC) + timedelta(seconds=seconds)
    elapsed = 0.0
    while datetime.now(UTC) < end:
        ts = datetime.now(UTC)
        pts = [sim.advance(interval, ts) for sim in sims]
        for p in pts:
            # 真实链路里 last 由 gateway 解码时填充（省一次每点 Redis 往返）；
            # 这里等价地取一次，让 deadband 真的参与。
            p.last = await StateRepo().get(p.vin)
        stats = await TelemetryService().ingest(pts)
        for p in pts:
            await publish_vin(p.vin, "telemetry", {
                "ts": p.ts, "lat": p.lat, "lon": p.lon, "speed_kmh": p.speed_kmh,
                "soc": p.soc, "odo_km": p.odo_km,
            })
        print(f"t+{elapsed:6.0f}s {stats.as_dict()}")
        elapsed += interval
        await asyncio.sleep(interval)


async def run(args: argparse.Namespace) -> None:
    try:
        vins = await pick_vins(args.vins)
        if not vins:
            print("没有 in_service 的车可造数据（先跑 scripts/seed_demo.py）")
            return
        sims = await build_sims(vins, args.speed)
        print(f"车辆 {len(vins)} 台 · 实时间隔 {args.interval}s · 基准 {args.speed} km/h")
        for s in sims:
            print(f"  {s.vin} 一圈约 {s.route.length / 1000.0:.1f} km")

        if args.history_minutes > 0:
            await backfill(sims, args.history_minutes, args.history_interval)
        if args.seconds > 0:
            await realtime(sims, args.seconds, args.interval)
    finally:
        await redis_client.close()
        await dispose_engine()


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--vins", nargs="*", default=None, help="默认取全部 in_service 车")
    p.add_argument("--seconds", type=float, default=120.0, help="实时上报时长，0 = 只回填历史")
    p.add_argument("--interval", type=float, default=5.0, help="实时上报间隔（秒）")
    p.add_argument("--speed", type=float, default=26.0, help="基准时速 km/h")
    p.add_argument("--history-minutes", type=float, default=0.0, help="回填多少分钟历史")
    p.add_argument("--history-interval", type=float, default=15.0, help="历史点间隔（秒）")
    asyncio.run(run(p.parse_args()))


if __name__ == "__main__":
    main()
