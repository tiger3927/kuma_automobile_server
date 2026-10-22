"""换电与电池资产 schema（M15，`Task-Plan.md` §16）。

取值域一律用 `Field(pattern=...)` 在前置层挡住：库里对应列有 CHECK，
但 422 比 500（`constraint_violation`）对用户友好，也让前端能按 code 出文案。
"""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, Field

CHEMISTRY = "lfp|nmc|nca|lto|other"
BMS_PROTO = "can_od|can_kd|custom_json|none"
IP_RATING = "ip54|ip65|ip67|other"
MODEL_STATUS = "draft|active|retired"
STATION_STATUS = "active|paused|closed"
BUSINESS_LINE = "dealer|ride_operator|swap_operator|manufacturing"


# ── 型号字典（§16.2：字段由规划定义，厂家只填值）────────────────────────
class BatteryModelIn(BaseModel):
    model_code: str = Field(
        pattern=r"^KM-(LFP|NMC|NCA|LTO|OTHER)-[0-9]{2,4}$",
        description="编码规则 KM-<化学>-<度数×10>，如 KM-LFP-25；一经用于台账与定价不可改")
    name: str = Field(min_length=2, max_length=120)
    vendor: str = Field(min_length=2, max_length=120)
    spec_doc_media_id: int | None = Field(default=None, description="规格书+铭牌照（core.media.id）")

    kwh_nominal: float = Field(gt=0, le=9999)
    kwh_accept_min: float | None = Field(
        default=None, gt=0, description="入池验收下限；留空按 0.9×标称")
    voltage_nominal_v: int = Field(gt=0, le=2000)
    voltage_max_v: int | None = None
    voltage_cutoff_v: int | None = None
    capacity_ah: int | None = Field(default=None, gt=0)
    chemistry: str = Field(pattern=f"^({CHEMISTRY})$")
    cell_format: str | None = Field(default=None, pattern="^(prismatic|cylindrical|pouch)$")
    cycle_life_rated: int | None = Field(default=None, gt=0)
    charge_max_c: float | None = Field(default=None, gt=0, le=10)
    discharge_max_c: float | None = Field(default=None, gt=0, le=20)
    temp_charge_min_c: int | None = None
    temp_charge_max_c: int | None = None

    dims_l_mm: int = Field(gt=0, le=5000)
    dims_w_mm: int = Field(gt=0, le=5000)
    dims_h_mm: int = Field(gt=0, le=5000)
    weight_kg: float = Field(gt=0, le=2000)
    mounting: str = Field(min_length=2, max_length=120)
    mounting_drawing_media_id: int | None = None
    ip_rating: str = Field(pattern=f"^({IP_RATING})$")

    connector_type: str = Field(min_length=2, max_length=80)
    connector_keying: str | None = None
    bms_proto: str = Field(pattern=f"^({BMS_PROTO})$")
    bms_sn_field: str | None = None
    compatible_types: list[str] = Field(default_factory=list,
                                        description="适配车型 type_code 数组（与车型侧双向声明）")
    cert_marks: list[str] = Field(default_factory=list)
    hazard_class: str | None = None
    recall_batch_support: bool = True


class BatteryModelPatch(BaseModel):
    """只允许改描述与规格值；`model_code` 不可改（改规格 = 新增一行 + 旧行 retired）。"""

    name: str | None = Field(default=None, min_length=2, max_length=120)
    vendor: str | None = Field(default=None, min_length=2, max_length=120)
    spec_doc_media_id: int | None = None
    kwh_accept_min: float | None = Field(default=None, gt=0)
    mounting_drawing_media_id: int | None = None
    compatible_types: list[str] | None = None
    cert_marks: list[str] | None = None


class BatteryModelStatusIn(BaseModel):
    status: str = Field(pattern=f"^({MODEL_STATUS})$")
    note: str | None = Field(default=None, max_length=300)


# ── 换电站（§16.4）──────────────────────────────────────────────────────
class StationIn(BaseModel):
    code: str = Field(min_length=2, max_length=40)
    name: str = Field(min_length=2, max_length=120)
    org_id: int | None = Field(default=None, description="归属运营商；非平台身份默认取自己的换电 org")
    lng: float = Field(ge=-180, le=180)
    lat: float = Field(ge=-90, le=90)
    address: str | None = None
    region_id: str | None = Field(default=None, pattern="^[A-Z]{2}$")
    business_hours: dict = Field(default_factory=dict)
    allowed_models: list[str] = Field(default_factory=list,
                                      description="准入型号白名单（只能引用 active 型号）")
    bay_count: int | None = Field(default=None, gt=0, le=500)


class StationPatch(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=120)
    address: str | None = None
    business_hours: dict | None = None
    bay_count: int | None = Field(default=None, gt=0, le=500)
    status: str | None = Field(default=None, pattern=f"^({STATION_STATUS})$")


class AllowedModelsIn(BaseModel):
    """改准入白名单（**必须写审计**：谁、何时、加减了哪个型号）。"""

    add: list[str] = Field(default_factory=list)
    remove: list[str] = Field(default_factory=list)


