"""V1 self-owned retrieval documents built from the formal V1 parse result."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from .schemas import VideoParseResult


class VideoChunkRetrievalDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sequence_no: int = Field(ge=0)
    start_seconds: int = Field(ge=0)
    end_seconds: int = Field(gt=0)
    video_compact: str = Field(min_length=1)
    text: str = Field(min_length=1)
    is_fallback: bool


def build_video_chunk_retrieval_documents(
    parse_result: VideoParseResult,
) -> list[VideoChunkRetrievalDocument]:
    """Build self-owned retrieval text for every canonical video chunk."""

    _validate_canonical_chunks(parse_result)
    documents: list[VideoChunkRetrievalDocument] = []
    for chunk in parse_result.chunks:
        video_compact = chunk.self_compact.strip()
        # The compact carries semantic continuity; owned events preserve exact
        # actions and dialogue terms. Their partial overlap is intentional.
        parts = [video_compact]
        for event in chunk.events:
            parts.append(event.description.strip())
            if event.text:
                parts.append(event.text.strip())
        text = "\n".join(part for part in parts if part).strip()
        if not video_compact or not text:
            raise ValueError(
                f"chunk {chunk.window.sequence_no} has no self-owned retrieval text"
            )
        documents.append(
            VideoChunkRetrievalDocument(
                sequence_no=chunk.window.sequence_no,
                start_seconds=chunk.window.chunk_start_seconds,
                end_seconds=chunk.window.chunk_end_seconds,
                video_compact=video_compact,
                text=text,
                is_fallback=chunk.is_fallback,
            )
        )
    return documents


def _validate_canonical_chunks(parse_result: VideoParseResult) -> None:
    if not parse_result.chunks:
        raise ValueError("parse result contains no chunks")
    expected_start_seconds = 0
    for expected_sequence_no, chunk in enumerate(parse_result.chunks):
        window = chunk.window
        if window.sequence_no != expected_sequence_no:
            raise ValueError(
                "parse result chunks must use contiguous sequence numbers"
            )
        if window.chunk_start_seconds != expected_start_seconds:
            raise ValueError("parse result canonical chunks must be contiguous")
        expected_start_seconds = window.chunk_end_seconds
    if expected_start_seconds != parse_result.duration_seconds:
        raise ValueError("parse result does not cover the complete video duration")
