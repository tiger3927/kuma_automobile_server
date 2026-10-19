"""车辆主数据相关 schema。"""

from __future__ import annotations

from pydantic import BaseModel, Field


class VehicleCreateIn(BaseModel):
    vin: str = Field(min_length=17, max_length=17)
    type_code: str
    region_id: str = Field(min_length=2, max_length=2)
    owner_type: str
    owner_account_id: int | None = None
    owner_org_id: int | None = None
    plate: str | None = None
    model_year: int | None = None
    color: str | None = None
    factory_attrs: dict = Field(default_factory=dict)


class StatusChangeIn(BaseModel):
    status: str


class DeviceBindIn(BaseModel):
    device_sn: str
    iccid: str | None = None
    model: str | None = None


class SubscriptionIn(BaseModel):
    plan_id: int


class TransferIn(BaseModel):
    to_type: str
    to_account_id: int | None = None
    to_org_id: int | None = None
