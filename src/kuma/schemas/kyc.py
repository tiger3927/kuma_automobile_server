"""实名认证入参（`Task-Plan.md` §8.6-24 三档 KYC）。

**等级取值域 1..3 与库侧 CHECK 一致**（迁移 `0010`：`kyc_record.level BETWEEN 1 AND 3`）：
1 = 手机号 OTP 级（打车）、2 = 当地基础证件级（**买积分门槛**、换电免押）、
3 = 完整证件 + 驾照 + 背景审查（司机）。

**证件号明文不落库**：`id_number` 只用来算 HMAC-SHA256（`services/kyc_service.py`），
库里存 `id_number_hash`。证件照片走 `core.media(owner_type='account')` 先上传，
这里只传 `media_ids`——`media_service` 已经保证"只能传自己的"且校验字节头，
KYC 侧不重复实现一套上传，也不允许把 base64 塞进 JSON。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

#: 证件类型。库里 `id_type` 无 CHECK，但审核要看懂材料，取值必须先收敛
KycIdType = Literal["id_card", "passport", "driver_license", "residence_permit", "other"]


class KycSubmitIn(BaseModel):
    """提交实名认证（本人，`kyc:write`）。

    `level` 只能**往上申请**：已经授予过 2 级的人再提交 1 级会被拒
    （`kyc_level_already_granted`）——否则一次误操作就能把等级"重新申请"成待审状态。
    """

    level: int = Field(ge=1, le=3, description="1=OTP 级 / 2=基础证件（买积分、免押）/ 3=司机级")
    id_type: KycIdType
    id_number: str | None = Field(
        default=None, min_length=3, max_length=40,
        description="证件号明文：只用于算哈希，服务端不落库；level≥2 必填")
    media_ids: list[int] = Field(default_factory=list, max_length=8,
                                 description="已上传的证件照片（core.media.id，owner_type=account）")
    note: str | None = Field(default=None, max_length=200)


class KycReviewIn(BaseModel):
    """审核（平台，`kyc:review` ∈ DUAL_CONTROL）。

    驳回**必须给 reason**：没有理由的驳回用户无法补件，也没法申诉；
    审计里留不下依据，§9.5 的双人签字就只是形式。
    """

    approve: bool = True
    co_approver_account_id: int | None = None
    reason: str | None = Field(default=None, max_length=300)
