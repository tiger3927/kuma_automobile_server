"""codec 抽象：报文协议未定 → 按 `proto` 字段分发，真实协议到位只加 codec（§8.9）。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime

from ...repositories.telemetry_repo import TelemetryPoint


@dataclass(slots=True)
class Decoded:
    kind: str                                   # telemetry | alarm | cmd_ack | heartbeat
    vin: str | None = None
    ts: datetime | None = None
    points: list[TelemetryPoint] = field(default_factory=list)
    alarm: dict | None = None
    ack: dict | None = None
    error: str | None = None


class Codec(ABC):
    proto: str

    @abstractmethod
    def decode(self, payload: bytes) -> Decoded: ...

    @abstractmethod
    def encode_cmd(self, cmd: dict) -> bytes: ...
