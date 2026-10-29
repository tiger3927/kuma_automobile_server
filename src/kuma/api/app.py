"""FastAPI 应用工厂（api 进程）。

`/healthz` 是 M0/M1 验收项：pg / redis / mq 三项全绿才算健康。
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import ORJSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import Response
from starlette.types import Scope

from ..core.bus import get_bus
from ..core.config import Settings, get_settings
from ..core.db import healthcheck as pg_health
from ..core.exceptions import register_handlers
from ..core.lifespan import lifespan
from ..core.middleware import RateLimit, RequestContext
from ..core.redis_client import healthcheck as redis_health
from ..mqtt import auth as mqtt_auth
from ..realtime import events_bridge
from ..realtime.manager import manager
from .v1 import api_router


class SPAStaticFiles(StaticFiles):
    """前端产物（Vue history 路由）的静态挂载。

    `StaticFiles(html=True)` 只会在"请求目录"时补 index.html；`/admin/fleet` 这种深链
    在磁盘上不存在，刷新一下就 404 —— 所以 404 要回 index.html 让前端路由接管。

    **`assets/` 下的 404 不兜底**：那说明产物本身缺文件（构建或上传出问题了），
    回一份 HTML 只会把问题变成"白屏 + JS 语法错"，比 404 更难查。
    """

    async def get_response(self, path: str, scope: Scope) -> Response:
        # 两个坑，都是线上实测出来的：
        # 1) StaticFiles 找不到文件时是 **raise HTTPException(404)**，不是返回 404 响应
        #    ——判 `resp.status_code` 永远不会命中，必须 catch。
        # 2) 它抛的是 **starlette** 的 HTTPException；`fastapi.HTTPException` 是它的**子类**，
        #    `except fastapi.HTTPException` 抓不到父类实例（线上 /admin/fleet 因此仍 404）。
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code == 404 and not path.startswith("assets/"):
                return await super().get_response("index.html", scope)
            raise


def create_app(settings: Settings | None = None) -> FastAPI:
    s = settings or get_settings()

    async def _startup() -> None:
        await manager.start()
        # C1：业务事件（outbox → kuma.events）→ Redis pubsub → WS。
        # 必须在 bus 已连上之后（lifespan 里 connect 先于 on_startup）。
        await events_bridge.start()

    async def _shutdown() -> None:
        await manager.stop()          # 先停 pubsub（§12-4）

    app = FastAPI(
        title="kuma 车联网服务", version="0.1.0",
        default_response_class=ORJSONResponse,
        docs_url="/docs" if not s.is_prod else None,
        lifespan=lambda a: lifespan(a, s, on_startup=_startup, on_shutdown=_shutdown),
    )
    app.add_middleware(RequestContext)
    # 规则按**插入顺序取第一个前缀匹配**（`core/middleware.py:48` 的 `next(...)`），
    # 所以更具体的 `/api/v1/public/leads` 必须排在 `/api/v1/public/` **前面**，否则 10/min 永不生效。
    # 公开**写**端点要比公开读端点严：120/min/IP 对意向表单的刷量太松（`Task-Server.md` §18.3）。
    app.add_middleware(RateLimit, rules={"/api/v1/public/leads": 10, "/api/v1/public/": 120})
    register_handlers(app)

    app.include_router(api_router, prefix="/api/v1")

    if s.mq_auth_token:
        # RabbitMQ `rabbitmq_auth_backend_http` 的回调入口（T1，见 mqtt/auth.py）。
        # 路径里带随机段：8100 被 nginx 全量反代到公网，不带 token 的调用只会拿到 404，
        # 这个端点不会被人当"口令探针"枚举。token 只落在 root-only 的 rabbitmq.conf 与 runtime.env。
        # 故意不进 RateLimit 规则：topic 权限每次 publish 都要判一次（1000 台 @5s ≈ 200 次/秒）。
        app.include_router(mqtt_auth.router, prefix=f"/internal/mqtt-auth/{s.mq_auth_token}")

    @app.get("/healthz")
    async def healthz() -> dict:
        pg = await pg_health()
        redis = await redis_health()
        mq = {"status": "ok"} if get_bus().is_connected() else {"status": "error"}
        status = "ok" if all(x["status"] == "ok" for x in (pg, redis, mq)) else "degraded"
        return {"status": status, "role": s.role, "pg": pg, "redis": redis, "mq": mq}

    @app.get("/metrics")
    async def metrics() -> dict:
        from ..core.redis_client import get_redis

        return {"role": s.role, "ws_connections": len(manager._by_vin),
                "redis_ping": (await get_redis().ping()),
                # C1：事件桥的投递计数（0 且 outbox 有行 = 桥没接上）
                **events_bridge.counters(),
                # C9：连接闸门拒绝数（持续增长 = 设备侧退避没生效或在被攻击）
                "mqtt_connect_denied": mqtt_auth.connect_denied}

    # ── 前端产物（`web/admin` 的 build 结果，`base: "/admin/"`）────────────────
    # `web/` 不进 git，产物是传上去的，所以目录在仓库外（`KUMA_ADMIN_DIST`）。
    # 目录或 index.html 不存在就**干脆不挂**：本机开发走 vite dev（5173），
    # 缺产物不该让后端起不来。
    # 挂在这里而不是改 nginx：nginx 由宝塔管，它的配置注释自己写着
    # "改坏了 443 的 API 一起挂"——静态由后端服务，反代一行都不用动。
    if s.admin_dist and (Path(s.admin_dist) / "index.html").is_file():

        # `/admin`（无尾斜杠）必须自己处理：`Mount` 的正则只匹配 `/admin/...`，所以 `/admin`
        # 谁都不匹配，落到 Starlette Router 的 redirect_slashes → 307 到它用 `scope["server"]`
        # 拼出来的**绝对** URL。线上实测 `https://kuma.my-gun.top/admin` 的
        # `Location: http://127.0.0.1/admin/`，浏览器直接被踢出站点
        # （nginx 没传 X-Forwarded-Host，手动带上才拼对）。
        # 这里给**相对** Location，浏览器按当前 origin 解析，就不依赖反代传什么头。
        async def admin_root(_request: Request) -> RedirectResponse:
            return RedirectResponse("/admin/", status_code=302)

        app.add_route("/admin", admin_root, methods=["GET"])
        app.mount("/admin", SPAStaticFiles(directory=s.admin_dist, html=True), name="admin")

    # ── 用户端 guest（`web/guest` 的 build 结果，`base: "/app/"`）───────────────
    # 必须排在 portal 的 `/` **之前**：Starlette 按注册顺序匹配，portal 是兜底。
    # 顺序错了 `/app/xxx` 会被 portal 接住，而 portal 的路由把未知路径 redirect 回 `/`
    # （`web/portal/src/main.ts` 的 catch-all）——线上表象是"访问 /app 被弹回官网首页"。
    # `/app`（无尾斜杠）那个坑与 `/admin` 完全同构，见上面 `admin_root` 的注释。
    if s.guest_dist and (Path(s.guest_dist) / "index.html").is_file():

        async def guest_root(_request: Request) -> RedirectResponse:
            return RedirectResponse("/app/", status_code=302)

        app.add_route("/app", guest_root, methods=["GET"])
        app.mount("/app", SPAStaticFiles(directory=s.guest_dist, html=True), name="guest")

    # ── 官网 portal（`web/portal` 的 build 结果，`base: "/"`）──────────────────
    # 同样"缺产物就不挂"。挂 "/" 必须放**最后**：Starlette 按注册顺序匹配，
    # /api/v1、/admin、/app、/internal、/healthz、/metrics 先命中，其余才落 portal；
    # 深链刷新由 SPAStaticFiles 回 index.html（portal 自身路由再把未知路径 redirect "/"）。
    if s.portal_dist and (Path(s.portal_dist) / "index.html").is_file():
        app.mount("/", SPAStaticFiles(directory=s.portal_dist, html=True), name="portal")

    return app
