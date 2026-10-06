"""Literal transcript matching with real cue ranges, independent of Parse versions."""

from __future__ import annotations

import unicodedata
from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from engine.rag.retrieval.fusion import RankedVideoChunkMatch

_TRANSCRIPT_PHRASE_BASE_SCORE = 0.9
_TRANSCRIPT_PHRASE_COVERAGE_BONUS = 0.09


class TranscriptCueDocument(BaseModel):
    """One absolute-time ASR cue available to lexical retrieval."""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1)
    start_seconds: int = Field(ge=0)
    end_seconds: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_time_range(self) -> "TranscriptCueDocument":
        if self.end_seconds < self.start_seconds:
            raise ValueError("end_seconds must not be less than start_seconds")
        return self


class TranscriptChunkDocument(BaseModel):
    """The ordered ASR cues owned by one searchable VideoChunk."""

    model_config = ConfigDict(extra="forbid")

    video_chunk_id: str = Field(min_length=1)
    cues: list[TranscriptCueDocument] = Field(min_length=1)


class RankedTranscriptMatch(RankedVideoChunkMatch):
    """One lexical ASR match with the exact cue range kept for explanation."""

    matched_text: str = Field(min_length=1)
    start_seconds: int = Field(ge=0)
    end_seconds: int = Field(ge=0)


def rank_transcript_chunks(
    query: str,
    documents: Sequence[TranscriptChunkDocument],
    *,
    limit: int,
) -> list[RankedTranscriptMatch]:
    """Rank literal ASR phrase matches without pretending they are semantic hits."""

    if not isinstance(query, str) or not query.strip():
        raise ValueError("query must not be empty")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise ValueError("limit must be a positive integer")
    normalized_query = _normalize_transcript_text(query)
    if not normalized_query:
        return []

    matches: list[tuple[float, str, int, int, str]] = []
    seen_ids: set[str] = set()
    for document in documents:
        if document.video_chunk_id in seen_ids:
            raise ValueError(
                "transcript documents contain duplicate video_chunk_id "
                f"{document.video_chunk_id!r}"
            )
        seen_ids.add(document.video_chunk_id)
        best = _best_transcript_match(normalized_query, document.cues)
        if best is None:
            continue
        score, first_cue, last_cue = best
        matched_cues = document.cues[first_cue : last_cue + 1]
        matches.append(
            (
                score,
                document.video_chunk_id,
                matched_cues[0].start_seconds,
                matched_cues[-1].end_seconds,
                " ".join(cue.text.strip() for cue in matched_cues),
            )
        )

    matches.sort(key=lambda item: (-item[0], item[2], item[1]))
    return [
        RankedTranscriptMatch(
            rank=rank,
            video_chunk_id=video_chunk_id,
            score=score,
            matched_text=matched_text,
            start_seconds=start_seconds,
            end_seconds=end_seconds,
        )
        for rank, (score, video_chunk_id, start_seconds, end_seconds, matched_text) in enumerate(
            matches[:limit],
            start=1,
        )
    ]


def _normalize_transcript_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(character for character in normalized if character.isalnum())


def _best_transcript_match(
    query: str,
    cues: Sequence[TranscriptCueDocument],
) -> tuple[float, int, int] | None:
    normalized_cues = [_normalize_transcript_text(cue.text) for cue in cues]
    joined = "".join(normalized_cues)
    best: tuple[float, int, int] | None = None

    offset = joined.find(query)
    while offset >= 0:
        first_cue = _cue_index_at_offset(normalized_cues, offset)
        last_cue = _cue_index_at_offset(
            normalized_cues,
            offset + len(query) - 1,
        )
        span_length = sum(
            len(value) for value in normalized_cues[first_cue : last_cue + 1]
        )
        score = (
            1.0
            if span_length == len(query)
            else _TRANSCRIPT_PHRASE_BASE_SCORE
            + _TRANSCRIPT_PHRASE_COVERAGE_BONUS * (len(query) / span_length)
        )
        candidate = (score, first_cue, last_cue)
        if best is None or score > best[0] or (
            score == best[0] and (first_cue, last_cue) < (best[1], best[2])
        ):
            best = candidate
        offset = joined.find(query, offset + 1)
    return best


def _cue_index_at_offset(cues: Sequence[str], offset: int) -> int:
    consumed = 0
    for index, cue in enumerate(cues):
        consumed += len(cue)
        if offset < consumed:
            return index
    return len(cues) - 1
