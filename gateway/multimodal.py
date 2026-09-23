from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, TypeVar

from pydantic import BaseModel


_ResultModel = TypeVar("_ResultModel", bound=BaseModel)


@dataclass(frozen=True, slots=True)
class TextInput:
    text: str

    def __post_init__(self) -> None:
        if not self.text.strip():
            raise ValueError("text input must not be empty")


@dataclass(frozen=True, slots=True)
class ImageInput:
    path: str | Path
    detail: Literal["auto", "low", "high"] = "high"


@dataclass(frozen=True, slots=True)
class VideoInput:
    path: str | Path
    fps: float

    def __post_init__(self) -> None:
        if not 0.2 <= self.fps <= 5.0:
            raise ValueError("video input fps must be between 0.2 and 5.0")


MultimodalInput = TextInput | ImageInput | VideoInput


class MultimodalModel(Protocol):
    provider_name: str
    model_name: str

    def generate(
        self,
        inputs: Sequence[MultimodalInput],
        *,
        schema_name: str,
        result_type: type[_ResultModel],
    ) -> _ResultModel: ...


class DeadlineAwareMultimodalModel(Protocol):
    """A multimodal adapter that accepts a shorter caller-owned deadline."""

    provider_name: str
    model_name: str

    def generate(
        self,
        inputs: Sequence[MultimodalInput],
        *,
        schema_name: str,
        result_type: type[_ResultModel],
        request_timeout_seconds: float | None = None,
    ) -> _ResultModel: ...


def validate_generate_request(
    inputs: Sequence[MultimodalInput],
    schema_name: str,
) -> None:
    if not inputs:
        raise ValueError("at least one multimodal input is required")
    if not schema_name.strip():
        raise ValueError("schema_name must not be empty")


def text_only_inputs(
    inputs: Sequence[MultimodalInput], *, input_type: str
) -> list[dict[str, object]]:
    if not inputs or any(not isinstance(item, TextInput) for item in inputs):
        raise ValueError("agent text calls require non-empty TextInput values")
    return [{"type": input_type, "text": item.text} for item in inputs]
