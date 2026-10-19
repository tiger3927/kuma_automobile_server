"""MQTT 一机一密认证后端（RabbitMQ HTTP auth backend 的对接点，§8.3/§8.4）。

RabbitMQ 配置示例（rabbitmq.conf）：
    auth_backends.1 = http
    auth_http.http_host = 127.0.0.1:8100
    auth_http.path_prefix = /internal/mqtt-auth
    auth_http.user_path = /user
    auth_http.vhost_path  = /vhost
    auth_http.topic_path  = /topic
**PoC 结论待 M0 验证**（RabbitMQ 4.x 的 MQTT + HTTP auth 是否满足一机一密 + ACL）；
不满足则生产换 EMQX（Task-Server.md R2）。
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from ..core.db import get_session_factory
from ..core.logging import get_logger
from ..core.security import verify_password
from ..models.core import VehicleCredential

log = get_logger("mqtt.auth")
router = APIRouter(prefix="/internal/mqtt-auth", tags=["internal"])

# 车端只允许自己的 topic（§8.5）
WRITE_PATTERN = "kuma/{vin}/+"
READ_PATTERN = "kuma/{vin}/cmd"


async def verify_credential(username: str, password: str) -> bool:
    async with get_session_factory()() as session:
        cred = await session.get(VehicleCredential, username)
    if cred is None or cred.status != "active":
        return False
    return verify_password(password, cred.password_hash)


@router.post("/user")
async def user_route(request: Request) -> dict:
    username = request.headers.get("x-rabbitmq-user", "")
    password = request.headers.get("x-rabbitmq-pass", "")
    if not await verify_credential(username, password):
        log.warn("mqtt.auth.denied", username=username)
        return {"status": 400}
    return {"status": 200, "expect": "topic"}


@router.post("/vhost")
async def vhost_route(request: Request) -> dict:
    username = request.headers.get("x-rabbitmq-user", "")
    if not username:
        return {"status": 400}
    return {"status": 200, "vhost": "/kuma"}


@router.post("/topic")
async def topic_route(request: Request) -> dict:
    username = request.headers.get("x-rabbitmq-user", "")
    topic = request.headers.get("x-rabbitmq-topic", "")
    reading = request.headers.get("x-rabbitmq-reading", "write")
    if not username:
        return {"status": 400}
    expected_read = READ_PATTERN.format(vin=username)
    expected_write_prefix = f"kuma/{username}/"
    if reading == "read":
        ok = topic == expected_read or topic == f"kuma/{username}/cmd_ack"
    else:
        ok = topic.startswith(expected_write_prefix)
    return {"status": 200 if ok else 400}
