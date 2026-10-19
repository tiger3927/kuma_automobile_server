"""Pydantic v2 请求/响应模型（与 ORM 分离，§7）。"""

from .auth import (
    LoginIn,
    MemberIn,
    OrgCreateIn,
    PrimaryAdminTransferIn,
    RefreshIn,
    RegisterIn,
    SwitchIdentityIn,
)
from .ops import AckIn, CommandIn, ConfirmIn, FenceIn, TrackQueryIn
from .vehicle import (
    DeviceBindIn,
    StatusChangeIn,
    SubscriptionIn,
    TransferIn,
    VehicleCreateIn,
)

__all__ = [
    "AckIn", "CommandIn", "ConfirmIn", "DeviceBindIn", "FenceIn", "LoginIn", "MemberIn",
    "OrgCreateIn", "PrimaryAdminTransferIn", "RefreshIn", "RegisterIn", "StatusChangeIn",
    "SubscriptionIn", "SwitchIdentityIn", "TrackQueryIn", "TransferIn", "VehicleCreateIn",
]
