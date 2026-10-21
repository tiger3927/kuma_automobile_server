"""媒体服务：`core.media` 的唯一写入口（M2 起图片/文件统一走这张表）。

设计边界（`Task-DataBase.md` §11.1"不为每种对象建 media 列"）：
- **落盘 = 本地磁盘**（`settings.media_root`），库里只存 `file_key`（相对路径）。v1 没有对象存储，
  换 S3/OSS 只改这里的 `_write` / `_unlink` / `abs_path`，表结构不动。
- **软删**：`status='deleted'` 保留行做审计线索，**磁盘文件立即删**（照片是个人数据，
  行使删除权时不能只改状态）。`uq_media_slot` 是 `WHERE status='active'` 的部分唯一索引，
  软删后槽位自动释放。
- **不解析图片宽高**：依赖里没有 pillow（§6 依赖清单没列，也不为这个加），
  `width_px/height_px` 由客户端提交，缺省留空——**别误以为服务端校验过图片尺寸**。
  但**字节头会校验**（`_MAGIC`）：声明 `image/jpeg` 就必须真是 JPEG，尺寸不管。
- 权限按 `owner_type` 分流（下表），公开类（车型/商品）任何登录身份可读。
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import secrets
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.audit import write_audit
from ..core.config import get_settings
from ..core.exceptions import Forbidden, NotFound, ValidationFailed
from ..core.logging import get_logger
from ..core.scope import Principal
from ..models.core import Media, Vehicle
from ..repositories.base import assert_vehicle_visible

log = get_logger("services.media")

# 与 `core.media.owner_type` 的 CHECK 取值域严格一致（migrations/sql/002_core.sql:241）
OWNER_TYPES = frozenset({
    "vehicle_type", "vehicle", "product", "sku", "order", "aftersale",
    "driver_profile", "org", "account",
})
# owner_type → 需要的 (resource, action)。不在表里的 owner_type 一律拒绝。
OWNER_PERM: dict[str, tuple[str, str]] = {
    "vehicle": ("vehicle_master", "write"),
    "vehicle_type": ("vehicle_master", "write"),
    "product": ("product", "write"),
    "sku": ("product", "write"),
    "order": ("order", "write"),
    "aftersale": ("order", "write"),
    "org": ("org", "write"),
    "account": ("self", "write"),
    "driver_profile": ("driver_review", "write"),
}
# 公开可读（车型图、商品图、门头照要能被官网/游客侧看到）
PUBLIC_OWNERS = frozenset({"vehicle_type", "product", "sku"})
# mime → 落盘扩展名。**白名单外一律拒绝**，不接受客户端声明的 filename 后缀。
ALLOWED_MIME: dict[str, str] = {
    "image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "application/pdf": "pdf",
}
# 声明的 mime 必须与**字节头**一致。只信 `UploadFile.content_type` 等于把闸门交给客户端：
# 任何人把脚本改名 .jpg 并声明 image/jpeg 就能存进来，再被 GET /media/{id}/file 按
# 声明的 mime 原样吐给别的用户（存储型 XSS / 恶意文件入口）。2026-10-21 端到端时发现的。
_MAGIC: dict[str, tuple[bytes, ...]] = {
    "image/jpeg": (b"\xff\xd8\xff",),
    "image/png": (b"\x89PNG\r\n\x1a\n",),
    "image/webp": (b"RIFF",),
    "application/pdf": (b"%PDF-",),
}
_ROLE_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


def _magic_ok(mime: str, data: bytes) -> bool:
    if mime == "image/webp":       # RIFF 是容器，必须再看 8..12 的 "WEBP"
        return data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    return any(data.startswith(p) for p in _MAGIC[mime])


class MediaService:

    # ── 路径 ────────────────────────────────────────────────────
    def abs_path(self, file_key: str) -> Path:
        """`file_key` 是相对 `media_root` 的路径；越界（`..`、绝对路径）直接拒。"""
        root = Path(get_settings().media_root).resolve()
        p = (root / file_key).resolve()
        if not p.is_relative_to(root):
            raise ValidationFailed("file_key 越界")
        return p

    # ── 校验 ────────────────────────────────────────────────────
    def _check_owner(self, principal: Principal, owner_type: str, owner_id: int) -> None:
        if owner_type not in OWNER_TYPES:
            raise ValidationFailed(f"owner_type 必须在 {sorted(OWNER_TYPES)}")
        resource, action = OWNER_PERM[owner_type]
        principal.require(resource, action)
        # 自己的头像只能自己传（platform_staff 对 self 只有 read，见 §10.1 矩阵）
        if owner_type == "account" and owner_id != principal.account_id:
            raise Forbidden("不能给他人账号上传媒体")

    async def _check_target(self, session: AsyncSession, principal: Principal,
                            owner_type: str, owner_id: int) -> None:
        """vehicle 类要过归属校验，别把图挂到别人车上。"""
        if owner_type != "vehicle":
            return
        vehicle = (await session.execute(
            select(Vehicle).where(Vehicle.id == owner_id))).scalars().first()
        if vehicle is None:
            raise NotFound("vehicle_not_found")
        assert_vehicle_visible(principal, vehicle)

    # ── 写 ──────────────────────────────────────────────────────
    async def upload(self, session: AsyncSession, principal: Principal, *,
                     owner_type: str, owner_id: int, role: str, data: bytes,
                     mime: str | None, ip: str | None = None) -> Media:
        self._check_owner(principal, owner_type, owner_id)
        await self._check_target(session, principal, owner_type, owner_id)

        if not role or not _ROLE_RE.match(role):
            raise ValidationFailed("role 只允许小写字母/数字/下划线，≤32 位")
        if mime not in ALLOWED_MIME:
            raise ValidationFailed(f"mime 必须在 {sorted(ALLOWED_MIME)}")
        limit = get_settings().media_max_bytes
        if not data:
            raise ValidationFailed("空文件")
        if len(data) > limit:
            raise ValidationFailed(f"超过上限 {limit} 字节")
        if not _magic_ok(mime, data):
            raise ValidationFailed(f"文件内容与声明的 {mime} 不符")

        row = Media(
            owner_type=owner_type, owner_id=owner_id, role=role,
            file_key="", mime=mime, size_bytes=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
            uploaded_by=principal.account_id, status="active",
        )
        # 先 flush 拿 id（identity 列），再用 id 拼 file_key 落盘：
        # 落盘失败会抛异常 → 调用方的事务回滚，库里不会留下指向空文件的行。
        session.add(row)
        await session.flush()
        key = f"{owner_type}/{owner_id}/{role}/{row.id}-{secrets.token_hex(8)}.{ALLOWED_MIME[mime]}"
        await self._write(key, data)
        row.file_key = key

        await write_audit(session, actor=principal, action="media.upload",
                          resource_type="media", resource_id=row.id,
                          after={"owner_type": owner_type, "owner_id": owner_id, "role": role,
                                 "size_bytes": len(data), "mime": mime},
                          ip=ip)
        log.info("media.uploaded", id=row.id, owner=f"{owner_type}:{owner_id}", role=role,
                 size=len(data))
        return row

    async def set_sort(self, session: AsyncSession, principal: Principal, media_id: int,
                       sort: int, ip: str | None = None) -> Media:
        row = await self._get_visible(session, principal, media_id, write=True)
        row.sort = sort
        await write_audit(session, actor=principal, action="media.reorder",
                          resource_type="media", resource_id=row.id,
                          after={"sort": sort}, ip=ip)
        return row

    async def delete(self, session: AsyncSession, principal: Principal, media_id: int,
                     ip: str | None = None) -> dict:
        """软删库行 + **真删磁盘文件**（个人数据不能只改状态）。"""
        row = await self._get_visible(session, principal, media_id, write=True)
        row.status = "deleted"
        removed = await self._unlink(row.file_key)
        await write_audit(session, actor=principal, action="media.delete",
                          resource_type="media", resource_id=row.id,
                          before={"owner_type": row.owner_type, "owner_id": row.owner_id,
                                  "role": row.role},
                          after={"status": "deleted", "file_removed": removed}, ip=ip)
        return {"id": row.id, "status": row.status, "file_removed": removed}

    # ── 读 ──────────────────────────────────────────────────────
    async def list_for(self, session: AsyncSession, principal: Principal, *,
                       owner_type: str, owner_id: int) -> list[Media]:
        self._check_owner_read(principal, owner_type, owner_id)
        if owner_type == "vehicle":
            await self._check_target(session, principal, owner_type, owner_id)
        rows = (await session.execute(
            select(Media)
            .where(Media.owner_type == owner_type, Media.owner_id == owner_id,
                   Media.status == "active")
            .order_by(Media.role, Media.sort, Media.id)
        )).scalars().all()
        return list(rows)

    def _check_owner_read(self, principal: Principal, owner_type: str, owner_id: int) -> None:
        if owner_type in PUBLIC_OWNERS:
            return
        if owner_type == "account" and owner_id != principal.account_id:
            if not principal.is_platform:
                raise Forbidden("只能看自己的媒体")
            return
        resource, _ = OWNER_PERM.get(owner_type, ("self", "read"))
        principal.require(resource, "read")

    async def open_file(self, session: AsyncSession, principal: Principal,
                        media_id: int) -> tuple[Path, str]:
        row = await self._get_visible(session, principal, media_id)
        path = self.abs_path(row.file_key)
        if not path.exists():
            # 库里有行但文件没了（磁盘被清 / 迁移漏文件）——必须报出来而不是 500
            log.error("media.file_missing", id=row.id, file_key=row.file_key)
            raise NotFound("media_file_missing")
        return path, row.mime or "application/octet-stream"

    async def _get_visible(self, session: AsyncSession, principal: Principal,
                           media_id: int, *, write: bool = False) -> Media:
        row = await session.get(Media, media_id)
        if row is None or row.status != "active":
            raise NotFound("media_not_found")
        self._check_owner_read(principal, row.owner_type, row.owner_id)
        if write:
            resource, action = OWNER_PERM[row.owner_type]
            principal.require(resource, action)
            if row.owner_type == "account" and row.owner_id != principal.account_id \
                    and not principal.is_platform:
                raise Forbidden("不能改他人账号的媒体")
        return row

    # ── 磁盘 IO（都放线程池，别堵事件循环，§12-1）────────────────
    async def _write(self, file_key: str, data: bytes) -> None:
        path = self.abs_path(file_key)

        def _do() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)

        await asyncio.to_thread(_do)

    async def _unlink(self, file_key: str) -> bool:
        if not file_key:
            return False
        path = self.abs_path(file_key)

        def _do() -> bool:
            try:
                path.unlink()
                return True
            except FileNotFoundError:
                return False

        return await asyncio.to_thread(_do)

    # ── 给 gdpr 服务复用：按账号批量软删 ─────────────────────────
    async def erase_account_media(self, session: AsyncSession, account_id: int) -> dict:
        """行使删除权时清掉账号名下所有媒体（文件真删，行留 status='deleted'）。"""
        rows = (await session.execute(
            select(Media).where(Media.owner_type == "account",
                                Media.owner_id == account_id,
                                Media.status == "active")
        )).scalars().all()
        removed = 0
        for row in rows:
            row.status = "deleted"
            if await self._unlink(row.file_key):
                removed += 1
        return {"media_rows": len(rows), "files_removed": removed,
                "at": datetime.now(UTC).isoformat()}

    async def disk_usage(self, session: AsyncSession) -> dict:
        """`/media/usage` 用：库里在册文件的大小合计（磁盘真值要 du，这里只是台账）。"""
        total, n = (await session.execute(
            select(func.coalesce(func.sum(Media.size_bytes), 0), func.count())
            .where(Media.status == "active")
        )).one()
        # size_bytes 可空，SUM 在零行时返回 NULL（SQL 里已 coalesce，这里再兜一层给类型系统）
        return {"active_rows": int(n or 0), "active_bytes": int(total or 0)}


media_service = MediaService()
