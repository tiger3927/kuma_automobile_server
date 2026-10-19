"""api 进程入口（uvicorn 单 worker；多 worker 前必须确认 pubsub 已接管，§12-7）。

生产由 systemd 启动：`uvicorn main:app --host 127.0.0.1 --port 8100 --workers 1`
"""

from __future__ import annotations

import uvicorn

from src.kuma.api import create_app
from src.kuma.core.config import get_settings

settings = get_settings()
settings.role = "api"

app = create_app(settings)


def run() -> None:
    uvicorn.run(
        "main:app",
        host="127.0.0.1",                 # 公网只经 nginx 反代（§5）
        port=settings.api_port,
        workers=1,
        proxy_headers=True,
        forwarded_allow_ips="127.0.0.1",
        log_config=None,                  # 用 structlog，不用 uvicorn 默认日志格式
        access_log=False,
    )


if __name__ == "__main__":
    run()
