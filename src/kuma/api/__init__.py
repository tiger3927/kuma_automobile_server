"""api 层：只做 HTTP/WS 契约与依赖注入，不写 SQL、不实现状态机。"""

from .app import create_app

__all__ = ["create_app"]
