"""实时广播：gateway → Redis pub/sub → api 进程 WS 推送（§12-7：api 多 worker 前必须走 pubsub）。

channel: `kuma:rt:{vin}`（与 orio 的 key 空间隔离，§4.1）。
"""

from __future__ import annotations

import json
from typing import Any

from ..core.redis_client import get_redis, kkey

CHANNEL_PREFIX = kkey("rt")


def channel_for_vin(vin: str) -> str:
    return f"{CHANNEL_PREFIX}:{vin}"


async def publish_vin(vin: str, event: str, data: dict[str, Any]) -> None:
    payload = json.dumps({"event": event, "data": data}, default=str)
    await get_redis().publish(channel_for_vin(vin), payload)


async def publish_user(account_id: int, event: str, data: dict[str, Any]) -> None:
    payload = json.dumps({"event": event, "data": data}, default=str)
    await get_redis().publish(kkey("rt", "user", str(account_id)), payload)
