"""入站去重与补报窗口（§8.8）。

两层：
1. 进程内滑动窗口（省一次 DB 往返，挡掉瞬时重复投递）；
2. 数据库 `(vin, ts)` 主键 + `ON CONFLICT DO NOTHING`（最终一致，跨进程/重启有效）。
"""

from __future__ import annotations

import time
from collections import OrderedDict
from datetime import UTC, datetime, timedelta

from ..core.config import get_settings
from ..repositories.telemetry_repo import TelemetryPoint

MAX_KEYS = 200_000


class DedupWindow:
    def __init__(self, *, max_keys: int = MAX_KEYS, ttl_s: int = 600) -> None:
        self._seen: OrderedDict[tuple[str, float], float] = OrderedDict()
        self._max_keys = max_keys
        self._ttl_s = ttl_s

    def filter(self, points: list[TelemetryPoint]) -> tuple[list[TelemetryPoint], int]:
        now = time.monotonic()
        kept: list[TelemetryPoint] = []
        dup = 0
        for p in points:
            key = (p.vin, p.ts.timestamp())
            last = self._seen.get(key)
            if last is not None and now - last < self._ttl_s:
                dup += 1
                continue
            self._seen[key] = now
            kept.append(p)
        while len(self._seen) > self._max_keys:
            self._seen.popitem(last=False)
        return kept, dup

    def stale(self, points: list[TelemetryPoint]) -> tuple[list[TelemetryPoint], int]:
        """超过补报窗口（默认 72h）的点直接丢弃 + 计数。"""
        horizon = datetime.now(UTC) - timedelta(hours=get_settings().backfill_max_hours)
        fresh = [p for p in points if p.ts >= horizon]
        return fresh, len(points) - len(fresh)
