"""实名认证服务（`Task-Plan.md` §8.6-24 三档；§7.2 买积分前置；§16.7 免押前置）。

**为什么这是"补后端"而不是新功能**：`core.kyc_record` 建库第一天就在，买积分闸门
`kyc_level_for_points=2` 也一直在读 `core.account.kyc_level`，但**全库没有写它们的端点**——
真实账号永远升不到 2，积分购买链在真机上不可用（集成测试是直接改库绕过的，`Task-Web.md` §11-2）。

三条立场：
1. **证件号明文不落库、不进审计**：只存 HMAC-SHA256（pepper 见 `config.id_hash_pepper`）。
   哈希必须确定性——§2.3-25"同证件多账号 → 关联标记"靠的就是同证同哈希；
   但裸 `sha256(证件号)` 能被撞库还原，所以必须带 pepper。
2. **等级只能往上申请、只能由平台审核抬高**：`raise_account_level` 带 `WHERE kyc_level < :lv`，
   审核通过不该有降别人等级的能力（清 0 只有 GDPR 匿名化做）。
3. **审核 ∈ DUAL_CONTROL**（`scope.py`）：一次通过 = 打开"现金买积分"与"免押换电"两道闸门。

证件照片不在这里上传：先走 `POST /media`（`owner_type='account'`，`media_service` 已保证
只能传自己的 + 字节头校验 + 5MB 上限），这里只认 `media_ids` 并复核归属。
"""

from __future__ import annotations

import hashlib
import hmac
import re
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.audit import write_audit
from ..core.config import get_settings
from ..core.exceptions import Conflict, InvalidState, NotFound, ValidationFailed
from ..core.scope import Principal
from ..models.core import Account
from ..repositories.kyc_repo import KycRepo
from .dual_control import require_co_approver

#: 等级取值域，与迁移 0010 的 CHECK 一致（§8.6-24 三档）
KYC_LEVELS = (1, 2, 3)
#: 队列可查的状态（`revoked` 是 GDPR 匿名化留下的空壳，只用于查历史）
KYC_STATUSES = ("pending", "approved", "rejected", "revoked")


def id_number_hash(raw: str) -> str:
    """证件号 → 确定性哈希（HMAC-SHA256）。

    归一化只去空白与连字符并转大写：各国证件号常见写法差异就在这两处。
    **不做**其它"纠错"（如 O/0 混淆）——那会让同一个人算出两个哈希，关联标记直接失效。
    """
    s = get_settings()
    key = (s.id_hash_pepper or s.jwt_secret).encode()
    norm = re.sub(r"[\s\-]", "", raw).upper()
    return hmac.new(key, norm.encode("utf-8"), hashlib.sha256).hexdigest()


