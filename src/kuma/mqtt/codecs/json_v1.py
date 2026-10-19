"""v1 占位协议：自定义 JSON。

`{proto,vin,ts,lat,lon,spd,soc,odo,seq}`；支持单点或 `{"points":[...]}` 批量补报。
真实协议到位时新增 codec 并注册，不改网关逻辑。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from ...core.logging import get_logger
from ...repositories.telemetry_repo import TelemetryPoint
from .base import Codec, Decoded

log = get_logger("codec.json_v1")


def _parse_ts(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=UTC)
    try:
        text = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    except ValueError:
        return None


class JsonV1Codec(Codec):
    proto = "v1"

    def decode(self, payload: bytes) -> Decoded:
        try:
            data = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return Decoded(kind="error", error=f"bad_json:{exc}")
        if not isinstance(data, dict):
            return Decoded(kind="error", error="not_object")

        vin = data.get("vin")
        if not vin or not isinstance(vin, str):
            return Decoded(kind="error", error="missing_vin")

        if "cmd_id" in data and data.get("ack"):
            return Decoded(kind="cmd_ack", vin=vin, ts=_parse_ts(data.get("ts")),
                           ack={"cmd_id": data["cmd_id"], "result": data["ack"],
                                "attempt": data.get("attempt", 0)})

        if data.get("alarm"):
            return Decoded(kind="alarm", vin=vin, ts=_parse_ts(data.get("ts")),
                           alarm={"type": data["alarm"].get("type", "unknown"),
                                  "level": data["alarm"].get("level", 2),
                                  "payload": data["alarm"].get("payload", {})})

        if data.get("heartbeat"):
            return Decoded(kind="heartbeat", vin=vin, ts=_parse_ts(data.get("ts")) or _utcnow())

        raw_points = data.get("points") or [data]
        ts_default = _parse_ts(data.get("ts")) or _utcnow()
        points: list[TelemetryPoint] = []
        for item in raw_points:
            if not isinstance(item, dict):
                continue
            ts = _parse_ts(item.get("ts")) or ts_default
            points.append(TelemetryPoint(
                vin=vin, ts=ts,
                lat=_num(item.get("lat")), lon=_num(item.get("lon")),
                speed_kmh=_num(item.get("spd", item.get("speed_kmh"))),
                soc=_num(item.get("soc")), odo_km=_num(item.get("odo", item.get("odo_km"))),
                seq=_int(item.get("seq")), proto=self.proto, recv_ts=_utcnow(),
                quality=int(item.get("quality", 0) or 0),
            ))
        if not points:
            return Decoded(kind="error", vin=vin, error="no_points")
        return Decoded(kind="telemetry", vin=vin, ts=points[-1].ts, points=points)

    def encode_cmd(self, cmd: dict) -> bytes:
        return json.dumps({
            "proto": self.proto, "cmd_id": cmd["cmd_id"], "cmd": cmd["cmd_type"],
            "args": cmd.get("payload", {}), "attempt": cmd.get("attempt", 0),
            "ts": cmd.get("issued_at"), "ttl_s": cmd.get("ttl_s"),
            "nonce": cmd.get("nonce"), "sig": cmd.get("sig"),   # 防重放字段先留位（§9）
        }, ensure_ascii=False).encode("utf-8")


def _num(v) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _int(v) -> int | None:
    try:
        return None if v is None else int(v)
    except (TypeError, ValueError):
        return None


def _utcnow() -> datetime:
    return datetime.now(UTC)
