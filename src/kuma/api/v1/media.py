"""媒体路由（M2：车型图 / 商品图 / 门头照 / 头像 / 凭证照片）。

`api` 层不写 SQL（§7 铁律）——这里只做 multipart 解析、大小闸门与响应封装。
"""

from __future__ import annotations

from fastapi import APIRouter, File, Form, Query, Request, UploadFile
from fastapi.responses import FileResponse

from ...core.config import get_settings
from ...core.deps import Db, PrincipalDep, client_ip
from ...core.exceptions import ValidationFailed
from ...models.core import Media
from ...schemas.media import MediaSortIn
from ...services.media_service import media_service

router = APIRouter(prefix="/media", tags=["media"])


def _media_dict(m: Media) -> dict:
    return {"id": m.id, "owner_type": m.owner_type, "owner_id": m.owner_id, "role": m.role,
            "mime": m.mime, "width_px": m.width_px, "height_px": m.height_px,
            "size_bytes": m.size_bytes, "sha256": m.sha256, "sort": m.sort,
            "created_at": m.created_at}


@router.get("/usage")
async def usage(session: Db, principal: PrincipalDep) -> dict:
    """在册媒体台账（行数与字节合计）。磁盘真值要 `du`，这里只是业务侧口径。"""
    principal.require("report", "read")
    return await media_service.disk_usage(session)


@router.get("")
async def list_media(session: Db, principal: PrincipalDep,
                     owner_type: str = Query(...), owner_id: int = Query(...)) -> dict:
    rows = await media_service.list_for(session, principal,
                                        owner_type=owner_type, owner_id=owner_id)
    return {"items": [_media_dict(m) for m in rows]}


@router.post("", status_code=201)
async def upload_media(request: Request, session: Db, principal: PrincipalDep,
                       file: UploadFile = File(...), owner_type: str = Form(...),
                       owner_id: int = Form(...), role: str = Form(...)) -> dict:
    """multipart 上传。上限 `KUMA_MEDIA_MAX_BYTES`（默认 5MB）。

    多读 1 字节来判断超限：`UploadFile.size` 在部分 starlette 版本不可靠，
    不能拿它当闸门（否则 5MB 上限形同虚设）。
    """
    limit = get_settings().media_max_bytes
    data = await file.read(limit + 1)
    if len(data) > limit:
        raise ValidationFailed(f"超过上限 {limit} 字节")

    media = await media_service.upload(
        session, principal, owner_type=owner_type, owner_id=owner_id, role=role,
        data=data, mime=file.content_type, ip=client_ip(request))
    return _media_dict(media)


@router.get("/{media_id}/file")
async def get_media_file(media_id: int, session: Db, principal: PrincipalDep) -> FileResponse:
    path, mime = await media_service.open_file(session, principal, media_id)
    return FileResponse(path, media_type=mime)


@router.patch("/{media_id}/sort")
async def reorder(media_id: int, body: MediaSortIn, request: Request, session: Db,
                  principal: PrincipalDep) -> dict:
    m = await media_service.set_sort(session, principal, media_id, body.sort,
                                     ip=client_ip(request))
    return _media_dict(m)


@router.delete("/{media_id}")
async def delete_media(media_id: int, request: Request, session: Db,
                       principal: PrincipalDep) -> dict:
    return await media_service.delete(session, principal, media_id, ip=client_ip(request))
