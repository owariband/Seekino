from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True, slots=True)
class VideoProcessingConfig:
    embedding_dimensions: int = 1024
    chunk_duration_seconds: int = 60
    context_overlap_seconds: int = 3
    analysis_max_height: int = 720
    canonical_max_height: int = 720
    asr_concurrency: int = 4
    asr_mode: Literal["lite", "none"] = "lite"

    def __post_init__(self) -> None:
        if self.embedding_dimensions <= 0:
            raise ValueError("embedding_dimensions must be greater than zero")
        if self.chunk_duration_seconds <= 0:
            raise ValueError("chunk_duration_seconds must be greater than zero")
        if self.context_overlap_seconds < 0:
            raise ValueError("context_overlap_seconds cannot be negative")
        if self.analysis_max_height <= 0:
            raise ValueError("analysis_max_height must be greater than zero")
        if self.canonical_max_height <= 0:
            raise ValueError("canonical_max_height must be greater than zero")
        if self.asr_concurrency <= 0:
            raise ValueError("asr_concurrency must be greater than zero")
        if self.asr_mode not in {"lite", "none"}:
            raise ValueError("asr_mode must be 'lite' or 'none'")