class KycService:
    repo = KycRepo()

    # ── 本人侧：提交与查看 ────────────────────────────────────────────────────
    async def submit(self, session: AsyncSession, principal: Principal, *, level: int,
                     id_type: str, id_number: str | None = None,
                     media_ids: list[int] | None = None, note: str | None = None,
                     ip: str | None = None) -> dict[str, Any]:
        principal.require("kyc", "write")
        account = await self._account(session, principal.account_id)
        granted = int(account.kyc_level or 0)
        if level not in KYC_LEVELS:
            raise ValidationFailed(f"level 必须在 {list(KYC_LEVELS)}", code="kyc_level_invalid")
        if level <= granted:
            # 已拿过这个等级还"重新申请"，会把已生效的实名状态搅回待审——直接拒
            raise InvalidState(f"已授予 KYC {granted} 级，无需申请 {level} 级",
                               code="kyc_level_already_granted")

        existing = await self.repo.open_or_approved_at_level(session, principal.account_id, level)
        if existing is not None:
            code = "kyc_already_approved" if existing["status"] == "approved" \
                else "kyc_pending_exists"
            raise Conflict(f"该等级已有 {existing['status']} 的记录（#{existing['id']}）",
                           code=code)

        owned = await self.repo.media_of_account(session, principal.account_id,
                                                 list(media_ids or []))
        if len(owned) != len(set(media_ids or [])):
            raise ValidationFailed("media_ids 里有不属于你（或已删除）的文件",
                                   code="kyc_media_not_owned")
        # 2 级以上必须证件号 + 照片：这一档的定义就是"当地基础证件齐全"
        if level >= 2 and not id_number:
            raise ValidationFailed("申请 2 级以上必须提供证件号", code="kyc_id_number_required")
        if level >= 2 and not owned:
            raise ValidationFailed("申请 2 级以上必须上传证件照片", code="kyc_docs_required")

        docs: dict[str, Any] = {"media_ids": owned, "note": note}
        rec = await self.repo.insert(
            session, account_id=principal.account_id, level=level, id_type=id_type,
            id_number_hash=id_number_hash(id_number) if id_number else None, docs=docs)
        # 审计只记哈希前 8 位：审计表长期留存、读面广，把证件号（哪怕哈希全串）写进去
        # 等于把个人数据复制到第二个地方（GDPR/PDPA 最小化）
        await write_audit(session, actor=principal, action="kyc.submit",
                          resource_type="kyc_record", resource_id=rec["id"],
                          after={"level": level, "id_type": id_type,
                                 "id_number_hash8": (rec["id_number_hash"] or "")[:8],
                                 "media_ids": owned},
                          ip=ip)
        return rec

    async def my_records(self, session: AsyncSession, principal: Principal, *,
                         limit: int = 50, offset: int = 0) -> dict[str, Any]:
        principal.require("kyc", "read")
        account = await self._account(session, principal.account_id)
        rows, total = await self.repo.records_of_account(session, principal.account_id,
                                                         limit=limit, offset=offset)
        return {"kyc_level": int(account.kyc_level or 0), "items": rows, "total": total,
                "limit": limit, "offset": offset}

    # ── 平台侧：队列与审核 ────────────────────────────────────────────────────
    async def queue(self, session: AsyncSession, principal: Principal, *,
                    status: str = "pending", region_id: str | None = None,
                    level: int | None = None, limit: int = 50,
                    offset: int = 0) -> dict[str, Any]:
        principal.require("kyc", "review")
        if status not in KYC_STATUSES:
            raise ValidationFailed(f"status 必须在 {list(KYC_STATUSES)}", code="bad_status")
        rows, total = await self.repo.queue_page(session, status=status, region_id=region_id,
                                                 level=level, limit=limit, offset=offset)
        return {"items": rows, "total": total, "limit": limit, "offset": offset,
                "status": status, "region_id": region_id, "level": level}

    async def review(self, session: AsyncSession, principal: Principal, *, kyc_id: int,
                     approve: bool = True, co_approver_account_id: int | None = None,
                     reason: str | None = None, ip: str | None = None) -> dict[str, Any]:
        """通过 = 抬 `core.account.kyc_level`；驳回 = 允许用户补件重提。"""
        principal.require("kyc", "review")
        if principal.needs_dual_control("kyc", "review"):
            await require_co_approver(session, principal, co_approver_account_id)
        if not approve and not reason:
            raise ValidationFailed("驳回必须给 reason（用户要据此补件，审计要留依据）",
                                   code="review_reason_required")

        rec = await self.repo.record_by_id(session, kyc_id)
        if rec is None:
            raise NotFound("实名记录不存在", code="kyc_not_found")
        if rec["status"] != "pending":
            raise InvalidState(f"该记录已是 {rec['status']}", code="kyc_reviewed")

        docs: dict[str, Any] = dict(rec.get("docs") or {})
        docs["review_reason"] = reason
        docs["reviewed_by"] = principal.account_id
        status = "approved" if approve else "rejected"
        try:
            updated = await self.repo.set_review(session, kyc_id=kyc_id, status=status,
                                                 reviewer=principal.account_id, docs=docs)
        except IntegrityError as e:
            # `uq_kyc_approved`：同账号同等级已有另一条 approved（两个人同时批）
            await session.rollback()
            raise Conflict("该等级已有通过记录", code="kyc_already_approved") from e
        if updated is None:
            raise InvalidState("该记录刚被其他人处理过，请刷新后重试", code="kyc_reviewed")

        new_level = int(rec["account_kyc_level"])
        if approve:
            raised = await self.repo.raise_account_level(session, int(rec["account_id"]),
                                                         int(rec["level"]))
            if raised is not None:
                new_level = raised
        await write_audit(session, actor=principal, action="kyc.review",
                          resource_type="kyc_record", resource_id=kyc_id,
                          before={"status": "pending", "account_kyc_level":
                                  rec["account_kyc_level"]},
                          after={"status": status, "account_kyc_level": new_level,
                                 "reason": reason, "co_approver_account_id":
                                 co_approver_account_id},
                          ip=ip)
        return {"kyc_id": kyc_id, "status": status, "level": int(rec["level"]),
                "account_id": int(rec["account_id"]), "account_kyc_level": new_level}

    # ── 内部 ──────────────────────────────────────────────────────────────────
    async def _account(self, session: AsyncSession, account_id: int) -> Account:
        account = await session.get(Account, account_id)
        if account is None:
            raise NotFound("账号不存在", code="account_not_found")
        return account
