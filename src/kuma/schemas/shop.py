"""商城（配件电商）入参。

**容器类型一律显式声明**：`list[str]` / `list[CheckoutLineIn]` 而不是裸 `dict`——
`Task-Plan.md` §5 的教训是裸 `str`/`dict` 会把非法值一路带到 service 里变成
`KeyError`/`TypeError`（`purchase_service.py:59-64` 的注释记着这次踩坑），
能在 422 挡住的就绝不放到下游。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

#: 与 DDL 的 `product.product_type` CHECK 逐字一致（`vehicle`/`used_vehicle` 走买车链，暂不开放）
SHOP_PRODUCT_TYPES = ("part", "service", "coupon")
#: 与 `commerce.sku.inventory_kind` CHECK 一致
INVENTORY_KINDS = ("qty", "serial")


class ProductIn(BaseModel):
    """建商品（`product:write`，经销商/厂家/平台）。"""

    name: str = Field(min_length=1, max_length=200)
    product_type: Literal["part", "service", "coupon"] = "part"
    category: str | None = Field(default=None, max_length=50, description="制动/轮胎/电池/保养…")
    currency: str = Field(default="THB", pattern="^[A-Z]{3}$")
    region_id: str | None = Field(default=None, pattern="^[A-Z]{2}$")
    msrp: int | None = Field(default=None, ge=0, description="建议零售价（最小货币单位）")
    manufacturer_org_id: int | None = None


class SkuIn(BaseModel):
    """建可售单位。`inventory_kind` 决定走数量库存还是 SN 台账（迁移 0013）。"""

    product_id: int = Field(gt=0)
    code: str = Field(min_length=3, max_length=60, pattern="^[A-Za-z0-9_.-]+$")
    price: int = Field(ge=0, description="单价（最小货币单位）")
    currency: str | None = Field(default=None, pattern="^[A-Z]{3}$")
    inventory_kind: Literal["qty", "serial"] = "qty"
    attrs: dict = Field(default_factory=dict, description="规格：颜色/容量/尺寸等")


class StockQtyIn(BaseModel):
    """数量库存增减（`part` 的入库与盘点）。负数=报损/手工扣减。"""

    sku_id: int = Field(gt=0)
    dealer_org_id: int | None = Field(default=None, description="缺省=当前登录的经销商组织")
    qty_delta: int


class StockUnitsIn(BaseModel):
    """SN 批量入库（`inventory_kind='serial'`）。重复 SN 会被跳过，不报错。"""

    sku_id: int = Field(gt=0)
    dealer_org_id: int | None = None
    sns: list[str] = Field(min_length=1, max_length=200)
    note: str | None = Field(default=None, max_length=200)


class CartItemIn(BaseModel):
    """加购 / 改数量。`qty=0` 等于移除（前端"减到 0"不该留僵尸行）。"""

    sku_code: str = Field(min_length=3, max_length=60)
    qty: int = Field(default=1, ge=0, le=99)


class CheckoutLineIn(BaseModel):
    sku_code: str = Field(min_length=3, max_length=60)
    qty: int = Field(default=1, ge=1, le=99)


class CheckoutIn(BaseModel):
    """下单。

    `items` 给了就按它下单（"立即购买"），不给就结购物车里全部（"去结算"）。
    两种路径都必须带 `dealer_org_id`：配件是**经销商的货**，没有卖方就不知道该找谁提货、
    也不知道钱该记在谁头上（`order.dealer_org_id` 是商家对账的唯一锚点）。
    """

    dealer_org_id: int = Field(gt=0)
    items: list[CheckoutLineIn] | None = Field(default=None, max_length=20)
    vin: str | None = Field(default=None, pattern=r"^[A-HJ-NPR-Z0-9]{17}$",
                            description="给哪台车买的（可选，质保与召回要用）")
    province: str | None = Field(default=None, max_length=50, description="收货省/府")
    note: str | None = Field(default=None, max_length=200)


class CancelIn(BaseModel):
    reason: str | None = Field(default=None, max_length=200)


class PickupConfirmIn(BaseModel):
    """到店提货核销（经销商扫码）。"""

    pickup_code: str = Field(min_length=6, max_length=32)
