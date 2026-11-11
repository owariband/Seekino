"""Inputs authored by an editor. Attribution and source snapshots belong to the server."""

import json
from pathlib import PurePosixPath, PureWindowsPath
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


class TemplateDetails(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=2000)
    tags: list[Annotated[str, Field(min_length=1, max_length=64)]] = Field(default_factory=list, max_length=16)


class TemplateEvaluation(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    kind: Literal["usage_feedback", "revision_rationale"] = "usage_feedback"
    about_version_id: str | None = Field(default=None, max_length=36)
    context: str | None = None
    user_feedback: str | list[str] | None = None
    agent_judgment: str | list[str] | None = None
    changes: str | list[str] | None = None
    verified: str | list[str] | None = None
    unverified: str | list[str] | None = None


def canonical_bytes(value) -> bytes:
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                          separators=(",", ":")).encode("utf-8")
    except (ValueError, TypeError, RecursionError) as error:
        raise ValueError("INVALID_ARGUMENT: Expected finite UTF-8 JSON.") from error


def validate_files(files: dict[str, str], entry: str) -> None:
    if entry not in files or not 1 <= len(files) <= 128:
        raise ValueError("INVALID_ARGUMENT: Supply 1–128 text files including the entry point.")
    size = 0
    folded = set()
    for name, text in files.items():
        path = PurePosixPath(name)
        if (not name or len(name) > 256 or path.as_posix() != name or path.is_absolute() or ".." in path.parts
                or not path.parts or "\\" in name or ":" in name or name.casefold() in folded
                or any(ord(c) < 32 or c in '<>"|?*' for c in name)
                or any(part.endswith((".", " ")) or PureWindowsPath(part).is_reserved() for part in path.parts)
                or "\x00" in text):
            raise ValueError("INVALID_ARGUMENT: Use portable relative text file paths.")
        folded.add(name.casefold())
        size += len(text.encode("utf-8"))
    if size > 1024 * 1024 or any(str(parent).casefold() in folded for name in files for parent in PurePosixPath(name).parents):
        raise ValueError("INVALID_ARGUMENT: Conflicting paths or files exceed one MiB.")


def validate_note(entry: dict, kind: str) -> dict:
    if not entry or len(canonical_bytes(entry)) > 8192:
        raise ValueError("INVALID_ARGUMENT: Supply a note of at most 8 KiB.")
    if kind == "evaluation":
        note = TemplateEvaluation.model_validate(entry).model_dump(exclude_none=True)
        if not any(note.get(field) for field in ("context", "user_feedback", "agent_judgment", "changes", "verified", "unverified")):
            raise ValueError("INVALID_ARGUMENT: Describe the feedback or the change.")
        return note
    if kind != "check" or not entry.get("what") or not entry.get("result"):
        raise ValueError("INVALID_ARGUMENT: A client check requires what and result.")
    return {key: value for key, value in entry.items() if key not in
            {"by", "author", "created_at", "reported_by", "request_id", "request_hash", "kind"}}
