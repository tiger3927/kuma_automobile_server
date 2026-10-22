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


class SilenceIn(BaseModel):
    snooze_s: int | None = Field(
        default=None, ge=60, le=2_592_000,
        description="静默秒数（60s ~ 30 天）；留空用 KUMA_ALARM_SILENCE_DEFAULT_S")
    note: str | None = None


class CloseIn(BaseModel):
    note: str | None = None


class ReopenIn(BaseModel):
    note: str = Field(min_length=3, description="重开原因，必填且写进审计")


class FenceIn(BaseModel):
    """围栏创建（C4）。几何用 **WKT POLYGON / SRID 4326**——
    与 `geo/fence.py` 的 `ST_Contains(geom, point::geometry)` 判定口径一致。

    `fence_type` 取值域：`operating_area`（获准经营区，派单硬过滤）|
    `no_go`（禁行区）| `alert`（告警区）。写死在这三个，因为 worker 规则按它分流。
    """

    name: str = Field(min_length=2, max_length=120)
    fence_type: str = Field(pattern="^(operating_area|no_go|alert)$")
    wkt_polygon: str = Field(description="SRID=4326 的 POLYGON WKT，环必须闭合")
    org_id: int | None = Field(default=None, description="归属运营商；operating_area 必填")
    region_id: str | None = Field(default=None, description="两位国家码，对应 core.region")
    status: str = Field(default="active", pattern="^(active|inactive|draft)$")
    active_from: datetime | None = None
    active_to: datetime | None = None


class FencePatch(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=120)
    status: str | None = Field(default=None, pattern="^(active|inactive|draft)$")
    active_from: datetime | None = None
    active_to: datetime | None = None

