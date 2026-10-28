from __future__ import annotations

from dataclasses import dataclass, field
import os
import re
from typing import Literal

from ..constants.tenant import DEFAULT_LOCAL_TENANT_ID, DEFAULT_PUBLIC_TENANT_ID
from .email import SMTPConfig


@dataclass(frozen=True, slots=True)
class AuthConfig:
    mode: Literal["local", "cloud"] = "local"
    secret_key: str = field(default="", repr=False)
    redis_url: str = field(default="", repr=False)
    redis_timeout_seconds: int = 5
    cookie_name: str = "seekino_auth"
    cookie_secure: bool = True
    token_ttl_seconds: int = 7 * 24 * 60 * 60
    public_tenant_id: str = DEFAULT_PUBLIC_TENANT_ID
    local_tenant_id: str = DEFAULT_LOCAL_TENANT_ID
    otp_ttl_seconds: int = 300
    otp_attempt_limit: int = 5
    send_cooldown_seconds: int = 60
    send_window_seconds: int = 3600
    send_limit_per_email: int = 5
    send_limit_per_guest: int = 10
    send_limit_per_ip: int = 30
    verify_window_seconds: int = 300
    verify_limit_per_email: int = 20
    verify_limit_per_guest: int = 30
    verify_limit_per_ip: int = 100
    guest_window_seconds: int = 3600
    guest_limit_per_ip: int = 30
    query_window_seconds: int = 86400
    query_limit_guest: int = 20
    query_limit_user: int = 200
    concurrent_queries_guest: int = 1
    concurrent_queries_user: int = 3
    query_lease_seconds: int = 600
    guest_max_model_calls: int = 4
    admin_emails: tuple[str, ...] = ()
    smtp: SMTPConfig = field(default_factory=SMTPConfig)
    public_library_only: bool = False

    def __post_init__(self) -> None:
        if self.mode not in {"local", "cloud"}:
            raise ValueError("auth mode must be local or cloud")
        if not isinstance(self.public_library_only, bool):
            raise ValueError("public_library_only must be a boolean")
        if self.mode == "cloud" and len(self.secret_key.encode("utf-8")) < 32:
            raise ValueError("cloud authentication requires a secret of at least 32 bytes")
        for name in ("cookie_name", "public_tenant_id", "local_tenant_id"):
            value = getattr(self, name)
            if not value or value != value.strip() or len(value) > 64:
                raise ValueError(f"{name} must be a nonempty identifier of at most 64 characters")
        if self.public_tenant_id == self.local_tenant_id:
            raise ValueError("public and local tenants must differ")
        if not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", self.cookie_name, flags=re.ASCII):
            raise ValueError("cookie_name must be a valid cookie token")
        for name in (
            "redis_timeout_seconds", "token_ttl_seconds", "otp_ttl_seconds", "otp_attempt_limit",
            "send_cooldown_seconds", "send_window_seconds", "send_limit_per_email",
            "send_limit_per_guest", "send_limit_per_ip", "verify_window_seconds",
            "verify_limit_per_email", "verify_limit_per_guest", "verify_limit_per_ip",
            "guest_window_seconds", "guest_limit_per_ip",
            "query_window_seconds", "query_limit_guest", "query_limit_user",
            "concurrent_queries_guest", "concurrent_queries_user", "query_lease_seconds",
            "guest_max_model_calls",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")

    @classmethod
    def from_environment(cls) -> AuthConfig:
        defaults = cls()
        values = {}
        for name in (
            "redis_timeout_seconds", "token_ttl_seconds", "otp_ttl_seconds", "otp_attempt_limit",
            "send_cooldown_seconds", "send_window_seconds", "send_limit_per_email",
            "send_limit_per_guest", "send_limit_per_ip", "verify_window_seconds",
            "verify_limit_per_email", "verify_limit_per_guest", "verify_limit_per_ip",
            "guest_window_seconds", "guest_limit_per_ip",
            "query_window_seconds", "query_limit_guest", "query_limit_user",
            "concurrent_queries_guest", "concurrent_queries_user", "query_lease_seconds",
            "guest_max_model_calls",
        ):
            values[name] = int(os.getenv(f"SEEKINO_AUTH_{name.upper()}", str(getattr(defaults, name))))
        def enabled(name: str, default: bool) -> bool:
            value = os.getenv(name, str(default)).strip().lower()
            if value not in {"true", "false", "1", "0"}:
                raise ValueError(f"{name} must be true or false")
            return value in {"true", "1"}
        return cls(
            mode=os.getenv("SEEKINO_AUTH_MODE", defaults.mode),
            public_library_only=enabled("SEEKINO_PUBLIC_LIBRARY_ONLY", defaults.public_library_only),
            secret_key=os.getenv("SEEKINO_AUTH_SECRET_KEY", ""),
            redis_url=os.getenv("SEEKINO_AUTH_REDIS_URL", ""),
            cookie_name=os.getenv("SEEKINO_AUTH_COOKIE_NAME", defaults.cookie_name),
            cookie_secure=enabled("SEEKINO_AUTH_COOKIE_SECURE", defaults.cookie_secure),
            public_tenant_id=os.getenv("SEEKINO_PUBLIC_TENANT_ID", defaults.public_tenant_id),
            local_tenant_id=os.getenv("SEEKINO_LOCAL_TENANT_ID", defaults.local_tenant_id),
            admin_emails=tuple(value.strip().lower() for value in os.getenv("SEEKINO_AUTH_ADMIN_EMAILS", "").split(",") if value.strip()),
            smtp=SMTPConfig(
                host=os.getenv("SEEKINO_SMTP_HOST", ""),
                port=int(os.getenv("SEEKINO_SMTP_PORT", "587")),
                username=os.getenv("SEEKINO_SMTP_USERNAME", ""),
                password=os.getenv("SEEKINO_SMTP_PASSWORD", ""),
                sender=os.getenv("SEEKINO_SMTP_SENDER", ""),
                use_ssl=enabled("SEEKINO_SMTP_USE_SSL", False),
                start_tls=enabled("SEEKINO_SMTP_START_TLS", True),
                timeout_seconds=float(os.getenv("SEEKINO_SMTP_TIMEOUT_SECONDS", "10")),
            ),
            **values,
        )
