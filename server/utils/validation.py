from __future__ import annotations

import re
from uuid import UUID


_LANGUAGE_TAG_PATTERN = re.compile(
    r"(?:[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*|x(?:-[A-Za-z0-9]{1,8})+)"
)


def canonical_uuid(value: str, field_name: str) -> str:
    try:
        return str(UUID(value))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a UUID") from exc


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


def normalize_language_tag_hint(
    value: str | None,
    field_name: str = "transcript_language_hint",
) -> str | None:
    """Validate and normalize the common BCP-47 forms used for retrieval routing."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a BCP-47 language tag or null")
    tag = value.strip()
    if not tag:
        return None
    if len(tag) > 64 or _LANGUAGE_TAG_PATTERN.fullmatch(tag) is None:
        raise ValueError(f"{field_name} must be a valid BCP-47 language tag")

    parts = tag.split("-")
    if parts[0].casefold() == "x":
        return "-".join(part.lower() for part in parts)
    normalized = [parts[0].lower()]
    for part in parts[1:]:
        if len(part) == 4 and part.isalpha():
            normalized.append(part.title())
        elif (len(part) == 2 and part.isalpha()) or (
            len(part) == 3 and part.isdigit()
        ):
            normalized.append(part.upper())
        else:
            normalized.append(part.lower())
    return "-".join(normalized)
