from __future__ import annotations

from typing import Literal

from ...schema import ContractModel


class AuthPermissions(ContractModel):
    can_upload: bool
    can_manage: bool
    can_configure: bool


class AuthIdentityView(ContractModel):
    user_id: str
    email: str | None
    is_guest: bool
    tenant_id: str | None
    public_tenant_id: str
    mode: Literal["local", "cloud"]
    csrf_token: str
    permissions: AuthPermissions


class EmailCodeSendView(ContractModel):
    retry_after_seconds: int
