"""下行：AMQP `kuma.cmd.down` → MQTT `kuma/{vin}/cmd`。

离线车不重复投递（gateway 消费时再查一次 Redis 状态，§9 末条）。
"""

from __future__ import annotations

import asyncio

import aiomqtt

from ..core.db import tx
from ..core.logging import get_logger
from ..core.redis_client import acquire_lock, release_lock
from ..repositories.state_repo import StateRepo
from ..services.command_service import CommandService
from .codecs import encode_cmd

log = get_logger("mqtt.outbound")


class Outbound:
    def __init__(self, mqtt_client: aiomqtt.Client) -> None:
        self._mqtt = mqtt_client
        self._state = StateRepo()
        self._svc = CommandService()

    async def handle(self, payload: dict) -> None:
        cmd_id = payload.get("cmd_id")
        vin = payload.get("vin")
        if not cmd_id or not vin:
            log.warn("outbound.bad_payload", payload=str(payload)[:200])
            return
        if not await self._state.is_online(vin):
            async with tx() as session:
                cmd = await self._svc.repo.get(session, cmd_id)
                if cmd is not None and cmd.status == "sent":
                    await self._svc.repo.transition(session, cmd, "timeout")
                    await self._svc.repo.append_audit(
                        session, cmd_id=cmd_id, attempt=cmd.attempt, event="drop_offline",
                        actor_account_id=None, detail={"reason": "vehicle_offline"})
            log.info("outbound.dropped_offline", cmd_id=cmd_id, vin=vin)
            return

        body = encode_cmd({**payload, "proto": "v1"})
        await self._mqtt.publish(f"kuma/{vin}/cmd", payload=body, qos=1, retain=False)
        log.info("outbound.published", cmd_id=cmd_id, vin=vin, attempt=payload.get("attempt", 0))


async def retry_sweep() -> None:
    """worker 侧超时重投（gateway 也跑一份，保证单实例时不依赖 worker）。"""
    token = await acquire_lock("cmd_timeout_sweep", ttl=60)
    if not token:
        return
    try:
        from sqlalchemy import select

        from ..models.biz import Command

        async with tx() as session:
            stale = (await session.execute(
                select(Command).where(Command.status == "sent",
                                      Command.sent_at < _minutes_ago(1))
            )).scalars().all()
            for cmd in stale:
                await CommandService().mark_timeout(session, cmd)
    finally:
        await release_lock(token)


def _minutes_ago(n: int):
    from datetime import UTC, datetime, timedelta

    return datetime.now(UTC) - timedelta(minutes=n)


async def loop_retry(interval_s: int = 30) -> None:
    while True:
        try:
            await retry_sweep()
        except Exception as exc:
            log.error("outbound.retry_sweep_failed", err=str(exc))
        await asyncio.sleep(interval_s)
