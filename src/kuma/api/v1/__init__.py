"""v1 路由汇总。"""

from fastapi import APIRouter

from . import alarms, auth, commands, fleet, gov, orgs, public, trips, vehicles, ws

api_router = APIRouter()
api_router.include_router(public.router)   # 无鉴权，必须落在 /api/v1/public/ 前缀下（限流规则按前缀匹配）
api_router.include_router(auth.router)
api_router.include_router(orgs.router)
api_router.include_router(vehicles.router)
api_router.include_router(fleet.router)
api_router.include_router(trips.router)
api_router.include_router(alarms.router)
api_router.include_router(commands.router)
api_router.include_router(gov.router)
api_router.include_router(ws.router)
