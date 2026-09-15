from __future__ import annotations


def require_nonempty_text(
    value: str,
    field_name: str,
    *,
    maximum: int | None = None,
) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    if maximum is not None and len(value) > maximum:
        raise ValueError(f"{field_name} must be at most {maximum} characters")
    return value
