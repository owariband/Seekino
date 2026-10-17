"""Speaker encoders consume prepared audio; identity matching belongs to the caller."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Protocol

from pydantic import BaseModel, ConfigDict, Field, computed_field

if TYPE_CHECKING:
    from gateway.config import CampPlusModelSourceConfig, ModelBinding


class SpeakerEmbeddingResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str = Field(min_length=1)
    encoder_revision: str = Field(min_length=1)
    vector: list[Annotated[float, Field(allow_inf_nan=False, strict=True)]] = Field(min_length=1)
    duration_ms: int = Field(gt=0, strict=True)

    @computed_field
    @property
    def dimension(self) -> int:
        return len(self.vector)


class SpeakerEmbeddingModel(Protocol):
    provider_name: str
    model_name: str
    encoder_revision: str
    dimensions: int

    def embed_audio(self, audio_path: str | Path) -> SpeakerEmbeddingResult: ...

    def close(self) -> None: ...


def build_speaker_embedding_model(
    binding: ModelBinding, source: CampPlusModelSourceConfig,
) -> SpeakerEmbeddingModel:
    from gateway.config import CampPlusModelSourceConfig
    from gateway.local.campplus import CampPlusSpeakerEmbedder

    if not isinstance(source, CampPlusModelSourceConfig):
        raise TypeError(f"unsupported speaker embedding adapter {source.adapter!r}")
    return CampPlusSpeakerEmbedder(
        binding.model,
        checkpoint_path=source.checkpoint_path,
        cpu_threads=source.cpu_threads,
        max_audio_seconds=source.max_audio_seconds,
        request_timeout_seconds=binding.request_timeout_seconds,
    )
