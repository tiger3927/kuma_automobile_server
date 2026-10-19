"""structlog 配置：运行时日志只走 stdout（journald），**不进 PG**（Task-Plan.md §10-21）。"""

from __future__ import annotations

import logging
import sys

import structlog


def setup_logging(role: str, *, json_logs: bool = True) -> None:
    logging.basicConfig(stream=sys.stdout, level=logging.INFO, format="%(message)s")
    processors: list = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),  # 全链路 UTC
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    processors.append(
        structlog.processors.JSONRenderer() if json_logs else structlog.dev.ConsoleRenderer()
    )
    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
    structlog.contextvars.bind_contextvars(role=role)


def get_logger(name: str = "kuma", **kw):
    return structlog.get_logger(name, **kw)
