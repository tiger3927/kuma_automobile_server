"""services：唯一业务层（api 与 gateway/worker 共用），**禁止 Depends/Request**（§7 铁律 3）。"""

from .alarm_service import AlarmService
from .auth_service import AuthService
from .command_service import CommandService, issue_confirm
from .org_service import OrgService
from .telemetry_service import TelemetryService
from .trip_service import TripService
from .vehicle_service import VEHICLE_TRANSITIONS, VehicleService

__all__ = [
    "VEHICLE_TRANSITIONS",
    "AlarmService",
    "AuthService",
    "CommandService",
    "OrgService",
    "TelemetryService",
    "TripService",
    "VehicleService",
    "issue_confirm",
]
