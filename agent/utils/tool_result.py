from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
import json
from typing import Any

from gateway.media import ToolImageArtifact


@dataclass(frozen=True, slots=True, kw_only=True)
class ToolError:
    code: str
    message: str
    retryable: bool
    phase: str | None = None
    retry_hint: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            **({"phase": self.phase} if self.phase is not None else {}),
            **(
                {"retry_hint": self.retry_hint}
                if self.retry_hint is not None else {}
            ),
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class ToolResult(ABC):
    success: bool
    artifact: ToolImageArtifact | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.success, bool):
            raise TypeError("success must be a boolean")

    @abstractmethod
    def model_content(self) -> str:
        """Return the content that should be sent back to the model."""


@dataclass(frozen=True, slots=True, kw_only=True)
class JsonToolResult(ToolResult):
    payload: object

    def model_content(self) -> str:
        return json.dumps(
            self.payload, ensure_ascii=False, separators=(",", ":"), default=str,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class TextToolResult(ToolResult):
    content: str

    def model_content(self) -> str:
        return self.content
