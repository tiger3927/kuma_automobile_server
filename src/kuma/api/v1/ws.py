"""WebSocket 入口：`/ws/kuma?token=<access_token>`。

WS 无法带 Authorization 头，故用 query token；**同样走身份解析与权限判定**，
订阅的 vin 必须落在 principal 的 scope 内（越权直接关闭连接）。
"""

from __future__ import annotations

from contextlib import suppress

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from sqlalchemy import select

from ...core.db import get_session_factory
from ...core.deps import resolve_principal
from ...core.exceptions import Forbidden
from ...core.logging import get_logger
from ...core.scope import Principal
from ...models.core import Vehicle
from ...realtime.manager import manager
from ...realtime.protocol import SubscribeIn
from ...repositories.base import vehicle_scope

log = get_logger("ws")
router = APIRouter()


async def _visible_vins(principal: Principal, vins: list[str]) -> set[str]:
    async with get_session_factory()() as session:
        rows = (await session.execute(
            select(Vehicle.vin).where(Vehicle.vin.in_(vins),
                                      Vehicle.id.in_(vehicle_scope(principal)))
        )).scalars().all()
    return set(rows)


@router.websocket("/ws/kuma")
async def ws_kuma(ws: WebSocket, token: str = Query(...)) -> None:
    await ws.accept()
    user_id: int | None = None
    subscribed: set[str] = set()
    try:
        async with get_session_factory()() as session:
            principal = await resolve_principal(session, token)
        principal.require("vehicle_live", "read")
        user_id = principal.account_id

        await ws.send_json({"event": "ready", "data": {
            "account_id": principal.account_id, "active_identity": principal.active_identity,
            "scope": principal.scope_kind()}})

        while True:
            msg = SubscribeIn.model_validate(await ws.receive_json())
            if msg.action == "ping":
                await ws.send_json({"event": "pong", "data": {}})
                continue
            if not msg.vins:
                continue

            allowed = await _visible_vins(principal, msg.vins)
            if allowed != set(msg.vins):
                raise Forbidden("vin_out_of_scope")

            await manager.unsubscribe(ws)
            if msg.action == "subscribe":
                subscribed |= allowed
            else:
                subscribed -= allowed
            await manager.subscribe(ws, vins=list(subscribed), user_id=user_id)
            await ws.send_json({"event": msg.action + "d", "data": {"vins": sorted(subscribed)}})
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        log.warn("ws.closed", err=type(exc).__name__, detail=str(exc))
        with suppress(Exception):        # 连接可能已经断了，关不掉不算错
            await ws.close(code=4403)
    finally:
        await manager.unsubscribe(ws)
