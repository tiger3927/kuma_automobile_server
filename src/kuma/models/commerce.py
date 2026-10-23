"""`commerce` schema：**本轮只映射积分包购买链路要用的五张**（`Task-DataBase.md` §11.2 / 口径 §5.1）。

`commerce` 12 张表里其余（`vehicle_listing` / `stock` / `cart_item` / `order_deposit` /
`aftersale` / `used_car_report` / `product_review`）尚无写入方，故意不映射——
映射了没有写入路径的表只会诱导出"看起来已实现"的假接口（`Task-Server.md` §16 C10 同一立场）。

**为什么积分包非要走这套骨架**（§5.1 自洽要点 2）：现金买积分必须走同一订单、同一支付核销、
同一退款规则、同一发票口径。否则"现金换积分"就成了一条游离在财务与合规之外的旁路——
这类生态最容易被审计卡住的地方。所以 `points_purchase.order_id` 是 NOT NULL FK，
而 `order_item.sku_id` 也是 NOT NULL ⇒ **配一个积分包 = 落一条 product + 一条 sku**（锚点在
`mkt.points_pack.sku_id`，迁移 0009）。

收款模式写死：积分包是**平台自营**商品 → `collect_mode = 'platform_online'`（§6.4 五条铁律第 2 条：
平台自己是卖方的交易才允许线上收款，钱进平台、天然无需二次分发）。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    CHAR,
    BigInteger,
    DateTime,
    SmallInteger,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class Product(Base):
    """商品主数据。`product_type` 的取值域在 DDL CHECK 里，含 `points_pack` / `points_item`。"""

    __tablename__ = "product"
    __table_args__ = {"schema": "commerce"}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    product_type: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str | None] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    type_code: Mapped[str | None] = mapped_column(Text)
    manufacturer_org_id: Mapped[int | None] = mapped_column(BigInteger)
    msrp: Mapped[int | None] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    region_id: Mapped[str | None] = mapped_column(CHAR(2))
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'draft'"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False,
                                                 server_default=text("now()"))


class Sku(Base):
    """可售单位（价格在这里）。积分包的 sku 由平台配包时自动创建，不手工维护。"""

    __tablename__ = "sku"
    __table_args__ = {"schema": "commerce"}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    product_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    code: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    attrs: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'"))
    price: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'active'"))


class Order(Base):
    """订单（表名 `order` 是保留字，SQLAlchemy 会自动加引号）。

    状态机见 §5.2：虚拟线（`points_pack` / `service` / `coupon`）是
    `created → paid → provisioning → completed`（自动）↘ `refunding → refunded`。
    `idempotency_key` UNIQUE 是弱网重试的防线（与 `energy.swap_order` 同一手法）。
    """

    __tablename__ = "order"
    __table_args__ = {"schema": "commerce"}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    order_no: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    account_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    order_type: Mapped[str | None] = mapped_column(Text)
    dealer_org_id: Mapped[int | None] = mapped_column(BigInteger)
    operator_org_id: Mapped[int | None] = mapped_column(BigInteger)
    region_id: Mapped[str] = mapped_column(CHAR(2), nullable=False)
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    amount_cash: Mapped[int] = mapped_column(BigInteger, nullable=False)
    amount_points: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    collect_mode: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(Text, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False,
                                                 server_default=text("now()"))
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OrderItem(Base):
    """订单行。`sku_id` NOT NULL 是"积分包必须是真商品"的硬约束落点。"""

    __tablename__ = "order_item"
    __table_args__ = {"schema": "commerce"}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    order_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sku_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    product_type: Mapped[str] = mapped_column(Text, nullable=False)
    vin: Mapped[str | None] = mapped_column(CHAR(17))
    qty: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("1"))
    unit_price: Mapped[int] = mapped_column(BigInteger, nullable=False)
    points_cost: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))


class Fulfillment(Base):
    """履约。虚拟商品走 `type='digital'`：支付成功即 `delivered`，没有取货码与物流。"""

    __tablename__ = "fulfillment"
    __table_args__ = {"schema": "commerce"}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    order_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    type: Mapped[str | None] = mapped_column(Text)
    dealer_org_id: Mapped[int | None] = mapped_column(BigInteger)
    pickup_code: Mapped[str | None] = mapped_column(Text, unique=True)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    confirmed_by: Mapped[int | None] = mapped_column(BigInteger)


class PaymentChannel(Base):
    """收款渠道配置（§6.4"改行不改代码"）。用户端要显示收款指引，所以只读它。"""

    __tablename__ = "payment_channel"
    __table_args__ = (
        UniqueConstraint("channel_code", name="uq_payment_channel_code"),
        {"schema": "fin"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    channel_code: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    provider_type: Mapped[str] = mapped_column(Text, nullable=False)
    regions: Mapped[list] = mapped_column(JSONB, nullable=False)
    currencies: Mapped[list] = mapped_column(JSONB, nullable=False)
    collect_mode: Mapped[str] = mapped_column(Text, nullable=False)
    account_entity: Mapped[str] = mapped_column(Text, nullable=False)
    fee_rule: Mapped[dict | None] = mapped_column(JSONB)
    config: Mapped[dict | None] = mapped_column(JSONB)
    config_version: Mapped[int] = mapped_column(SmallInteger, nullable=False,
                                                server_default=text("1"))
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'off'"))
