"""模拟 N 台车 @5s 上报（§3 容量验证与 §12-5 批量写验证）。

用法：
    python scripts/load_sim.py --vehicles 200 --seconds 120 --url mqtt://127.0.0.1:1883 \
        --user kuma_mq --password <pw>
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
from datetime import UTC, datetime

import aiomqtt


async def run(args: argparse.Namespace) -> None:
    vins = [f"SIM{1000000000 + i}" for i in range(args.vehicles)]
    sent = 0
    async with aiomqtt.Client(args.url, username=args.user, password=args.password or None,
                              identifier="kuma-load-sim") as client:
        while sent < args.seconds * args.vehicles:
            batch = []
            for vin in vins:
                lat = 13.75 + random.uniform(-0.05, 0.05)
                lon = 100.50 + random.uniform(-0.05, 0.05)
                batch.append((vin, lat, lon))
            now = datetime.now(UTC)
            for vin, lat, lon in batch:
                payload = json.dumps({
                    "proto": "v1", "vin": vin, "ts": now.isoformat(),
                    "lat": round(lat, 6), "lon": round(lon, 6),
                    "spd": round(random.uniform(0, 60), 1),
                    "soc": round(random.uniform(20, 100), 1),
                    "odo": round(random.uniform(1000, 20000), 1),
                    "seq": sent,
                })
                await client.publish(f"kuma/{vin}/telemetry", payload=payload, qos=0)
                sent += 1
            print(f"sent={sent}")
            await asyncio.sleep(args.interval)
    print(f"DONE sent={sent}")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--vehicles", type=int, default=200)
    p.add_argument("--seconds", type=int, default=60)
    p.add_argument("--interval", type=float, default=5.0)
    p.add_argument("--url", default="mqtt://127.0.0.1:1883")
    p.add_argument("--user", default="kuma_mq")
    p.add_argument("--password", default="")
    asyncio.run(run(p.parse_args()))


if __name__ == "__main__":
    main()
