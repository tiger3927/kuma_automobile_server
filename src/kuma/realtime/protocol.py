"""WS 客户端协议（前端契约）。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class SubscribeIn(BaseModel):
    action: Literal["subscribe", "unsubscribe", "ping"] = "subscribe"
    vins: list[str] = Field(default_factory=list, max_length=200)


class Event(BaseModel):
    event: str
    data: dict
