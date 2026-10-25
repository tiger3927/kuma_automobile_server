"""v1 路由汇总。"""

from fastapi import APIRouter

from . import (
    alarms,
    audit,
    auth,
    commands,
    energy,
    fences,
    fleet,
    gdpr,
    gov,
    kyc,
    media,
    orgs,
    points,
    public,
    trips,
    vehicles,
    ws,
)

api_router = APIRouter()
api_router.include_router(public.router)   # 无鉴权，必须落在 /api/v1/public/ 前缀下（限流规则按前缀匹配）
api_router.include_router(auth.router)
api_router.include_router(orgs.router)
api_router.include_router(vehicles.router)
api_router.include_router(media.router)
api_router.include_router(gdpr.router)     # /me/data 与 /me/erasure
api_router.include_router(kyc.me_router)   # /me/kyc：本人提交与查看（kyc:write / kyc:read）
api_router.include_router(kyc.router)      # /kyc/queue、/kyc/{id}/review：平台审核（kyc:review，双人）
api_router.include_router(fleet.router)
api_router.include_router(trips.router)
api_router.include_router(alarms.router)
api_router.include_router(commands.router)
api_router.include_router(gov.router)
api_router.include_router(audit.router)     # R_AUDIT 只读；查审计本身也写审计
api_router.include_router(energy.router)    # 换电与电池资产（M15）：scope 按 swap_operator 业务线
api_router.include_router(points.router)    # 积分余额/流水/可抵额度 + 抵扣规则（没有"扣积分"端点）
api_router.include_router(fences.router)    # 围栏：写操作只允许 platform
api_router.include_router(ws.router)
