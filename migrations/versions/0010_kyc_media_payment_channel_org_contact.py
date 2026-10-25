"""补前端阻塞项的库侧缺口（`Task-Web.md` §11 的 #2 / #3 / #7）

三件事都是"表在、列在，但**取值域或列本身缺**"，不是新建业务表：

## 1. `core.media.owner_type` 加 `payment_channel`（§11-3 收款二维码）

`purchase_service.buy()` 的注释早就写死："二维码走 `core.media(owner_type='payment_channel')`"
——因为 `fin.payment_channel` **没有存账号/二维码的列**（实测只有 `fee_rule` + `config_version`）。
但 `002_core.sql:241` 的 CHECK 里没有这个取值，`media_service.OWNER_TYPES` 也就加不进去：
**运营拿不到上传口，积分包收款页只能显示 `account_entity` 文本，没有二维码。**

CHECK 是内联未命名的 → PG 自动命名 `media_owner_type_check`，DROP 用这个名字。
加完取值域必须与 `services/media_service.py:OWNER_TYPES` **逐字一致**（那边注释也这么要求）。

## 2. `core.kyc_record` 补取值域与队列索引（§11-2 KYC）

表从 `002_core.sql:90` 就在（`level / id_type / id_number_hash / docs / status / reviewed_by`），
但**没有任何 CHECK、没有按状态的索引**，且全库零写入方（只被 GDPR 读与匿名化）：
买积分闸门 `kyc_level_for_points=2` 因此永远过不去——真实账号没有任何途径升到 2。

- `status` CHECK：`pending`（默认）/ `approved`（`uq_kyc_approved` 部分索引已按它过滤）/
  `rejected`（本轮审核用）/ `revoked`（`gdpr_service.py:202` 匿名化时写）。
  库里现存只可能是 `pending` 与 `revoked`（除 GDPR 外无人写过），加 CHECK 不会挡存量行。
- `level` CHECK `1..3`：对齐 `Task-Plan.md` §8.6-24 的 **KYC 三档**
  （1=手机号 OTP 级、2=当地基础证件级=买积分门槛、3=司机完整证件+驾照+背景）。
- `idx_kyc_queue (status, created_at)`：平台侧"待审核队列"要按状态翻页，没索引就是全表扫。
- `idx_kyc_account (account_id, id DESC)`：查"我的实名记录"。

**不给 `core.account.kyc_level` 加 CHECK**：集成测试为了绕过 KYC 闸门直接改过库里的等级
（`Task-Web.md` §11-2 记着这件事），CHECK 会让迁移在测试库上挂掉。
等级取值域由 `kyc_record.level` 的 CHECK + `schemas/kyc.py` 的 `ge=1 le=3` 双重保证。

## 3. `core.org` 加 `address / phone / lat / lng`（§11-7 门店联系方式）

`public_service.dealer_public()` 的注释写死了原因："core.org 没有地址/电话/坐标列——
官网要展示门店位置必须走迁移加列，不能在这里拼假数据"。portal 经销商页因此只能显示名称 + 国家。

坐标用 `double precision` + 范围 CHECK，**不引入 `geography`/`Point`**：
portal 只做"附近门店"列表与地图打点，`earth_distance` 那套 PostGIS 口径属于车联网围栏侧
（`geo/sql.py`），门店查询在 v1 的量级下 `lat/lng` 足够，也不给 `core` 表增加 PostGIS 依赖。

Revision ID: 0010_kyc_media_org_contact
Revises: 0009_points_award_pack
"""
from __future__ import annotations

from alembic import op

revision = "0010_kyc_media_org_contact"
down_revision = "0009_points_award_pack"
branch_labels = None
depends_on = None

#: 必须与 `services/media_service.py:OWNER_TYPES` 逐字一致
MEDIA_OWNER_TYPES = ("vehicle_type", "vehicle", "product", "sku", "order", "aftersale",
                     "driver_profile", "org", "account", "payment_channel")
#: `Task-Plan.md` §8.6-24 KYC 三档；`revoked` 由 GDPR 匿名化写入
KYC_STATUS = ("pending", "approved", "rejected", "revoked")


def _in_check(col: str, values: tuple[str, ...]) -> str:
    vals = ", ".join(f"'{v}'" for v in values)
    return f"CHECK ({col} IN ({vals}))"


def upgrade() -> None:
    # ══ 1. media：给收款二维码一个合法的挂载点 ══════════════════════════════════
    op.execute("ALTER TABLE core.media DROP CONSTRAINT IF EXISTS media_owner_type_check")
    op.execute("ALTER TABLE core.media ADD CONSTRAINT media_owner_type_check "
               f"{_in_check('owner_type', MEDIA_OWNER_TYPES)}")

    # ══ 2. KYC：把"能不能审"钉进库里 ═══════════════════════════════════════════
    op.execute("ALTER TABLE core.kyc_record ADD CONSTRAINT kyc_record_status_check "
               f"{_in_check('status', KYC_STATUS)}")
    op.execute("ALTER TABLE core.kyc_record "
               "ADD CONSTRAINT kyc_record_level_check CHECK (level BETWEEN 1 AND 3)")
    op.execute("""
        CREATE INDEX idx_kyc_queue ON core.kyc_record (status, created_at)
    """)
    op.execute("""
        CREATE INDEX idx_kyc_account ON core.kyc_record (account_id, id DESC)
    """)

    # ══ 3. org：门店联系方式与坐标（portal 经销商页）════════════════════════════
    op.execute("""
        ALTER TABLE core.org
          ADD COLUMN IF NOT EXISTS address text,
          ADD COLUMN IF NOT EXISTS phone text,
          ADD COLUMN IF NOT EXISTS lat double precision CHECK (lat BETWEEN -90 AND 90),
          ADD COLUMN IF NOT EXISTS lng double precision CHECK (lng BETWEEN -180 AND 180)
    """)


def downgrade() -> None:
    op.execute("""
        ALTER TABLE core.org
          DROP COLUMN IF EXISTS address,
          DROP COLUMN IF EXISTS phone,
          DROP COLUMN IF EXISTS lat,
          DROP COLUMN IF EXISTS lng
    """)
    op.execute("DROP INDEX IF EXISTS idx_kyc_account")
    op.execute("DROP INDEX IF EXISTS idx_kyc_queue")
    op.execute("ALTER TABLE core.kyc_record DROP CONSTRAINT IF EXISTS kyc_record_level_check")
    op.execute("ALTER TABLE core.kyc_record DROP CONSTRAINT IF EXISTS kyc_record_status_check")
    # 回到 002_core.sql 的原始取值域（去掉 payment_channel）
    op.execute("ALTER TABLE core.media DROP CONSTRAINT IF EXISTS media_owner_type_check")
    op.execute("ALTER TABLE core.media ADD CONSTRAINT media_owner_type_check "
               f"{_in_check('owner_type', MEDIA_OWNER_TYPES[:-1])}")
