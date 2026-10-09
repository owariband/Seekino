from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pydantic import Field

from ...schema import ContractModel, VideoChunkKeyframeReference

if TYPE_CHECKING:
    from engine.parsev1.schemas import VideoChunkParseResult, VideoParseResult
    from engine.parsev1.retrieval import VideoChunkRetrievalDocument


class ParseImportReport(ContractModel):
    series_id: str
    video_id: str
    video_chunk_ids: list[str]
    chunk_count: int = Field(gt=0)
    vectors_imported: bool


@dataclass(frozen=True)
class PreparedEmbedding:
    vector: list[float]
    metadata: dict[str, Any]


@dataclass(frozen=True)
class PreparedVideoChunk:
    video_chunk_id: str
    snapshot_id: str
    parse_result: VideoChunkParseResult
    retrieval_document: VideoChunkRetrievalDocument
    chunk_media_ref: str
    analysis_window_media_ref: str
    keyframes: tuple[VideoChunkKeyframeReference, ...]
    text_embedding: PreparedEmbedding | None
    vision_embedding: PreparedEmbedding | None


@dataclass(frozen=True)
class PreparedVideoParseImport:
    series_id: str
    workspace_id: str
    series_name: str
    series_description: str | None
    important_extra_data: dict[str, Any] | None
    character_bible: list[dict[str, Any]]
    video_id: str
    episode_order: int
    upload_sequence: int
    original_filename: str
    original_source_uri: str
    content_hash: str
    total_bytes: int
    pipeline_version: str
    parsed_at: int
    parse_result: VideoParseResult
    chunks: tuple[PreparedVideoChunk, ...]
