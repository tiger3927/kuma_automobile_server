"""MQTT 一机一密认证 + topic ACL —— RabbitMQ `rabbitmq_auth_backend_http` 的对接点（§8.3/§8.4/§8.5）。

契约（官方 README + RabbitMQ 4.3.6 实测，T1 PoC）
--------------------------------------------------
插件用 **GET（默认）或 POST**（`auth_http.http_method = post`）请求配置里的四个 URL，
参数放在 **query string（GET）或 urlencoded body（POST）**——**不是自定义 header**：

| 端点 | 参数 |
|---|---|
| `user_path` | `username`、`password`（MQTT 连接还会带 `client_id`、`vhost`） |
| `vhost_path` | `username`、`vhost`、`ip` |
| `resource_path` | `username`、`vhost`、`resource`、`name`、`permission`（MQTT 还带 `client_id`） |
| `topic_path` | `username`、`vhost`、`resource=topic`、`name=<exchange>`、`permission=read/write`、`routing_key` |

响应必须是 **HTTP 200 + 纯文本 body**：`allow` / `allow <tags>` / `deny` / `deny <reason>`。
❌ 返回 4xx/5xx 或 JSON（本文件旧桩就是这么写的）插件不认，只会当成后端故障。

MQTT topic → routing key 的映射：插件把 `/` 换成 `.`，所以 ACL 判定必须用**点分**形式
（`kuma/<vin>/telemetry` → `kuma.<vin>.telemetry`）。

⚠️ 性能：`topic_path` 在**每次 publish** 都会被调一次 → 1000 台 @5s ≈ 200 次/秒打 api。
生产必须叠 `rabbitmq_auth_backend_cache`（镜像自带，见 `deploy/rabbitmq.conf` 注释）。
"""

from __future__ import annotations

from urllib.parse import parse_qs

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse
from starlette.requests import ClientDisconnect

from ..core.config import get_settings
from ..core.db import get_session_factory
from ..core.logging import get_logger
from ..core.security import averify_password
from ..models.core import VehicleCredential

log = get_logger("mqtt.auth")
router = APIRouter(tags=["internal"])

# 车端只允许发这四类上行（`mqtt/topics.py`），且只能在自己的 VIN 前缀下
PUBLISH_LEAVES = frozenset({"telemetry", "alarm", "cmd_ack", "heartbeat"})
# 车端只允许精确订阅自己的下行；`+` / `#` 通配一律拒（§8.5）
SUBSCRIBE_LEAVES = frozenset({"cmd"})


def _allow() -> PlainTextResponse:
    return PlainTextResponse("allow")


def _deny(reason: str) -> PlainTextResponse:
    return PlainTextResponse(f"deny {reason}")


async def _params(request: Request) -> dict[str, str]:
    """GET 走 query string、POST 走 urlencoded body，两种都收（插件按 `http_method` 决定）。"""
    params = {k: v for k, v in request.query_params.multi_items()}
    if request.method == "POST":
        try:
            body = await request.body()
        except ClientDisconnect:
            # 认证排队太久、客户端先断开的情况（2026-10-21 冒烟实测）：拿不到 body 就安静返回，
            # 别让它冒成 500 污染日志——连接已经死了，MQ 那边会自己重试。
            return params
        if body:
            for k, vals in parse_qs(body.decode("utf-8", "replace")).items():
                params[k] = vals[-1]
    return params


def to_routing_key(topic: str) -> str:
    """MQTT topic → RabbitMQ routing key（`/` → `.`）。"""
    return topic.replace("/", ".")


def topic_allowed(username: str, permission: str, routing_key: str) -> bool:
    """纯字符串判定，**不查库**——`topic_path` 每次 publish 都要调，必须便宜。"""
    if not username:
        return False
    rk = to_routing_key(routing_key)
    own = f"kuma.{username}."
    if permission == "write":
        if not rk.startswith(own):
            return False
        return rk[len(own):] in PUBLISH_LEAVES
    if permission == "read":
        return rk in {f"kuma.{username}.{leaf}" for leaf in SUBSCRIBE_LEAVES}
    return False


