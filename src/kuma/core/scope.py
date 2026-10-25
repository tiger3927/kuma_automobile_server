"""权限模型：权限 = 身份 × 资源 × 动作 × 数据范围（Task-Plan.md §2.2 / §10.1）。

三条实现约束（写死，不可绕过）：
1. token 只带 `active_identity` + `identity_id`，**scope 每次请求重新解析**；
2. 列表查询必须经 repository 层的 scope 过滤器（见 `repositories/base.py`），
   **禁止**在 api 层手写 `WHERE org_id = :id`；
3. 身份切换只换 `active_identity`：积分/订单跨身份共享，车辆数据按 owner 归属不共享给 driver。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

IdentityType = Literal[
    "consumer", "owner", "driver", "dealer_staff", "manufacturer_staff",
    "operator_staff", "regulator", "platform_staff",
]
Action = Literal["read", "write", "verify", "review", "config"]
OrgType = Literal["manufacturer", "dealer", "operator", "government"]

# 资源名（与 §10.1 矩阵行一一对应；新增资源必须同时补矩阵，否则默认拒绝）
R_SELF = "self"                       # 自己的账号/积分/订单
R_KYC = "kyc"                         # 实名认证：本人提交、平台审核（买积分/免押的前置）
R_VEHICLE_LIVE = "vehicle_live"       # 车辆实时状态
R_VEHICLE_TRACE = "vehicle_trace"     # 轨迹 / 历史 trip
R_VEHICLE_MASTER = "vehicle_master"   # 车辆主数据（VIN、绑定、T-Box）
R_COMMAND = "command_down"            # 下行控制
R_PRODUCT = "product"
R_ORDER = "order"
R_PAYMENT = "payment"                 # 发起 + 回填转账凭证
R_PAYOUT_VERIFY = "payout_verify"     # 收款核销队列
R_POINTS = "points"                   # 购买 / 抵扣
R_POINTS_LIABILITY = "points_liability"
R_POINTS_RULE = "points_rule"
R_DRIVER_REVIEW = "driver_review"
R_FLEET_CONTRACT = "fleet_contract"
R_TARIFF = "tariff"
R_DISPATCH_POLICY = "dispatch_policy"
R_POOL = "pool"
R_SETTLEMENT = "settlement"
R_ORG = "org"
R_REPORT = "report"
R_AUDIT = "audit"
# ── 换电与电池资产（M15，Task-Plan.md §10.1 换电六行）─────────────────────
R_BATTERY_MODEL = "battery_model"   # 型号字典（型号→度数→单价，也是准入键）：平台录入与审核
R_STATION = "station"               # 换电站主数据 + 准入型号白名单：运营商自有，改白名单写审计
R_BATTERY = "battery"               # SN 级电池台账与流转流水：运营商自有池
R_SWAP_LEASE = "swap_lease"         # 换电协议（押金是生效前置）
R_SWAP = "swap"                     # 换电单（发起 + 站点作业台执行）
R_DEPOSIT = "deposit"               # 押金规则与押金台账（负债，不是收入）

_RW = {"read", "write"}
_R = {"read"}

# §10.1 权限矩阵（生成源；docs/permissions.md 由此导出）
PERMISSIONS: dict[str, dict[IdentityType, set[str]]] = {
    R_SELF: {i: _RW for i in (
        "consumer", "owner", "driver", "dealer_staff", "manufacturer_staff", "operator_staff")}
    | {"regulator": set(), "platform_staff": _R},
    # 实名认证（`Task-Plan.md` §8.6-24 三档）。`write` = 提交/重提自己的记录；
    # **`review` 只有平台有**——审核通过会直接抬高 `core.account.kyc_level`，
    # 而它是买积分（§7.2）与换电免押（§16.7）的闸门，所以 review 进 DUAL_CONTROL。
    # regulator 只读：监管要能查"这个平台做没做实名"，但不能改等级。
    R_KYC: {i: _RW for i in (
        "consumer", "owner", "driver", "dealer_staff", "manufacturer_staff", "operator_staff")}
    | {"regulator": _R, "platform_staff": _RW | {"review"}},
    R_VEHICLE_LIVE: {
        "owner": _RW, "driver": _R, "dealer_staff": _R, "manufacturer_staff": _R,
        "operator_staff": _R, "regulator": _R, "platform_staff": _R, "consumer": set()},
    R_VEHICLE_TRACE: {
        "owner": _R, "operator_staff": _R, "dealer_staff": _R, "manufacturer_staff": _R,
        "driver": _R, "regulator": _R, "platform_staff": _R},
    R_VEHICLE_MASTER: {
        "manufacturer_staff": _RW, "dealer_staff": _RW, "operator_staff": _RW,
        "platform_staff": _RW, "owner": _R},
    R_COMMAND: {
        "owner": _RW, "driver": _RW, "operator_staff": _RW,
        "platform_staff": _RW,   # 双人授权（dual control）
    },
    R_PRODUCT: {
        "consumer": _R, "owner": _R, "driver": _R,
        "dealer_staff": _RW, "manufacturer_staff": _RW, "platform_staff": {"review"}},
    R_ORDER: {
        "consumer": _RW, "owner": _RW, "driver": _R, "dealer_staff": _RW,
        "manufacturer_staff": _R, "operator_staff": _R, "regulator": _R,
        "platform_staff": _R | {"write"}},
    R_PAYMENT: {
        "consumer": _RW, "owner": _RW, "dealer_staff": _RW, "operator_staff": _RW,
        "platform_staff": _RW},
    R_PAYOUT_VERIFY: {"dealer_staff": _R, "operator_staff": _R, "platform_staff": _RW},
    R_POINTS: {i: _RW for i in ("consumer", "owner", "driver")}
    | {"dealer_staff": _R, "operator_staff": _R, "regulator": _R, "platform_staff": {"config"}},
    R_POINTS_LIABILITY: {"regulator": _R, "platform_staff": _RW},
    R_POINTS_RULE: {"platform_staff": _RW},          # 双人授权
    R_DRIVER_REVIEW: {"driver": {"write"}, "dealer_staff": _R,
                      "operator_staff": _RW, "regulator": _R, "platform_staff": {"review"}},
    R_FLEET_CONTRACT: {"owner": _RW, "driver": _R, "operator_staff": _RW,
                       "regulator": _R, "platform_staff": {"review"}},
    R_TARIFF: {"operator_staff": _RW, "regulator": _R, "platform_staff": {"review"}},
    R_DISPATCH_POLICY: {"operator_staff": _RW, "regulator": _R,
                        "platform_staff": {"review", "config"}},
    R_POOL: {"consumer": _R, "owner": _R, "driver": _R, "operator_staff": _RW, "regulator": _R},
    R_SETTLEMENT: {i: _R for i in ("consumer", "owner", "driver", "dealer_staff",
                                   "manufacturer_staff", "operator_staff", "regulator")}
    | {"platform_staff": _RW},
    R_ORG: {"manufacturer_staff": _RW, "dealer_staff": _RW, "operator_staff": _RW,
            "platform_staff": _RW, "regulator": _R},
    R_REPORT: {"dealer_staff": _R, "manufacturer_staff": _R, "operator_staff": _R,
               "regulator": _R, "platform_staff": _RW},
    R_AUDIT: {"platform_staff": _R, "regulator": _R},
    # ── 换电与电池资产（§10.1 换电六行；scope 见 repositories/base.py:station_scope）──
    R_BATTERY_MODEL: {"platform_staff": _RW | {"review"}, "operator_staff": _R,
                      "dealer_staff": _R, "regulator": _R},
    R_STATION: {"operator_staff": _RW, "platform_staff": _R | {"review"}, "regulator": _R,
                "consumer": _R, "owner": _R, "driver": _R},
    R_BATTERY: {"operator_staff": _RW, "owner": _R, "driver": _R,
                "platform_staff": _R | {"write"}, "regulator": _R},
    R_SWAP_LEASE: {"consumer": _RW, "owner": _RW, "operator_staff": _RW,
                   "platform_staff": _R | {"config"}, "regulator": _R},
    R_SWAP: {"consumer": _RW, "owner": _RW, "driver": _RW, "operator_staff": _RW,
             "platform_staff": _R | {"write"}, "regulator": _R},
    R_DEPOSIT: {"consumer": _R, "owner": _R, "operator_staff": _RW,
                "platform_staff": _R | {"review"}, "regulator": _R},
}

# 需要双人授权的高危动作（Task-Plan.md §9.5 / §10.1）
DUAL_CONTROL: set[tuple[str, str]] = {
    (R_COMMAND, "write"), (R_POINTS_RULE, "write"), (R_PAYOUT_VERIFY, "write"),
    # 审核通过 = 抬高 kyc_level = 打开"现金买积分"与"免押换电"两道闸门（§7.2 / §16.7）。
    # 一个人就能改的话，内部账号给自己刷个 2 级就绕过了 §7.4 的整套资金风控。
    (R_KYC, "review"),
}


def grants(identity: IdentityType, resource: str) -> set[str]:
    return PERMISSIONS.get(resource, {}).get(identity, set())


@dataclass(slots=True)
class OrgRef:
    org_id: int
    org_type: OrgType
    role: str            # primary_admin | admin | agent | auditor
    level: int
    path: str
    # M15：该 org 的**业务线**（core.org_business.line，active 行）。
    # org_type 是单值"主业态"，多业态（同一家公司既做网约车又做换电）只能靠这里表达。
    business_lines: list[str] = field(default_factory=list)


@dataclass(slots=True)
class Principal:
    """一次请求的身份快照（由 auth 依赖从 DB 解析，绝不信任前端传 org_id）。"""

    account_id: int
    identity_id: int
    active_identity: IdentityType
    identities: list[str] = field(default_factory=list)
    orgs: list[OrgRef] = field(default_factory=list)
    kyc_level: int = 0
    status: str = "active"

    # ── 权限判定 ────────────────────────────────────────────────
    def can(self, resource: str, action: str = "read") -> bool:
        if self.status != "active":
            return False
        return any(action in grants(i, resource) for i in self.identities)  # type: ignore[arg-type]

    def require(self, resource: str, action: str = "read") -> None:
        if not self.can(resource, action):
            from .exceptions import Forbidden

            raise Forbidden(f"{self.active_identity} 无 {resource}:{action} 权限")

    def needs_dual_control(self, resource: str, action: str = "read") -> bool:
        return (resource, action) in DUAL_CONTROL and "platform_staff" in self.identities

    # ── scope 解析（§2.2）───────────────────────────────────────
    def org_ids(self, org_type: OrgType | None = None) -> list[int]:
        return [o.org_id for o in self.orgs if org_type is None or o.org_type == org_type]

    def org_ids_for_line(self, line: str) -> list[int]:
        """按**业务线**取 org（M15）。

        与 `org_ids(org_type)` 的分工：后者按单值 `org_type`（主业态）过滤，是 M1–M2 的既有口径，
        不动它以免波及车联网；换电侧必须按业务线判定——一家公司同时是网约车运营商和换电站运营商时，
        `org_type` 只能记一个，按 org_type 过滤会漏（`Task-Server.md` §16 C12）。
        业务线只在 `core.org_business` 有 active 行时出现，**没有隐式默认**：
        没申报换电业务线的 org 拿不到换电 scope（牌照与业务线是线下准入的线上事实）。
        """
        return [o.org_id for o in self.orgs if line in o.business_lines]

    def is_primary_admin(self, org_id: int | None = None) -> bool:
        return any(o.role == "primary_admin" and (org_id is None or o.org_id == org_id)
                   for o in self.orgs)

    @property
    def is_platform(self) -> bool:
        return "platform_staff" in self.identities

    @property
    def is_regulator(self) -> bool:
        return "regulator" in self.identities

    def scope_kind(self) -> str:
        """当前 active_identity 对应的数据范围标签（写审计与前端展示用）。"""
        if self.is_platform:
            return "all"
        if self.is_regulator:
            return "all_readonly"
        match self.active_identity:
            case "dealer_staff":
                return "my_dealer"
            case "manufacturer_staff":
                return "my_manufacturer"
            case "operator_staff":
                # M15：有换电业务线时标签换成 my_station（换电侧的数据范围按站点收敛）
                return "my_station" if self.org_ids_for_line("swap_operator") else "my_operator"
            case "driver":
                return "self"
            case _:
                return "self"
