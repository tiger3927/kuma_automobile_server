"""围栏业务（C4）。

**权限口径（2026-10-22 定）**：围栏定义"运营商获准经营哪些区域"，这是**平台动作**——
所以创建/修改/停用只允许 `platform_staff`；运营商只读自己 `org_id` 的围栏。
理由：如果 operator 能改自己的经营区，派单硬过滤（§8.4）就形同虚设。

几何校验全部交给 PostGIS，但**范围与类型**要在应用层判定，否则会出现两类静默失效：
- 退化多边形（面积 0）→ `ST_Contains` 永远 false，围栏存在但从不生效；
- 跨大洲的巨型多边形 → "在经营区域内"永远 true，过滤失效。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.audit import write_audit
from ..core.exceptions import Forbidden, NotFound, ValidationFailed
from ..core.outbox import emit
from ..core.scope import Principal
from ..models.core import Org, Region
from ..repositories.fence_repo import MAX_AREA_KM2, MIN_AREA_KM2, FenceRepo
from ..schemas.ops import FenceIn, FencePatch


class FenceService:
    repo = FenceRepo()

    async def page(self, principal: Principal, *, session: AsyncSession,
                   org_id: int | None = None,
                   region_id: str | None = None, fence_type: str | None = None,
                   status: str | None = None, limit: int = 50, offset: int = 0) -> dict:
        # 非平台身份只能看自己可见 org 集合内的围栏（Principal 的字段是 `org_ids`，复数）
        org_ids: list[int] | None = None
        if not principal.is_platform:
            org_ids = principal.org_ids()      # 是方法不是属性（scope.py:146）
            if not org_ids:
                return {"items": []}
        return {"items": await self.repo.page(session, org_id=org_id, org_ids=org_ids,
                                              region_id=region_id, fence_type=fence_type,
                                              status=status, limit=limit, offset=offset)}

    async def detail(self, principal: Principal, session: AsyncSession,
                     fence_id: int) -> dict:
        row = await self.repo.get(session, fence_id)
        if row is None:
            raise NotFound("fence_not_found")
        if not principal.is_platform and row["org_id"] not in principal.org_ids():
            raise Forbidden("fence_out_of_scope")
        return row

    async def create(self, session: AsyncSession, principal: Principal, body: FenceIn,
                     *, ip: str | None = None) -> dict:
        if not principal.is_platform:
            raise Forbidden("fence_write_requires_platform")
        if body.fence_type == "operating_area" and body.org_id is None:
            raise ValidationFailed("operating_area 必须指定 org_id（派单硬过滤按 org 判定）")
        await self._check_refs(session, org_id=body.org_id, region_id=body.region_id)

        geom = await self._check_geom(body.wkt_polygon)
        fence_id = await self.repo.insert(
            session,
            name=body.name, fence_type=body.fence_type, org_id=body.org_id,
            region_id=body.region_id, wkt=body.wkt_polygon, status=body.status,
            active_from=body.active_from, active_to=body.active_to,
        )
        await write_audit(session, actor=principal, action="fence.create",
                          resource_type="fence", resource_id=fence_id,
                          after={"name": body.name, "fence_type": body.fence_type,
                                 "org_id": body.org_id, "region_id": body.region_id,
                                 "area_km2": geom["area_km2"]}, ip=ip)
        await emit(session, aggregate_type="fence", aggregate_id=fence_id, event_type="created",
                   payload={"name": body.name, "fence_type": body.fence_type,
                            "org_id": body.org_id, "status": body.status})
        row = await self.repo.get(session, fence_id)
        assert row is not None
        return row

    async def patch(self, session: AsyncSession, principal: Principal, fence_id: int,
                    body: FencePatch, *, ip: str | None = None) -> dict:
        if not principal.is_platform:
            raise Forbidden("fence_write_requires_platform")
        before = await self.repo.get(session, fence_id)
        if before is None:
            raise NotFound("fence_not_found")
        row = await self.repo.update(session, fence_id, name=body.name, status=body.status,
                                     active_from=body.active_from, active_to=body.active_to)
        if row is None:
            raise NotFound("fence_not_found")
        await write_audit(session, actor=principal, action="fence.update",
                          resource_type="fence", resource_id=fence_id,
                          before={"status": before["status"], "name": before["name"]},
                          after={"status": row["status"], "name": row["name"]}, ip=ip)
        await emit(session, aggregate_type="fence", aggregate_id=fence_id, event_type="updated",
                   payload={"name": row["name"], "org_id": row["org_id"],
                            "fence_type": row["fence_type"], "status": row["status"],
                            "from_status": before["status"]})
        return row

    async def deactivate(self, session: AsyncSession, principal: Principal, fence_id: int,
                         *, ip: str | None = None) -> dict:
        """停用而不是物理删除：围栏参与过派单与告警判定，删掉会让历史无法复现。"""
        if not principal.is_platform:
            raise Forbidden("fence_write_requires_platform")
        if await self.repo.get(session, fence_id) is None:
            raise NotFound("fence_not_found")
        row = await self.repo.update(session, fence_id, status="inactive")
        if row is None:
            raise NotFound("fence_not_found")
        await write_audit(session, actor=principal, action="fence.deactivate",
                          resource_type="fence", resource_id=fence_id, ip=ip)
        # 停用必须广播：`fence_exit` 告警与派单硬过滤都读这张表，
        # 别的进程要能在下一轮之前知道"这块区域不再受约束了"。
        await emit(session, aggregate_type="fence", aggregate_id=fence_id,
                   event_type="deactivated",
                   payload={"name": row["name"], "org_id": row["org_id"],
                            "fence_type": row["fence_type"], "status": row["status"]})
        return row

    async def _check_refs(self, session: AsyncSession, *, org_id: int | None,
                          region_id: str | None) -> None:
        """外键先查再插。

        2026-10-22 实测：`org_id` 填一个不存在的 id，Postgres 抛 ForeignKeyViolation，
        接口直接 500（当时没有 IntegrityError 兜底处理器），运维看不出是自己填错了。
        """
        if org_id is not None:
            found = (await session.execute(select(Org.id).where(Org.id == org_id)
                                           )).scalar_one_or_none()
            if found is None:
                raise ValidationFailed(f"org {org_id} 不存在", code="fence_org_not_found")
        if region_id is not None:
            region_found = (await session.execute(select(Region.country_code).where(
                Region.country_code == region_id))).scalar_one_or_none()
            if region_found is None:
                raise ValidationFailed(f"region {region_id} 不存在",
                                       code="fence_region_not_found")

    async def _check_geom(self, wkt: str) -> dict:
        try:
            geom = await self.repo.validate_wkt(wkt)
        except Exception as exc:                      # 非法 WKT 由 PostGIS 抛出
            raise ValidationFailed(f"WKT 解析失败：{type(exc).__name__}",
                                   code="fence_wkt_invalid") from exc
        if geom["gtype"] != "POLYGON":
            raise ValidationFailed(f"只接受 POLYGON（收到 {geom['gtype']}）",
                                   code="fence_geom_type")
        if not geom["valid"]:
            raise ValidationFailed("多边形自相交或环未闭合", code="fence_geom_invalid")
        area = float(geom["area_km2"] or 0)
        if area < MIN_AREA_KM2:
            raise ValidationFailed(f"面积 {area:.4f} km² 过小，围栏永远不会命中",
                                   code="fence_area_too_small")
        if area > MAX_AREA_KM2:
            raise ValidationFailed(f"面积 {area:.0f} km² 超过上限 {MAX_AREA_KM2:.0f}",
                                   code="fence_area_too_large")
        # bbox 判定是"落在经纬度范围内"，不是"每个分量都等于某个边界值"
        # （第一版写成后者，导致任何合法围栏都被判越界——2026-10-22 实测抓到）。
        xmin, xmax = float(geom["xmin"]), float(geom["xmax"])
        ymin, ymax = float(geom["ymin"]), float(geom["ymax"])
        if not (xmin >= -180.0 and xmax <= 180.0 and ymin >= -90.0 and ymax <= 90.0):
            raise ValidationFailed(f"坐标越界（x=[{xmin},{xmax}] y=[{ymin},{ymax}]）",
                                   code="fence_out_of_bounds")
        return geom
