"""中间件：请求 id / 访问日志 / 简单限流（官网等公开端点必须限流，Task-Plan.md §9.7）。"""

from __future__ import annotations

import time
import uuid

import structlog
from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from .deps import client_ip
from .exceptions import QuotaExceeded, error_response

log = structlog.get_logger("http")


class RequestContext(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
        request.state.request_id = rid
        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            log.exception("http.unhandled", path=request.url.path, rid=rid)
            raise
        cost_ms = round((time.perf_counter() - start) * 1000, 1)
        response.headers["x-request-id"] = rid
        if request.url.path not in ("/healthz", "/metrics"):
            log.info("http.access", method=request.method, path=request.url.path,
                     status=response.status_code, cost_ms=cost_ms, rid=rid)
        return response


class RateLimit(BaseHTTPMiddleware):
    """进程内滑动窗口限流（单实例够用；跨实例限流在 nginx）。

    `per_minute` 按路径前缀配置；公开端点（/api/v1/public/*）必须显式配置。
    """

    def __init__(self, app, *, rules: dict[str, int] | None = None) -> None:
        super().__init__(app)
        self.rules = rules or {"/api/v1/public/": 120}
        self._hits: dict[str, list[float]] = {}

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        prefix = next((p for p in self.rules if request.url.path.startswith(p)), None)
        if prefix is None:
            return await call_next(request)
        now = time.time()
        # 8100 只绑 127.0.0.1、nginx 是唯一入口 → 必须按 X-Forwarded-For 分桶，
        # 否则全站访客共用一个 120/min 桶（公开端点等于对第二个访客就 429）
        key = f"{prefix}|{client_ip(request)}"
        window = [t for t in self._hits.get(key, []) if now - t < 60]
        if len(window) >= self.rules[prefix]:
            # 中间件跑在 ExceptionMiddleware **外面**：这里 raise QuotaExceeded 到不了 register_handlers，
            # 客户端会收到 500 且丢掉 {"error":{...}} envelope → 必须直接出响应
            exc = QuotaExceeded("rate_limited", detail={"retry_after_s": 60})
            log.warning("http.rate_limited", path=request.url.path, key=key,
                        limit=self.rules[prefix])
            resp = error_response(exc.code, exc.message, exc.detail, exc.http_status)
            resp.headers["retry-after"] = "60"
            return resp
        window.append(now)
        self._hits[key] = window
        if len(self._hits) > 10_000:      # 防止内存无界增长
            self._hits = {k: v for k, v in self._hits.items() if now - (v[-1] if v else 0) < 120}
        return await call_next(request)
