"""分页与时间窗约束（512MB PG 下轨迹类查询必须限窗，Task-DataBase.md §8）。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from .config import get_settings
from .exceptions import ValidationFailed


@dataclass(slots=True)
class PageParams:
    limit: int = 20
    offset: int = 0

    def __post_init__(self) -> None:
        self.limit = max(1, min(self.limit, 200))
        self.offset = max(0, self.offset)


@dataclass(slots=True)
class Page[T]:
    items: list[T]
    limit: int
    offset: int
    total: int | None = None

    def as_dict(self) -> dict:
        return {"items": self.items, "limit": self.limit, "offset": self.offset,
                "total": self.total}


def check_time_window(start: datetime, end: datetime, *, max_days: int | None = None) -> None:
    if start > end:
        raise ValidationFailed("start 晚于 end")
    cap = max_days or get_settings().trace_max_days
    if end - start > timedelta(days=cap):
        raise ValidationFailed(f"查询时间窗不得超过 {cap} 天")