def resource_allowed(username: str, client_id: str, resource: str, name: str,
                     permission: str) -> bool:
    """只允许设备动**自己的**队列；别的资源一律拒。

    2026-10-20 PoC 实测到的真实行为（别再按文档想当然）：
      * 插件**先查 exchange 级权限**（`resource=exchange name=amq.topic permission=read|write`，
        请求里**没有 routing_key**），这一关过了**才会**去查 `topic_path` 做 routing key 级判定。
        把 amq.topic 在这里拒掉 = topic ACL 永远不会被调用（当时 `mqtt.topic.*` 日志计数为 0）。
        所以这里是**粗闸门**，细粒度交给 `topic_allowed`。
      * 真实队列名：订阅队列 `mqtt-subscription-<client_id>qos0`（QoS1 是 `qos1`）、
        会话队列 `session-<client_id>` / 临时会话随机名。`client_id` 是 MQTT 的 client id，
        **不等于用户名**（我们的 PoC 里 client id 是 `poc-<vin>-<hex>`）。
      * 别的 exchange、别人的队列、服务端队列（`kuma.cmd.down`）一律拒。
    """
    if not username:
        return False
    if resource == "exchange":
        # 粗闸门：只放行 MQTT 用的 amq.topic；能不能发/订哪个 topic 由 topic_path 决定
        return name == "amq.topic" and permission in ("read", "write")
    if resource != "queue":
        return False
    if name.startswith("kuma."):          # 服务端队列是 gateway（internal 用户）的事
        return False
    if client_id and (name == client_id or name.startswith(
            (f"session-{client_id}", f"mqtt-subscription-{client_id}", f"{client_id}-"))):
        return True
    # 临时/自动生成的队列名带随机后缀，别人猜不到，允许（跨连接无法复用）
    return name.startswith(("session-", "amq.gen-"))


async def verify_credential(username: str, password: str) -> bool:
    if not username or not password:
        return False
    async with get_session_factory()() as session:
        cred = await session.get(VehicleCredential, username)
    if cred is None or cred.status != "active":
        return False
    return await averify_password(password, cred.password_hash)


@router.post("/user")
@router.get("/user")
async def user_route(request: Request) -> PlainTextResponse:
    p = await _params(request)
    username, password = p.get("username", ""), p.get("password", "")
    if not await verify_credential(username, password):
        log.warn("mqtt.auth.denied", username=username, client_id=p.get("client_id", ""))
        return _deny("unknown user, revoked or bad password")
    log.info("mqtt.auth.ok", username=username, client_id=p.get("client_id", ""))
    return _allow()          # 不带 tags：设备账号不该拿到任何 management 权限


@router.post("/vhost")
@router.get("/vhost")
async def vhost_route(request: Request) -> PlainTextResponse:
    p = await _params(request)
    if p.get("username") and p.get("vhost") == get_settings().mqtt_vhost:
        return _allow()
    return _deny(f"vhost {p.get('vhost', '')!r} not allowed")


@router.post("/resource")
@router.get("/resource")
async def resource_route(request: Request) -> PlainTextResponse:
    p = await _params(request)
    if resource_allowed(p.get("username", ""), p.get("client_id", ""), p.get("resource", ""),
                        p.get("name", ""), p.get("permission", "")):
        return _allow()
    log.warn("mqtt.resource.denied", username=p.get("username", ""),
             resource=p.get("resource", ""), name=p.get("name", ""),
             permission=p.get("permission", ""), client_id=p.get("client_id", ""))
    return _deny("resource not owned by this client")


@router.post("/topic")
@router.get("/topic")
async def topic_route(request: Request) -> PlainTextResponse:
    p = await _params(request)
    username, permission, rk = p.get("username", ""), p.get("permission", ""), p.get("routing_key", "")
    if topic_allowed(username, permission, rk):
        return _allow()
    log.warn("mqtt.topic.denied", username=username, permission=permission, routing_key=rk)
    return _deny("topic not owned by this client")
