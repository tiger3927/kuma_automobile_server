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


class MemberIn(BaseModel):
    account_id: int
    role: str


class PrimaryAdminTransferIn(BaseModel):
    new_account_id: int
    co_approver_account_id: int | None = None
