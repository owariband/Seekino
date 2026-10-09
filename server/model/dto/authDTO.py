from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from pydantic import Field

from ...schema import ContractModel


@dataclass(frozen=True, slots=True)
class AuthIdentity:
    user_id: str
    email: str | None
    is_guest: bool
    tenant_id: str | None
    public_tenant_id: str
    role: str
    auth_version: int
    auth_level: Literal["guest", "email", "local"]
    mode: Literal["local", "cloud"]


@dataclass(frozen=True, slots=True)
class AuthCredentials:
    identity: AuthIdentity
    token: str = field(repr=False)
    csrf_token: str = field(repr=False)
    expires_at: int


class EmailCodeSendRequest(ContractModel):
    email: str = Field(min_length=3, max_length=254)


class EmailCodeVerifyRequest(EmailCodeSendRequest):
    code: str = Field(pattern=r"^[0-9]{6}$")
