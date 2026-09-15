from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class VideoProcessingConfig:
    embedding_dimensions: int = 1024
    canonical_max_height: int = 720

    def __post_init__(self) -> None:
        if self.embedding_dimensions <= 0:
            raise ValueError("embedding_dimensions must be greater than zero")
        if self.canonical_max_height <= 0:
            raise ValueError("canonical_max_height must be greater than zero")
