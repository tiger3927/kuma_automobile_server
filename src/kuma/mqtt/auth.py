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

from datetime import UTC, datetime
from urllib.parse import parse_qs

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse
from starlette.requests import ClientDisconnect

from ..core.config import get_settings
from ..core.db import get_session_factory
from ..core.logging import get_logger
from ..core.redis_client import get_redis, kkey
from ..core.security import averify_password
from ..models.core import VehicleCredential

log = get_logger("mqtt.auth")
router = APIRouter(tags=["internal"])

# 连接闸门拒绝总数（/metrics 暴露）。风暴时它是"设备侧退避有没有生效"的直接读数。
connect_denied = 0

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


async def _connect_gate(username: str) -> str | None:
    """连接速率闸门（C9，2026-10-22 决策）：返回 None 放行，否则返回 deny 理由。

    **为什么放在口令校验之前**：`verify_credential` 里是 argon2id 验证（几十毫秒 CPU），
    连接风暴时先验口令等于让风暴直接打满 api 的 CPU——闸门必须先于昂贵操作。

    **为什么服务端闸门 + 设备侧退避都要有**：网关重启后 100 台设备同时重连是必然事件，
    服务端只靠"扛住"没有意义（RabbitMQ 没有连接速率配置项），必须让设备退避；
    而设备退避的口径要靠服务端拒绝来反馈（MQTT 5 没有服务端退避指令，`deny` 是唯一手段）。

    窗口用**分钟桶**而不是 TTL 计数：TTL 计数在 incr 与 expire 之间崩溃会留下无 TTL 的键，
    把这台设备永久锁死；桶键天然过期，最坏只是多一个没人读的键。
    """
    s = get_settings()
    r = get_redis()
    bucket = int(datetime.now(UTC).timestamp() // 60)
    key_vin = kkey("mqconn", username, str(bucket))
    key_all = kkey("mqconn", "_all", str(bucket))
    pipe = r.pipeline()
    pipe.incr(key_vin)
    pipe.incr(key_all)
    pipe.expire(key_vin, 180)
    pipe.expire(key_all, 180)
    results = await pipe.execute()
    per_vin, total = int(results[0]), int(results[1])
    if per_vin > s.mq_connect_per_vin_per_min:
        return "reconnect too fast"
    if total > s.mq_connect_burst_per_min:
        return "connect storm throttled"
    return None


@router.post("/user")
@router.get("/user")
async def user_route(request: Request) -> PlainTextResponse:
    p = await _params(request)
    username, password = p.get("username", ""), p.get("password", "")
    reason = await _connect_gate(username)
    if reason is not None:
        global connect_denied
        connect_denied += 1
        if connect_denied % 20 == 1:      # 风暴时这条日志会炸，留痕本身也要限频
            log.warn("mqtt.auth.throttled", username=username, reason=reason,
                     total_denied=connect_denied)
        return _deny(reason)
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
