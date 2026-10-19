"""口令哈希与 JWT（Task-Server.md §6 依赖：pwdlib[argon2] + pyjwt，不用 passlib/bcrypt）。

访问令牌携带 `active_identity` + `identity_id`（Task-Plan.md §2.2 约束 1）：
**scope 不写进 token**，服务端每次请求按 membership 重新解析，不信任前端传的 org_id。
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from pwdlib import PasswordHash
from pwdlib.hashers.argon2 import Argon2Hasher

from .config import get_settings
from .exceptions import Unauthorized

# pwdlib 0.2 的入口类是 PasswordHash（不是 passlib 的 CryptContext）
_hasher = PasswordHash((Argon2Hasher(),))


def hash_password(raw: str) -> str:
    return _hasher.hash(raw)


def verify_password(raw: str, hashed: str | None) -> bool:
    if not hashed:
        return False
    try:
        return _hasher.verify(raw, hashed)   # 实测签名：verify(password, hash)
    except Exception:
        return False


def hash_token(raw: str) -> str:
    """refresh token / OTP 只存哈希。"""
    return hashlib.sha256(raw.encode()).hexdigest()


def new_random_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def _now() -> datetime:
    return datetime.now(UTC)


def create_access_token(*, account_id: int, identity_id: int, active_identity: str,
                        extra: dict[str, Any] | None = None) -> tuple[str, int]:
    s = get_settings()
    now = _now()
    payload: dict[str, Any] = {
        "sub": str(account_id),
        "identity_id": identity_id,
        "active_identity": active_identity,
        "typ": "access",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=s.access_ttl)).timestamp()),
        "jti": new_random_token(12),
    }
    if extra:
        payload.update(extra)
    return jwt.encode(payload, s.jwt_secret, algorithm=s.jwt_alg), s.access_ttl


def create_refresh_token() -> tuple[str, datetime]:
    """不透明随机串（存哈希，可撤销）；返回 (明文, 过期时间)。"""
    s = get_settings()
    raw = new_random_token(48)
    return raw, _now() + timedelta(seconds=s.refresh_ttl)


def decode_access_token(token: str) -> dict[str, Any]:
    s = get_settings()
    try:
        payload = jwt.decode(token, s.jwt_secret, algorithms=[s.jwt_alg])
    except jwt.ExpiredSignatureError as exc:
        raise Unauthorized("token_expired") from exc
    except jwt.PyJWTError as exc:
        raise Unauthorized("token_invalid") from exc
    if payload.get("typ") != "access":
        raise Unauthorized("token_type")
    return payload
