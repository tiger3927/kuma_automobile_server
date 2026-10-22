"""离线判定：Redis 状态 TTL 过期即离线（§9 末条、§12-9）。

2026-10-22 补两件事：
1. 阈值走 `biz.alarm_rule`（`offline_s`），并带上同类抑制窗口——此前每 120s 一轮，
   一辆长期离线的车会被反复 raise（靠 `UNIQUE(vin,ts,type)` 挡不住，因为 `ts` 每轮都不同）；
2. **车恢复上报就自动了结**（`auto_closed`）。否则离线告警会永远挂在 open，
   运营只能手动 ack，告警列表只增不减。
"""

from __future__ import annotations

from sqlalchemy import select

from ..core.config import get_settings
from ..core.db import tx
from ..core.logging import get_logger
from ..models.core import Vehicle
from ..repositories.alarm_repo import AlarmRepo
from ..repositories.state_repo import StateRepo
from .alarm_rules import _pick, _rules

log = get_logger("tasks.offline")

OFFLINE_ALARM_S = 600      # fallback：规则表缺失时按 10 分钟无上报告警


async def run() -> None:
    ttl = get_settings().state_ttl
    state = StateRepo()

    async with tx() as session:
        vins = (await session.execute(
            select(Vehicle.vin).where(Vehicle.status.in_(("delivered", "in_service")))
            .order_by(Vehicle.id).limit(5000)
        )).scalars().all()
        rules = await _rules(session, "offline")

    threshold_s = float(_pick(rules, None, "offline_s") or OFFLINE_ALARM_S)
    level = int(_pick(rules, None, "level") or 2)
    dedupe_s = int(_pick(rules, None, "dedupe_s") or 1800)

    online: list[str] = []
    offline: list[tuple[str, str]] = []
    for vin in vins:
        if await state.is_online(vin):
            online.append(vin)
            continue
        last = await state.last_ts(vin)
        if last is None:
            continue                       # 从未上报过，不告警（未交付/未装 T-Box）
        offline.append((vin, last))

    log.info("offline.checked", total=len(vins), offline=len(offline), online=len(online))

    raised = 0
    async with tx() as session:
        repo = AlarmRepo()
        for vin, last in offline[:500]:     # 单轮上限：离线风暴时不要把事务撑爆
            if await repo.raise_alarm(
                session, vin=vin, ts=_now(), type_="offline", level=level,
                payload={"last_ts": last, "ttl_s": ttl, "threshold_s": threshold_s},
                dedupe_window_s=dedupe_s,
            ):
                raised += 1
        closed = await repo.auto_close(session, vins=online, type_="offline")

    log.info("offline.raised", count=raised, auto_closed=closed)


def _now():
    from datetime import UTC, datetime

    return datetime.now(UTC)
