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
from .energy import (
    AllowedModelsIn,
    BatteryIn,
    BatteryModelIn,
    BatteryStatusIn,
    DepositRegisterIn,
    DepositRuleIn,
    LeaseIn,
    LeaseTerminateIn,
    NearbyQuery,
    OrgBusinessIn,
    StationIn,
    SwapIn,
    SwapPriceIn,
)
from .ops import AckIn, CommandIn, ConfirmIn, FenceIn, TrackQueryIn
from .points import OffsetApproveIn, OffsetCapacityIn, OffsetRuleIn
from .vehicle import (
    DeviceBindIn,
    StatusChangeIn,
    SubscriptionIn,
    TransferIn,
    VehicleCreateIn,
)

__all__ = [
    "AckIn", "AllowedModelsIn", "BatteryIn", "BatteryModelIn", "BatteryStatusIn", "CommandIn",
    "ConfirmIn", "DepositRegisterIn", "DepositRuleIn", "DeviceBindIn", "FenceIn", "LeaseIn",
    "LeaseTerminateIn", "LoginIn", "MemberIn", "NearbyQuery", "OffsetApproveIn",
    "OffsetCapacityIn", "OffsetRuleIn", "OrgBusinessIn", "OrgCreateIn",
    "PrimaryAdminTransferIn", "RefreshIn", "RegisterIn", "StationIn", "StatusChangeIn",
    "SubscriptionIn", "SwapIn", "SwapPriceIn", "SwitchIdentityIn", "TrackQueryIn", "TransferIn",
    "VehicleCreateIn",
]
