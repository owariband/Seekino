"""Anonymous speaker turns; media selection and character naming belong to callers."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Protocol

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

if TYPE_CHECKING:
    from gateway.config import ModelBinding, PyannoteModelSourceConfig


_Identity = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class SpeakerDiarizationTurn(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    start_ms: int = Field(ge=0, strict=True)
    end_ms: int = Field(gt=0, strict=True)
    speaker: _Identity

    @model_validator(mode="after")
    def validate_interval(self) -> SpeakerDiarizationTurn:
        if self.end_ms <= self.start_ms:
            raise ValueError("speaker turn end_ms must be greater than start_ms")
        return self


class SpeakerDiarizationResult(BaseModel):
    """Overlapping turns are preserved; speaker identifiers apply to this input only."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    model: _Identity
    model_revision: _Identity
    duration_ms: int = Field(gt=0, strict=True)
    turns: list[SpeakerDiarizationTurn]

    @model_validator(mode="after")
    def validate_turns(self) -> SpeakerDiarizationResult:
        previous = None
        for turn in self.turns:
            if turn.end_ms > self.duration_ms:
                raise ValueError("speaker turn exceeds input duration_ms")
            position = (turn.start_ms, turn.end_ms, turn.speaker)
            if previous is not None and position < previous:
                raise ValueError("speaker turns must be sorted by start_ms, end_ms and speaker")
            previous = position
        return self


class SpeakerDiarizationModel(Protocol):
    provider_name: str
    model_name: str
    model_revision: str

    def diarize_audio(self, audio_path: str | Path) -> SpeakerDiarizationResult: ...

    def close(self) -> None: ...


def build_speaker_diarization_model(
    binding: ModelBinding, source: PyannoteModelSourceConfig,
) -> SpeakerDiarizationModel:
    from gateway.config import PyannoteModelSourceConfig
    from gateway.local.pyannote import PyannoteSpeakerDiarizer

    if not isinstance(source, PyannoteModelSourceConfig):
        raise TypeError(f"unsupported speaker diarization adapter {source.adapter!r}")
    return PyannoteSpeakerDiarizer(
        binding.model,
        pipeline_path=source.pipeline_path,
        cpu_threads=source.cpu_threads,
        max_audio_seconds=source.max_audio_seconds,
        request_timeout_seconds=binding.request_timeout_seconds,
    )
