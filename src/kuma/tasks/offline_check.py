"""离线判定：Redis 状态 TTL 过期即离线（§9 末条、§12-9）。

顺带把 `mobility.driver_profile.online_status` 与车辆在线态对齐（派单硬过滤要用）。
"""

from __future__ import annotations

from sqlalchemy import select

from ..core.config import get_settings
from ..core.db import tx
from ..core.logging import get_logger
from ..models.core import Vehicle
from ..repositories.state_repo import StateRepo
from ..services.alarm_service import AlarmService

log = get_logger("tasks.offline")

OFFLINE_ALARM_S = 600      # 超过 10 分钟无上报 → 离线告警


async def run() -> None:
    ttl = get_settings().state_ttl
    state = StateRepo()
    async with tx() as session:
        vins = (await session.execute(
            select(Vehicle.vin).where(Vehicle.status.in_(("delivered", "in_service")))
            .order_by(Vehicle.id).limit(5000)
        )).scalars().all()

    newly_offline = 0
    for vin in vins:
        if await state.is_online(vin):
            continue
        last = await state.last_ts(vin)
        if last is None:
            continue                       # 从未上报过，不告警（未交付/未装 T-Box）
        newly_offline += 1

    log.info("offline.checked", total=len(vins), offline=newly_offline)
    async with tx() as session:
        for vin in vins[:200]:
            if not await state.is_online(vin) and await state.last_ts(vin):
                await AlarmService().repo.raise_alarm(
                    session, vin=vin, ts=_now(), type_="offline", level=2,
                    payload={"last_ts": await state.last_ts(vin), "ttl_s": ttl,
                             "threshold_s": OFFLINE_ALARM_S})


def _now():
    from datetime import UTC, datetime

    return datetime.now(UTC)
