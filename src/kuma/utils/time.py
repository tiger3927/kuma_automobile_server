"""时间：全链路 UTC（Task-Server.md §12-1）。展示层按 `core.region.timezone` 转换。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo


def utcnow() -> datetime:
    return datetime.now(UTC)


def ensure_utc(dt: datetime) -> datetime:
    return dt.astimezone(UTC) if dt.tzinfo else dt.replace(tzinfo=UTC)


def to_local(dt: datetime, tz_name: str) -> datetime:
    return ensure_utc(dt).astimezone(ZoneInfo(tz_name))


def parse_iso(text: str) -> datetime:
    return ensure_utc(datetime.fromisoformat(text.replace("Z", "+00:00")))


def window(days: int = 1) -> tuple[datetime, datetime]:
    end = utcnow()
    return end - timedelta(days=days), end
