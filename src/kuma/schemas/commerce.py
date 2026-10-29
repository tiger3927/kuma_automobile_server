"""潜客意向入参（`commerce.lead`，`Task-Server.md` §18.3）。

**两个入口、两套入参**，差别只在"能不能没有手机号"：

- `LeadIn`（登录用户，`POST /leads`）：身份已经解析出来了，`account_id` 由服务端填；
  手机号可空（可以不填，线索靠账号回访）。
- `LeadAnonymousIn`（portal 未登录，`POST /public/leads`）：**没有 principal**，所以
  `region_id` 与 `contact_phone` 必须显式给——否则这条线索既进不了任何地区的看板，
  也没法回访，只是垃圾。库里 `lead_has_contact` 会把两者皆空的行直接拒掉。

`payload` 不接受自由 dict：公开端点收到的 JSON 形状不可信，自由 dict 等于给这张表
开了一个无界的"什么都能塞"列，日后既没法查询也没法定位是谁写进来的。
这里只收四个有界字段（来源页 / utm / 提交时坐标），坐标用于就近分派，其余用于渠道归因。

取值域 `LEAD_STATUS` / `LEAD_SOURCE` 必须与迁移 `0012` 的 CHECK、以及
`services/lead_service.py` 的状态机**逐字一致**（同 `schemas/kyc.py` 与 `0010` 的关系）。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

#: 与迁移 0012 的 `lead_status_check` 一致
LeadStatus = Literal["new", "contacted", "visited", "won", "lost"]
#: 与迁移 0012 的 `lead_source_check` 一致
LeadSource = Literal["portal", "app", "dealer", "exhibition", "campaign"]


class LeadContext(BaseModel):
    """渠道归因与就近分派用的上下文（全部可空，全部有界）。"""

    page: str | None = Field(default=None, max_length=200, description="来源页路径")
    utm: str | None = Field(default=None, max_length=120, description="utm_source/medium/campaign 拼接")
    lat: float | None = Field(default=None, ge=-90, le=90)
    lng: float | None = Field(default=None, ge=-180, le=180)


class LeadIn(BaseModel):
    """登录用户登记意向（`lead:write`）。"""

    contact_name: str = Field(min_length=1, max_length=80)
    contact_phone: str | None = Field(default=None, min_length=5, max_length=32,
                                      description="可空：账号本身就是回访入口")
    intent_type_code: str | None = Field(default=None, max_length=40,
                                         description="意向车型（core.vehicle_type.type_code）")
    intent_dealer_org_id: int | None = Field(default=None,
                                             description="用户自选的门店；不填则由系统就近分派")
    note: str | None = Field(default=None, max_length=500)
    context: LeadContext | None = None


class LeadAnonymousIn(LeadIn):
    """portal 未登录登记意向（`POST /public/leads`，无限流之外的任何身份）。"""

    region_id: str = Field(min_length=2, max_length=2, description="意向地区（core.region.country_code）")
    contact_phone: str = Field(min_length=5, max_length=32)


class LeadStatusIn(BaseModel):
    """经销商/平台改状态（`POST /leads/{id}/status`）。

    `assign_dealer_org_id` **只有平台能改**（重新分派）：经销商只能改派给自己名下的线索，
    能改派给别人就等于把别家的线索捞走。裁决在 `services/lead_service.py`。
    """

    status: LeadStatus
    note: str | None = Field(default=None, max_length=500)
    assign_dealer_org_id: int | None = None
