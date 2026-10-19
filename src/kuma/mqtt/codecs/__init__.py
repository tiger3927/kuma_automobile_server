"""协议编解码（可插拔）。"""

from .base import Codec, Decoded
from .json_v1 import JsonV1Codec
from .registry import decode, encode_cmd, get_codec, register

__all__ = ["Codec", "Decoded", "JsonV1Codec", "decode", "encode_cmd", "get_codec", "register"]
