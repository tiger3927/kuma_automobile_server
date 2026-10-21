"""数据主体权利（PDPA TH / POPIA ZA / NDPR NG，`Task-Server.md` §10.5）：导出 + 匿名化。

**口径（2026-10-21 定）：匿名化，不物理删。**

为什么不是 `DELETE`：
- 审计与财务必须留（`Task-DataBase.md` §11.6：`audit_log` 保留期 ≥12 个月；
  `fin_*` / `points_ledger` 是 append-only，`kuma_app` 连 UPDATE 权限都没有）；
- 车辆侧数据（遥测、行程、里程）按 VIN 保留——它们首先是**设备与车辆**的事实，
  匿名化后不再能关联到自然人（`account` 行还在，但 `username/phone/email` 已清空）；
- 物理删要跨 9 个 schema 处理 FK，任何一处漏了就是外键事故；匿名化只改**归属字段**。

因此"删除"在这里的定义是：**让这条记录再也无法指向这个自然人**，并留下可复核的痕迹。

⚠️ 审计里**不写原值**：`before/after` 只记字段名与计数，否则行使删除权的同时
把手机号/邮箱复制进了永久保留的 `audit_log`，等于自己制造一个无法删除的副本。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.audit import write_audit
from ..core.config import get_settings
from ..core.exceptions import Conflict
from ..core.logging import get_logger
from ..core.scope import Principal
from ..models.audit import AuditLog
from ..models.biz import Trip
from ..models.core import (
    Account,
    Identity,
    KycRecord,
    Membership,
    Org,
    OtpCode,
    RefreshToken,
    UserProfile,
    Vehicle,
)
from ..models.tele import Telemetry
from ..services.media_service import media_service

log = get_logger("services.gdpr")

ERASED_STATUS = "erased"


class GdprService:

    # ── 导出（数据可携权 / 访问权）──────────────────────────────
    async def export_self(self, session: AsyncSession, principal: Principal) -> dict:
        """导出**与本人相关**的数据。只导本人 scope 内的，绝不跨账号。"""
        me = principal.account_id
        account = await session.get(Account, me)
        profile = await session.get(UserProfile, me)

        identities = (await session.execute(
            select(Identity).where(Identity.account_id == me).order_by(Identity.identity_type)
        )).scalars().all()

        memberships = (await session.execute(
            select(Membership, Org)
            .join(Org, Org.id == Membership.org_id)
            .where(Membership.account_id == me)
        )).all()

        kycs = (await session.execute(
            select(KycRecord).where(KycRecord.account_id == me).order_by(KycRecord.id)
        )).scalars().all()

        sessions_rows = (await session.execute(
            select(RefreshToken).where(RefreshToken.account_id == me)
            .order_by(RefreshToken.id.desc()).limit(50)
        )).scalars().all()

        vehicles = (await session.execute(
            select(Vehicle).where(Vehicle.owner_account_id == me).order_by(Vehicle.id)
        )).scalars().all()
        vins = [v.vin for v in vehicles]

        trip_count = (await session.execute(
            select(func.count()).select_from(Trip).where(Trip.owner_account_id == me)
        )).scalar_one()

        # 遥测只数**最近 N 天**：生产上 tele 是 17M 行/天量级（§3），
        # 全量 count(*) 会把 512MB 的库拖死；这里要的是"你有多少数据"的量级感，不是精确账。
        days = get_settings().tele_keep_days
        tele_count = 0
        if vins:
            since = datetime.now(UTC) - timedelta(days=days)
            tele_count = (await session.execute(
                select(func.count()).select_from(Telemetry)
                .where(Telemetry.vin.in_(vins), Telemetry.ts >= since)
            )).scalar_one()

        audit_count = (await session.execute(
            select(func.count()).select_from(AuditLog)
            .where(AuditLog.actor_account_id == me)
        )).scalar_one()

        media = await media_service.list_for(session, principal,
                                             owner_type="account", owner_id=me)

        return {
            "exported_at": datetime.now(UTC).isoformat(),
            "account_id": me,
            "account": None if account is None else {
                "username": account.username, "phone": account.phone, "email": account.email,
                "status": account.status, "kyc_level": account.kyc_level,
                "region_id": account.region_id, "last_login_at": account.last_login_at,
                "created_at": account.created_at,
            },
            "profile": None if profile is None else {
                "display_name": profile.display_name, "avatar_url": profile.avatar_url,
                "locale": profile.locale, "id_type": profile.id_type,
                "id_number_hash": profile.id_number_hash,
            },
            "identities": [{"identity_type": i.identity_type, "status": i.status,
                            "created_at": i.created_at} for i in identities],
            "memberships": [{"org_id": m.org_id, "org_name": o.name, "org_type": o.org_type,
                             "role": m.role, "status": m.status} for m, o in memberships],
            "kyc_records": [{"level": k.level, "id_type": k.id_type,
                             "id_number_hash": k.id_number_hash, "docs": k.docs,
                             "status": k.status, "created_at": k.created_at} for k in kycs],
            "sessions": [{"id": s.id, "device_id": s.device_id, "expires_at": s.expires_at,
                          "revoked_at": s.revoked_at} for s in sessions_rows],
            "vehicles_owned": [{"vin": v.vin, "status": v.status, "plate": v.plate,
                                "model_year": v.model_year} for v in vehicles],
            "trips": {"count": trip_count, "retained_after_erasure": True},
            "telemetry": {"count_last_n_days": tele_count, "window_days": days,
                          "retained_after_erasure": True,
                          "why": "遥测按 VIN 存储，匿名化后不再关联到自然人；"
                                 "原始点受保留期治理（retention 任务 DROP PARTITION）"},
            "media": [{"id": m.id, "role": m.role, "mime": m.mime, "size_bytes": m.size_bytes,
                       "created_at": m.created_at} for m in media],
            "audit_records": {
                "count": audit_count, "retained_after_erasure": True,
                "why": "监管要求操作留痕 ≥12 个月（Task-DataBase.md §11.6），"
                       "且 audit_log 对 kuma_app 是 append-only，无法改写",
            },
            "not_included": [
                "password_hash（哈希本身不外泄）",
                "他人订单/行程中涉及你的部分（需另行申请，涉及第三方个人数据）",
                "fin.* 结算与负债台账（法定保留）",
            ],
        }

    # ── 匿名化（删除权）────────────────────────────────────────
    async def erase_self(self, session: AsyncSession, principal: Principal, *,
                         reason: str | None, ip: str | None) -> dict:
        """把"这个账号"从所有个人数据里摘掉。**不可逆**。"""
        me = principal.account_id
        account = await session.get(Account, me)
        if account is None:
            raise Conflict("account_not_found")
        if account.status == ERASED_STATUS:
            raise Conflict("already_erased")

        now = datetime.now(UTC)
        # 先留一份"改了哪些字段"的清单（只记字段名，不记原值）
        touched: list[str] = []

        # 1) 账号本身：username 有 UNIQUE NOT NULL，必须留一个不会撞车的占位；
        #    phone/email 是 UNIQUE 可空 → 置 NULL（多个 NULL 不违反唯一约束）
        before_fields = [f for f in ("username", "phone", "email", "password_hash")
                         if getattr(account, f) is not None]
        account.username = f"__erased_{me}"
        account.phone = None
        account.email = None
        account.password_hash = None          # 无法再登录
        account.status = ERASED_STATUS        # resolve_principal 会拒（status != active）
        account.kyc_level = 0
        account.last_login_at = None
        touched.append(f"account({','.join(before_fields)})")

        # 2) 身份与组织关系：置失效，不删行（FK 与历史归属要能追）
        await session.execute(
            update(Identity).where(Identity.account_id == me, Identity.status == "active")
            .values(status="revoked"))
        await session.execute(
            update(Membership).where(Membership.account_id == me, Membership.status == "active")
            .values(status="inactive"))
        touched += ["identity→revoked", "membership→inactive"]

        # 3) 档案与 KYC：证件号哈希也清（哈希仍是个人数据，能被撞库还原）
        profile = await session.get(UserProfile, me)
        if profile is not None:
            profile.display_name = None
            profile.avatar_url = None
            profile.id_type = None
            profile.id_number_hash = None
            touched.append("user_profile")
        kyc_rows = (await session.execute(
            select(KycRecord).where(KycRecord.account_id == me))).scalars().all()
        for k in kyc_rows:
            k.id_type = None
            k.id_number_hash = None
            k.docs = None
            k.status = "revoked"
        if kyc_rows:
            touched.append(f"kyc_record({len(kyc_rows)})")

        # 4) 会话与验证码：全部作废（留着就是凭据残留）
        await session.execute(
            update(RefreshToken).where(RefreshToken.account_id == me,
                                       RefreshToken.revoked_at.is_(None))
            .values(revoked_at=now))
        await session.execute(
            update(OtpCode).where(OtpCode.used_at.is_(None))
            .values(used_at=now, target="__erased", attempts=0))
        touched += ["refresh_token→revoked", "otp_code→used/target 清空"]

        # 5) 组织主账号指针：不能留一个已匿名的人当 primary
        await session.execute(
            update(Org).where(Org.primary_account_id == me).values(primary_account_id=None))
        touched.append("org.primary_account_id→NULL")

        # 6) 媒体：行软删 + **磁盘文件真删**（照片是最典型的个人数据）
        media_stats = await media_service.erase_account_media(session, me)
        touched.append(f"media({media_stats['media_rows']} 行 / "
                       f"{media_stats['files_removed']} 个文件已删)")

        # 7) 审计：只记"动了哪些字段"，绝不记原值
        await write_audit(session, actor=principal, action="account.erase",
                          resource_type="account", resource_id=me,
                          before={"fields": before_fields},
                          after={"status": ERASED_STATUS, "touched": touched,
                                 "reason": reason},
                          ip=ip)

        await session.flush()
        log.warning("account.erased", account_id=me, touched=len(touched))
        return {
            "account_id": me, "status": ERASED_STATUS, "erased_at": now.isoformat(),
            "touched": touched, "media": media_stats,
            "retained": {
                "audit_log": "监管留痕 ≥12 个月，append-only 无法改写",
                "fin.* / points_ledger": "法定财务保留",
                "telemetry / trip / vehicle": "按 VIN 保留，已无法关联到本人",
            },
            "warning": "不可逆：该账号立即无法登录，且无法通过本接口恢复",
        }


gdpr_service = GdprService()
