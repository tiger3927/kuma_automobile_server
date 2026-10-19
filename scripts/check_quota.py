"""地图配额与缓存检查（§10.2）。

    python scripts/check_quota.py
"""

from __future__ import annotations

import asyncio
import json

from src.kuma.core.config import get_settings
from src.kuma.core.redis_client import close
from src.kuma.maps import quota


async def run() -> None:
    s = get_settings()
    out = {"daily_quota": s.maps_daily_quota, "providers": {}}
    for name in ("google", "amap"):
        out["providers"][name] = {
            "used_today": await quota.used(name),
            "remaining": await quota.remaining(name),
            "key_configured": bool(getattr(s, f"{name}_key")),
        }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    await close()


if __name__ == "__main__":
    asyncio.run(run())
