"""统一错误：后端只出 `code`，文案由前端按 locale 映射（Task-Server.md §10.4）。"""

from __future__ import annotations

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse


class KumaError(Exception):
    code = "internal_error"
    http_status = status.HTTP_500_INTERNAL_SERVER_ERROR

    def __init__(self, message: str = "", *, code: str | None = None,
                 detail: dict | None = None) -> None:
        super().__init__(message or self.code)
        self.message = message or self.code
        self.code = code or self.code          # 具体错误码可覆盖类别码（前端按 code 出文案）
        self.detail = detail or {}


class Unauthorized(KumaError):
    code = "unauthorized"
    http_status = status.HTTP_401_UNAUTHORIZED


class Forbidden(KumaError):
    code = "forbidden"
    http_status = status.HTTP_403_FORBIDDEN


class NotFound(KumaError):
    code = "not_found"
    http_status = status.HTTP_404_NOT_FOUND


class Conflict(KumaError):
    code = "conflict"
    http_status = status.HTTP_409_CONFLICT


class InvalidState(KumaError):
    """状态机不允许的迁移（services 层唯一裁决）。"""

    code = "invalid_state"
    http_status = status.HTTP_409_CONFLICT


class ValidationFailed(KumaError):
    code = "validation_failed"
    http_status = 422          # Starlette 已弃用 HTTP_422_UNPROCESSABLE_ENTITY 常量名


class VehicleOffline(KumaError):
    code = "vehicle_offline"
    http_status = status.HTTP_409_CONFLICT


class SafetyBlocked(KumaError):
    """下行控制安全闸门拦截（行车中断电等，Task-Server.md §9）。"""

    code = "safety_blocked"
    http_status = status.HTTP_423_LOCKED


class QuotaExceeded(KumaError):
    code = "quota_exceeded"
    http_status = status.HTTP_429_TOO_MANY_REQUESTS


def error_response(code: str, message: str, detail: dict | None = None,
                   http_status: int = 500) -> JSONResponse:
    return JSONResponse(status_code=http_status, content={"error": {"code": code,
                                                                   "message": message,
                                                                   "detail": detail or {}}})


def register_handlers(app: FastAPI) -> None:
    @app.exception_handler(KumaError)
    async def _kuma_error(_: Request, exc: KumaError) -> JSONResponse:
        return error_response(exc.code, exc.message, exc.detail, exc.http_status)
