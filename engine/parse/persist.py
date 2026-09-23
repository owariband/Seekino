from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Literal, Protocol

from .embed import ChunkEmbedding
from .schemas import (
    AnalysisWindow,
    VideoChunkParseResult,
    VideoParseModels,
    VideoParseResult,
)

if TYPE_CHECKING:
    from .pipeline import ParsePipelineResult


ParsePipelineStage = Literal[
    "transcribe",
    "understand",
    "text_embedding",
    "vision_embedding",
    "persist",
]


class ParsePersistence(Protocol):
    """Durability boundary implemented by the production application layer."""

    def start_stage(self, stage: ParsePipelineStage) -> None: ...

    def ensure_active(self) -> None: ...

    def load_completed_chunks(
        self,
        windows: Sequence[AnalysisWindow],
        *,
        models: VideoParseModels,
    ) -> Sequence[VideoChunkParseResult]: ...

    def save_chunk(
        self,
        chunk: VideoChunkParseResult,
        *,
        models: VideoParseModels,
    ) -> None: ...

    def save_parse_result(self, result: VideoParseResult) -> None: ...

    def load_embedding(
        self,
        *,
        sequence_no: int,
        modality: Literal["text", "video"],
        input_sha256: str,
    ) -> ChunkEmbedding | None: ...

    def save_embedding(self, embedding: ChunkEmbedding) -> None: ...

    def persist(self, result: ParsePipelineResult) -> Sequence[str]: ...
