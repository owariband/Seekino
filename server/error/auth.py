from __future__ import annotations


class AuthError(RuntimeError):
    def __init__(self, message: str, *, status_code: int = 401,
                 code: str = "AUTH_REQUIRED", retry_after_seconds: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.retry_after_seconds = retry_after_seconds


class IdentityConflictError(RuntimeError):
    pass


class IdentityDeniedError(PermissionError):
    pass
