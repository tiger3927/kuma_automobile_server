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


# ══ 返积分规则（`mkt.points_rule`，§7.2 / §9.5 双人授权）══════════════════════
#: 取值域就是 DDL 的 CHECK（006 建表 + 0008 扩 'swap'）——写错值会被库拒，
#: 但让 pydantic 先挡住才能给出可读错误，而不是 `points_rule_source_type_check`
AwardSource = Literal["vehicle_purchase", "commerce", "ride", "campaign", "purchase", "swap"]


class AwardRuleIn(BaseModel):
    """返积分规则（平台申报，双人授权后生效）。

    `ratio` 与 `fixed_points` **二选一**：前者是"每 1 最小货币单位返多少积分"
    （换电/商城/打车按实付现金算），后者是"每单固定点数"（买车按车型）。
    两个都给会无法判定优先级，services 直接拒。
    """

    rule_code: str = Field(min_length=2, max_length=40)
    source_type: AwardSource
    region_id: str | None = Field(default=None, pattern="^[A-Z]{2}$",
                                  description="NULL = 全区兜底行")
    currency: str | None = Field(default=None, pattern="^[A-Z]{3}$",
                                 description="NULL = 不限币种；ratio 按最小货币单位，跨币种必须另配一行")
    type_code: str | None = Field(default=None, max_length=40,
                                  description="按车型/型号给不同比例时填")
    ratio: float | None = Field(default=None, gt=0, lt=100,
                                description="每 1 最小货币单位返多少积分（THB 下 1 泰铢返 1 积分 = 0.01）")
    fixed_points: int | None = Field(default=None, ge=1, description="每单固定返多少积分")
    caps: dict[str, int] = Field(default_factory=dict,
                                description='风控上限，如 {"per_order_max":500,"per_day_max":2000}')
    version: int = Field(ge=1)
    effective_from: date
    effective_to: date | None = None
    validity_months: int = Field(default=12, ge=1, le=120,
                                 description="赠送积分过期月数（§7.1 默认 12）")
    co_approver_account_id: int | None = Field(default=None, description="联审人（双人授权）")


class AwardApproveIn(BaseModel):
    approve: bool = True
    co_approver_account_id: int | None = None


# ══ 积分包（§5.1 积分包是商品 / §7.2 购买与限额 / §7.4 退款）═════════════════
class PackIn(BaseModel):
    """配一个积分包：平台一次落 `product` + `sku` + `points_pack`（不是"充值档位"）。"""

    pack_code: str = Field(min_length=2, max_length=40)
    name: str = Field(min_length=1, max_length=120)
    points: int = Field(ge=1, description="包内含多少积分")
    price: int = Field(ge=1, description="售价（最小货币单位，§3.2）")
    currency: str = Field(pattern="^[A-Z]{3}$")
    validity_months: int = Field(default=24, ge=1, le=120, description="§7.2 默认 24 个月")
    region_id: str | None = Field(default=None, pattern="^[A-Z]{2}$",
                                  description="NULL = 全区可售")


class VoucherIn(BaseModel):
    """回填转账流水号（§5.2 `qr_issued → voucher_pending`）。

    `transfer_no` 是**唯一防线的键**：同一笔转账给多个订单核销会被 `uq_transfer_no` 拒。
    """

    transfer_no: str = Field(min_length=4, max_length=80)
    amount: int | None = Field(default=None, ge=0, description="实付金额（给了就必须等于应付）")
    currency: str | None = Field(default=None, pattern="^[A-Z]{3}$")
    proof_url: str | None = Field(default=None, max_length=500, description="转账截图/凭证地址")


class VoucherVerifyIn(BaseModel):
    """核销凭证（§9.5 收款核销工作台，DUAL_CONTROL）。"""

    approve: bool = True
    co_approver_account_id: int | None = None


class PackRefundIn(BaseModel):
    """积分包退款（按原路退现金 + 整包扣回积分，§7.4 禁止交叉）。"""

    reason: str | None = Field(default=None, max_length=200)
    co_approver_account_id: int | None = None
