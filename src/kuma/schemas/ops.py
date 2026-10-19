"""车联网运行侧 schema：遥测查询、行程、告警、下行指令。"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class TrackQueryIn(BaseModel):
    vin: str
    start: datetime
    end: datetime
    limit: int = Field(default=5000, le=20000)


class CommandIn(BaseModel):
    vin: str
    cmd_type: str
    payload: dict = Field(default_factory=dict)
    confirm_token: str | None = None
    co_approver_account_id: int | None = None


class ConfirmIn(BaseModel):
    vin: str
    cmd_type: str


class AckIn(BaseModel):
    note: str | None = None


class FenceIn(BaseModel):
    name: str
    fence_type: str
    wkt_polygon: str = Field(description="SRID=4326 的 POLYGON WKT")
    org_id: int | None = None
    region_id: str | None = None
