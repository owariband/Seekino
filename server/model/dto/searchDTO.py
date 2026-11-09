from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import Field

from agent.video_parse_v2.input import ParseConfig
from engine.rag.contracts import VideoChunkEvidence
from engine.rag.retrieval.contracts import ChunkRetrievalFacts

from ...schema import ContractModel


class VideoSearchRequest(ContractModel):
    tenant_id: str = Field(min_length=1, max_length=64)
    query: str = Field(min_length=1)
    series_ids: list[str] = Field(min_length=1)
    top_k: int | None = Field(
        default=None, ge=1, strict=True,
        description="Omitted or null values use the retrieval search default; explicit positive integers are passed through",
    )


@dataclass(frozen=True, slots=True)
class VideoParseRetrievalTaskData:
    """Task fields required to choose and validate a retrieval scope."""

    id: str
    video_id: str
    status: str
    parse_config: dict[str, Any]
    chunk_count: int


@dataclass(frozen=True, slots=True)
class VideoParseRetrievalChunkData:
    """Published chunk identity needed to validate retrieval scope coverage."""

    video_task_id: str
    video_id: str
    sequence_no: int


@dataclass(frozen=True, slots=True)
class PublishedTimelineChunk:
    video_id: str
    parse_task_id: str
    video_chunk_id: str
    content_version: int
    index_generation: int
    event_count: int


@dataclass(frozen=True, slots=True)
class VideoParseRetrievalScope:
    """Current queryable V2 task set and its shared index contract."""

    series_ids: tuple[str, ...]
    searched_video_ids: tuple[str, ...]
    parse_tasks_by_video_id: Mapping[str, str]
    series_by_video_id: Mapping[str, str]
    parse_config: ParseConfig
    index_generation: int
    partial: bool = False
    warnings: tuple[str, ...] = ()

    @property
    def embedding_model(self) -> str:
        return self.parse_config.models.embedding.model


@dataclass(frozen=True, slots=True)
class HydratedRetrievalCandidate:
    facts: ChunkRetrievalFacts
    evidence: VideoChunkEvidence
    contexts: tuple[ChunkRetrievalFacts, ...] = ()
