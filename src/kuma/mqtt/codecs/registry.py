"""codec 注册表：按报文里的 `proto` 分发；未注册的 proto 直接拒绝（不猜格式）。"""

from __future__ import annotations

import json

from .base import Codec, Decoded
from .json_v1 import JsonV1Codec

_REGISTRY: dict[str, Codec] = {}


def register(codec: Codec) -> None:
    _REGISTRY[codec.proto] = codec


def get_codec(proto: str) -> Codec | None:
    return _REGISTRY.get(proto)


def decode(payload: bytes) -> Decoded:
    try:
        proto = json.loads(payload.decode("utf-8")).get("proto", "v1")
    except (UnicodeDecodeError, json.JSONDecodeError):
        proto = "v1"
    codec = get_codec(str(proto))
    if codec is None:
        return Decoded(kind="error", error=f"unknown_proto:{proto}")
    return codec.decode(payload)


def encode_cmd(cmd: dict) -> bytes:
    codec = get_codec(str(cmd.get("proto", "v1")))
    if codec is None:
        raise ValueError(f"unknown proto {cmd.get('proto')}")
    return codec.encode_cmd(cmd)


register(JsonV1Codec())
