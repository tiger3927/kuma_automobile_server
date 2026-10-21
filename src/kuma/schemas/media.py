"""媒体相关 schema。"""

from __future__ import annotations

from pydantic import BaseModel, Field


class MediaSortIn(BaseModel):
    sort: int = Field(ge=0, le=32767)


class ErasureIn(BaseModel):
    """删除权必须是**显式确认**：不可逆动作不接受"手滑点一下"。"""

    confirm: bool
    reason: str | None = Field(default=None, max_length=200)
