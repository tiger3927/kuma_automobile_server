"""认证与组织相关的 Pydantic v2 模型（与 ORM 分离）。"""

from __future__ import annotations

from pydantic import BaseModel, Field


class RegisterIn(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=8, max_length=128)
    phone: str | None = None
    email: str | None = None
    region_id: str | None = Field(default=None, min_length=2, max_length=2)
    display_name: str | None = None


class LoginIn(BaseModel):
    username: str
    password: str
    identity_type: str | None = None


class RefreshIn(BaseModel):
    refresh_token: str


class SwitchIdentityIn(BaseModel):
    identity_type: str


class OrgCreateIn(BaseModel):
    org_type: str
    code: str = Field(min_length=2, max_length=32)
    name: str
    region_id: str | None = None
    parent_code: str | None = None
    primary_account_id: int | None = None


class OrgContactIn(BaseModel):
    """改组织名称与联系方式（迁移 0010 给 `core.org` 加了 address/phone/lat/lng）。

    **只碰这几列**：`org_type` / 父子关系 / 主管理员各有自己的流程与权限
    （`/orgs`、`/orgs/{id}/primary-admin`），混在一个 PATCH 里就等于给
    `org:write` 开了改组织结构的路。
    坐标必须**成对**给（只给 lat 会让"附近门店"查询把这家店漏掉或算错）。
    v1 不支持把坐标清空（传 null = 不改）；文本字段清空传空串。
    """

    name: str | None = Field(default=None, min_length=1, max_length=128)
    address: str | None = Field(default=None, max_length=300)
    phone: str | None = Field(default=None, max_length=32)
    lat: float | None = Field(default=None, ge=-90, le=90)
    lng: float | None = Field(default=None, ge=-180, le=180)


class MemberIn(BaseModel):
    account_id: int
    role: str


class PrimaryAdminTransferIn(BaseModel):
    new_account_id: int
    co_approver_account_id: int | None = None
