"""积分域入参（`Task-Plan.md` §7 / §16.9）。

`points_per_unit` 与 `unit_scale` 必须成对出现：金额列是最小货币单位（§3.2），
只给"1 元 = 100 积分"而不说"1 元含多少最小单位"，在 JPY/KRW（0 位小数）下会算错一个数量级。
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

BizLine = Literal["commerce", "ride", "swap"]


class OffsetRuleIn(BaseModel):
    """配置抵扣规则（平台，双人授权 §9.5）。"""

    rule_code: str = Field(min_length=2, max_length=40)
    biz_line: BizLine
    region_id: str | None = Field(default=None, pattern="^[A-Z]{2}$",
                                  description="NULL = 全区兜底行")
    currency: str = Field(pattern="^[A-Z]{3}$")
    points_per_unit: int = Field(ge=1, description="1 主币单位 = 多少积分（本轮定 100）")
    unit_scale: int = Field(default=100, ge=1, le=10000,
                            description="1 主币单位含多少最小货币单位（THB/MYR=100，JPY/KRW=1）")
    max_offset_pct: int = Field(ge=0, le=100, description="单笔最多抵百分之几（§7.3 / §16.9）")
    min_amount: int | None = Field(default=None, ge=0, description="低于此金额不允许抵扣")
    version: int = Field(ge=1)
    effective_from: date
    effective_to: date | None = None
    co_approver_account_id: int | None = Field(default=None, description="联审人（双人授权）")


class OffsetApproveIn(BaseModel):
    approve: bool = True
    co_approver_account_id: int | None = None


class OffsetCapacityIn(BaseModel):
    """下单前问"这单最多能抵多少"，不扣任何东西。"""

    biz_line: BizLine
    amount: int = Field(ge=0, description="订单金额（最小货币单位）")
    currency: str = Field(pattern="^[A-Z]{3}$")
    region_id: str | None = Field(default=None, pattern="^[A-Z]{2}$")