# ── 电池台账（§16.3）────────────────────────────────────────────────────
class BatteryIn(BaseModel):
    battery_sn: str = Field(min_length=4, max_length=64)
    model_code: str = Field(min_length=3, max_length=40)
    station_id: int | None = Field(default=None, description="入池站点；池外型号在此就被拒")
    owner_org_id: int | None = Field(default=None, description="资产方=换电站运营商；非平台必填自己的")
    batch_no: str | None = None
    kwh_actual: float | None = Field(default=None, gt=0, description="入池验收实测容量")
    accept_media_id: int | None = Field(default=None, description="验收照片")
    note: str | None = Field(default=None, max_length=300)


class BatteryStatusIn(BaseModel):
    """人工改状态（故障隔离 / 退役 / 丢失登记）。换电引起的状态变化不走这里。

    `soc` 是**全站唯一的 SOC 写入口**（站点后台巡检登记，不来自遥测）：
    换电闸门⑤"换出电池 SOC 达标"读的就是这一列，没有它换电永远 `soc_below`。
    """

    to_status: str = Field(
        pattern="^(in_stock|charging|ready|returned|faulty|repair|retired|lost|stolen)$")
    station_id: int | None = None
    soc: int | None = Field(default=None, ge=0, le=100,
                            description="巡检登记的电量；置 ready 时必须有值（本列或本次传入）")
    note: str = Field(min_length=3, max_length=300)
    media_id: int | None = None


# ── 换电协议与押金（§16.6 / §16.7）──────────────────────────────────────
class LeaseIn(BaseModel):
    model_code: str = Field(min_length=3, max_length=40)
    terms_version: str = Field(min_length=1, max_length=40,
                               description="用户确认的条款版本；换型号需新协议")
    operator_org_id: int | None = Field(default=None, description="对手方运营商；只有一个候选时可省略")


class DepositRegisterIn(BaseModel):
    """登记"运营商已线下收到押金"——平台不经手钱，只登记凭证（P28）。"""

    amount: int | None = Field(default=None, ge=0, description="留空用协议里的档位快照")
    collected_voucher_no: str = Field(min_length=3, max_length=80)
    proof_media_id: int | None = None


class LeaseTerminateIn(BaseModel):
    reason: str = Field(min_length=3, max_length=300)
    refund_voucher_no: str | None = Field(default=None, max_length=80,
                                          description="线下退汇凭证号；给了就把押金置 refunded")


class SwapPriceIn(BaseModel):
    model_code: str = Field(min_length=3, max_length=40)
    region_id: str | None = Field(default=None, pattern="^[A-Z]{2}$")
    station_org_id: int | None = None
    price: int = Field(ge=0, description="最小货币单位（×100）")
    currency: str = Field(pattern="^[A-Z]{3}$")
    min_soc_pct: int = Field(default=95, ge=50, le=100)
    version: int = Field(ge=1)
    effective_from: date
    effective_to: date | None = None


class DepositRuleIn(BaseModel):
    model_code: str = Field(min_length=3, max_length=40)
    region_id: str | None = Field(default=None, pattern="^[A-Z]{2}$")
    amount: int = Field(ge=0)
    currency: str = Field(pattern="^[A-Z]{3}$")
    waiver_min_kyc: int = Field(default=3, ge=0, le=3)
    version: int = Field(ge=1)
    effective_from: date
    effective_to: date | None = None


class RuleApproveIn(BaseModel):
    approve: bool
    note: str | None = Field(default=None, max_length=300)


# ── 换电单（§16.8：四写同事务）──────────────────────────────────────────
class SwapIn(BaseModel):
    station_code: str = Field(min_length=2, max_length=40)
    vin: str = Field(min_length=8, max_length=17)
    out_battery_sn: str = Field(min_length=4, max_length=64, description="车上拆下来的旧电池")
    in_battery_sn: str = Field(min_length=4, max_length=64, description="换上去的满电电池")
    points_offset: int = Field(default=0, ge=0, description="积分抵扣（上限 20%，由平台补贴）")
    re_swap_of: int | None = Field(default=None, description="30 分钟内免费重换指向的原单 id")
    note: str | None = Field(default=None, max_length=300)


class OrgBusinessIn(BaseModel):
    """申报业务线与牌照（M15）：换电 scope 的唯一入口。"""

    org_id: int
    line: str = Field(pattern=f"^({BUSINESS_LINE})$")
    license_no: str | None = Field(default=None, max_length=80)
    license_expiry: date | None = None


class NearbyQuery(BaseModel):
    lng: float = Field(ge=-180, le=180)
    lat: float = Field(ge=-90, le=90)
    model_code: str | None = None
    radius_km: float = Field(default=10.0, gt=0.1, le=200)
    limit: int = Field(default=20, ge=1, le=100)


class TimeWindow(BaseModel):
    start: datetime | None = None
    end: datetime | None = None
