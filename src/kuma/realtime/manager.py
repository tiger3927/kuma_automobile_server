"""WS 连接管理：订阅 `kuma:rt:*` 并按 vin / user 分发（M4 验收：P95 < 500ms）。

api 进程启动时拉起一个 pubsub 订阅任务，收到消息后扇出给本进程内订阅者；
多 worker 时每个进程各自订阅（Redis pubsub 天然广播）。
"""

from __future__ import annotations

import asyncio
import contextlib
from collections import defaultdict

from fastapi import WebSocket

from ..core.logging import get_logger
from ..core.redis_client import get_redis
from .broadcaster import CHANNEL_PREFIX

log = get_logger("realtime")


class ConnectionManager:
    def __init__(self) -> None:
        self._by_vin: dict[str, set[WebSocket]] = defaultdict(set)
        self._by_user: dict[int, set[WebSocket]] = defaultdict(set)
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._stopped = asyncio.Event()

    async def subscribe(self, ws: WebSocket, *, vins: list[str], user_id: int | None) -> None:
        async with self._lock:
            for vin in vins:
                self._by_vin[vin].add(ws)
            if user_id is not None:
                self._by_user[user_id].add(ws)

    async def unsubscribe(self, ws: WebSocket) -> None:
        async with self._lock:
            for vins in list(self._by_vin.values()):
                vins.discard(ws)
            for users in list(self._by_user.values()):
                users.discard(ws)

    async def send_to_ws(self, ws: WebSocket, payload: str) -> None:
        try:
            await ws.send_text(payload)
        except Exception:
            await self.unsubscribe(ws)

    # ── pubsub 桥 ───────────────────────────────────────────────
    async def start(self) -> None:
        if self._task is not None:
            return
        self._stopped.clear()
        self._task = asyncio.create_task(self._pump(), name="rt-pubsub")

    async def stop(self) -> None:
        self._stopped.set()
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _pump(self) -> None:
        pubsub = get_redis().pubsub()
        await pubsub.psubscribe(f"{CHANNEL_PREFIX}:*")   # 不 await 就是空订阅，一条也收不到
        try:
            while not self._stopped.is_set():
                msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if not msg:
                    continue
                await self._fanout(msg["channel"], msg["data"])
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("realtime.pump_failed", err=str(exc))
        finally:
            with contextlib.suppress(Exception):
                await pubsub.aclose()

    async def _fanout(self, channel: str, data: str) -> None:
        """channel 形如 `kuma:rt:{vin}` 或 `kuma:rt:user:{account_id}`。"""
        tail = channel[len(CHANNEL_PREFIX) + 1:]
        if tail.startswith("user:"):
            account_id = int(tail.split(":", 1)[1])
            targets = list(self._by_user.get(account_id, set()))
        else:
            targets = list(self._by_vin.get(tail, set()))
        for ws in targets:
            await self.send_to_ws(ws, data)


manager = ConnectionManager()
