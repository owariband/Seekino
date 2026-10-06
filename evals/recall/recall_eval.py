"""Raw multimodal retrieval evaluation for one parsed episode.

This module deliberately stops at dual-channel embedding retrieval plus RRF.
It does not implement semantic reranking or a CandidateVerifier.  Local video
materialization is evaluation-only; production retrieval is expected to consume
the canonical media persisted by the media-processing pipeline.
"""

from __future__ import annotations

import hashlib
import json
import math
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable, Literal, Protocol

from gateway.embedding import EmbeddingResult
from pydantic import BaseModel, ConfigDict, Field, model_validator

from engine.parsev1.analysis_windows import (
    materialize_analysis_windows,
    plan_analysis_windows,
)
from engine.parsev1.schemas import AnalysisWindow, VideoParseResult
from engine.rag.retrieval.fusion import (
    RankedVideoChunk,
    weighted_reciprocal_rank_fusion,
)
from engine.parsev1.retrieval import (
    VideoChunkRetrievalDocument,
    build_video_chunk_retrieval_documents,
)

from .elasticsearch_index import ElasticsearchVideoChunkIndex

_CACHE_VERSION = 2
_METRIC_CUTOFFS = (1, 3, 5, 10)


class TextVideoEmbedder(Protocol):
    """The subset of Gateway's shared-space embedder used by evaluation."""

    model_name: str
    output_dimensionality: int
    fps: float

    def embed_text(self, text: str) -> EmbeddingResult: ...

    def embed_video(self, video_path: str | Path) -> EmbeddingResult: ...


class RelevantTimeRange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start_seconds: int = Field(ge=0)
    end_seconds: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_range(self) -> "RelevantTimeRange":
        if self.end_seconds <= self.start_seconds:
            raise ValueError("end_seconds must be greater than start_seconds")
        return self


class RecallQueryFixture(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query_id: str = Field(min_length=1)
    query: str = Field(min_length=1)
    relevant_chunk_sequence_nos: list[int] = Field(default_factory=list)
    relevant_time_ranges_seconds: list[RelevantTimeRange] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def accept_legacy_relevance_name(cls, data: object) -> object:
        if not isinstance(data, dict) or "relevant_segment_sequence_nos" not in data:
            return data
        populated = dict(data)
        populated.setdefault(
            "relevant_chunk_sequence_nos",
            populated["relevant_segment_sequence_nos"],
        )
        populated.pop("relevant_segment_sequence_nos")
        return populated

    @model_validator(mode="after")
    def require_relevance(self) -> "RecallQueryFixture":
        if not self.relevant_chunk_sequence_nos and not self.relevant_time_ranges_seconds:
            raise ValueError(
                "at least one relevant video chunk or relevant time range is required"
            )
        if len(set(self.relevant_chunk_sequence_nos)) != len(
            self.relevant_chunk_sequence_nos
        ):
            raise ValueError("relevant chunk sequence numbers must be unique")
        return self


class RecallEvalFixture(BaseModel):
    model_config = ConfigDict(extra="forbid")

    queries: list[RecallQueryFixture] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_query_ids(self) -> "RecallEvalFixture":
        query_ids = [query.query_id for query in self.queries]
        if len(set(query_ids)) != len(query_ids):
            raise ValueError("query_id values must be unique")
        return self


class ChannelQueryMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reciprocal_rank: float = Field(ge=0, le=1)
    hit_at: dict[int, bool]


class RecallQueryResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query_id: str
    query: str
    relevant_chunk_sequence_nos: list[int]
    text_rankings: list[RankedVideoChunk]
    vision_rankings: list[RankedVideoChunk]
    fused_rankings: list[RankedVideoChunk]
    text_metrics: ChannelQueryMetrics
    vision_metrics: ChannelQueryMetrics
    fused_metrics: ChannelQueryMetrics


class AggregateChannelMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mrr: float = Field(ge=0, le=1)
    hit_at: dict[int, float]


class RecallEvalResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_video: Path
    parse_result: Path | None = None
    embedding_model: str
    embedding_dimensions: int = Field(gt=0)
    embedding_fps: float = Field(gt=0)
    channel_top_k: int = Field(gt=0)
    rrf_k: int = Field(gt=0)
    text_weight: float = Field(ge=0)
    vision_weight: float = Field(ge=0)
    retrieval_backend: Literal["local", "qdrant", "elasticsearch"]
    # Read-only compatibility for reports written by the former Qdrant backend.
    qdrant_exact: bool | None = None
    qdrant_collection: str | None = None
    elasticsearch_search_mode: Literal["exact", "approximate"] | None = None
    elasticsearch_index: str | None = None
    dataset_id: str | None = None
    query_count: int = Field(gt=0)
    chunk_count: int = Field(gt=0)
    text_metrics: AggregateChannelMetrics
    vision_metrics: AggregateChannelMetrics
    fused_metrics: AggregateChannelMetrics
    queries: list[RecallQueryResult]


class _SourceFingerprint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resolved_path: str
    size_bytes: int = Field(ge=0)
    mtime_ns: int = Field(ge=0)


class _VideoChunkFingerprint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sequence_no: int = Field(ge=0)
    start_seconds: int = Field(ge=0)
    end_seconds: int = Field(gt=0)
    max_height: int = Field(gt=0)


class _EmbeddingCacheRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cache_version: Literal[2] = _CACHE_VERSION
    model: str
    dimensions: int = Field(gt=0)
    fps: float = Field(gt=0)
    source: _SourceFingerprint
    modality: Literal["text", "video", "query"]
    input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    chunk: _VideoChunkFingerprint | None
    vector: list[float] = Field(min_length=1)

    @model_validator(mode="before")
    @classmethod
    def accept_legacy_chunk_name(cls, data: object) -> object:
        if not isinstance(data, dict) or "segment" not in data:
            return data
        populated = dict(data)
        populated.setdefault("chunk", populated["segment"])
        populated.pop("segment")
        return populated

    @model_validator(mode="after")
    def validate_vector(self) -> "_EmbeddingCacheRecord":
        if len(self.vector) != self.dimensions:
            raise ValueError("cached embedding dimension does not match metadata")
        if not all(math.isfinite(value) for value in self.vector):
            raise ValueError("cached embedding contains a non-finite value")
        if self.modality == "query" and self.chunk is not None:
            raise ValueError("query cache records cannot contain chunk metadata")
        if self.modality != "query" and self.chunk is None:
            raise ValueError("video cache records require chunk metadata")
        return self


def load_parse_result(path: str | Path) -> VideoParseResult:
    result_path = Path(path).expanduser().resolve()
    return VideoParseResult.model_validate_json(result_path.read_text(encoding="utf-8"))


def load_recall_fixture(path: str | Path) -> RecallEvalFixture:
    fixture_path = Path(path).expanduser().resolve()
    return RecallEvalFixture.model_validate_json(
        fixture_path.read_text(encoding="utf-8")
    )


def materialize_eval_only_canonical_media(
    parse_result: VideoParseResult,
    *,
    source_video: str | Path,
    output_dir: str | Path,
    max_height: int = 720,
) -> dict[int, Path]:
    """Create canonical zero-overlap media solely for local recall evaluation."""

    if max_height <= 0:
        raise ValueError("max_height must be greater than zero")
    _validate_canonical_chunks(parse_result)
    source_path = Path(source_video).expanduser().resolve()
    source = _source_fingerprint(source_path)
    source_key = _sha256_text(
        f"{source.resolved_path}\0{source.size_bytes}\0{source.mtime_ns}"
    )[:16]
    media_dir = (
        Path(output_dir).expanduser().resolve()
        / "eval-only-canonical-media"
        / f"{source_key}-h{max_height}"
    )
    windows = plan_analysis_windows(
        source_path,
        duration_seconds=parse_result.duration_seconds,
        output_dir=media_dir,
        chunk_duration_seconds=parse_result.chunk_duration_seconds,
        context_overlap_seconds=0,
    )
    _validate_planned_media(parse_result, windows)
    media_by_sequence: dict[int, Path] = {}
    pending_windows: list[AnalysisWindow] = []
    for window in windows:
        legacy_path = window.media_path.with_name(
            window.media_path.name.replace("chunk-", "segment-", 1)
        )
        if window.media_path.is_file() and window.media_path.stat().st_size > 0:
            media_by_sequence[window.sequence_no] = window.media_path
        elif legacy_path.is_file() and legacy_path.stat().st_size > 0:
            media_by_sequence[window.sequence_no] = legacy_path
        else:
            pending_windows.append(window)

    materialize_analysis_windows(pending_windows, max_height=max_height)
    for window in pending_windows:
        media_by_sequence[window.sequence_no] = window.media_path
    return media_by_sequence


class RecallEvaluator:
    def __init__(
        self,
        embedder: TextVideoEmbedder,
        *,
        cache_dir: str | Path,
        max_height: int = 720,
        channel_top_k: int = 20,
        rrf_k: int = 60,
        text_weight: float = 1.0,
        vision_weight: float = 1.0,
        elasticsearch_index: ElasticsearchVideoChunkIndex | None = None,
        elasticsearch_search_mode: Literal["exact", "approximate"] = "exact",
        concurrency: int = 1,
    ) -> None:
        if max_height <= 0:
            raise ValueError("max_height must be greater than zero")
        if channel_top_k <= 0:
            raise ValueError("channel_top_k must be greater than zero")
        if rrf_k <= 0:
            raise ValueError("rrf_k must be greater than zero")
        for name, weight in (
            ("text_weight", text_weight),
            ("vision_weight", vision_weight),
        ):
            if not math.isfinite(weight) or weight < 0:
                raise ValueError(f"{name} must be a finite nonnegative number")
        if text_weight == 0 and vision_weight == 0:
            raise ValueError("at least one channel weight must be greater than zero")
        if not 1 <= concurrency <= 4:
            raise ValueError("concurrency must be between 1 and 4")
        if elasticsearch_search_mode not in ("exact", "approximate"):
            raise ValueError("elasticsearch_search_mode must be exact or approximate")
        self._embedder = embedder
        self._cache_dir = Path(cache_dir).expanduser().resolve()
        self._max_height = max_height
        self._channel_top_k = channel_top_k
        self._rrf_k = rrf_k
        self._text_weight = text_weight
        self._vision_weight = vision_weight
        self._elasticsearch_index = elasticsearch_index
        self._elasticsearch_search_mode = elasticsearch_search_mode
        self._concurrency = concurrency

    def evaluate(
        self,
        parse_result: VideoParseResult,
        fixture: RecallEvalFixture,
        *,
        source_video: str | Path,
        media_by_sequence: dict[int, Path],
        parse_result_path: str | Path | None = None,
    ) -> RecallEvalResult:
        documents = build_video_chunk_retrieval_documents(parse_result)
        if self._elasticsearch_index is not None:
            self._elasticsearch_index.ensure_index(
                text_dimensions=self._embedder.output_dimensionality,
                vision_dimensions=self._embedder.output_dimensionality,
            )

        source = _source_fingerprint(source_video)
        expected_sequences = {document.sequence_no for document in documents}
        if set(media_by_sequence) != expected_sequences:
            raise ValueError(
                "canonical media must contain exactly one file for every video chunk"
            )
        for sequence_no, media_path in media_by_sequence.items():
            if not Path(media_path).is_file():
                raise FileNotFoundError(
                    f"canonical media for chunk {sequence_no} does not exist: "
                    f"{media_path}"
                )
        vision_input_sha256 = {
            document.sequence_no: _video_input_sha256(
                document,
                source=source,
                media_path=media_by_sequence[document.sequence_no],
                max_height=self._max_height,
            )
            for document in documents
        }
        if self._elasticsearch_index is not None:
            self._elasticsearch_index.assert_dataset_snapshot(
                documents,
                vision_input_sha256=vision_input_sha256,
                embedding_model=self._embedder.model_name,
                embedding_fps=self._embedder.fps,
                allow_incomplete=True,
            )

        text_vectors, video_vectors = self._embed_video_chunks(
            documents,
            media_by_sequence=media_by_sequence,
            source=source,
            vision_input_sha256=vision_input_sha256,
        )
        if self._elasticsearch_index is not None:
            self._elasticsearch_index.upsert_chunks(
                documents,
                text_vectors=text_vectors,
                vision_vectors=video_vectors,
                vision_input_sha256=vision_input_sha256,
                embedding_model=self._embedder.model_name,
                embedding_fps=self._embedder.fps,
            )
            self._elasticsearch_index.assert_dataset_snapshot(
                documents,
                vision_input_sha256=vision_input_sha256,
                embedding_model=self._embedder.model_name,
                embedding_fps=self._embedder.fps,
                allow_incomplete=False,
            )

        query_results: list[RecallQueryResult] = []
        for query_fixture in fixture.queries:
            relevant = _resolve_relevant_chunks(
                query_fixture,
                documents,
                duration_seconds=parse_result.duration_seconds,
            )
            query_vector = self._cached_query_embedding(query_fixture.query, source)
            if self._elasticsearch_index is None:
                text_rankings = cosine_rankings(query_vector, text_vectors)
                vision_rankings = cosine_rankings(query_vector, video_vectors)
            else:
                text_rankings = self._elasticsearch_index.search_text(
                    query_vector,
                    limit=len(documents),
                    mode=self._elasticsearch_search_mode,
                )
                vision_rankings = self._elasticsearch_index.search_vision(
                    query_vector,
                    limit=len(documents),
                    mode=self._elasticsearch_search_mode,
                )
            fused_rankings = weighted_reciprocal_rank_fusion(
                text_rankings,
                vision_rankings,
                text_weight=self._text_weight,
                vision_weight=self._vision_weight,
                channel_top_k=self._channel_top_k,
                rrf_k=self._rrf_k,
            )
            query_results.append(
                RecallQueryResult(
                    query_id=query_fixture.query_id,
                    query=query_fixture.query,
                    relevant_chunk_sequence_nos=sorted(relevant),
                    text_rankings=text_rankings,
                    vision_rankings=vision_rankings,
                    fused_rankings=fused_rankings,
                    text_metrics=_query_metrics(text_rankings, relevant),
                    vision_metrics=_query_metrics(vision_rankings, relevant),
                    fused_metrics=_query_metrics(fused_rankings, relevant),
                )
            )

        return RecallEvalResult(
            source_video=Path(source.resolved_path),
            parse_result=(
                Path(parse_result_path).expanduser().resolve()
                if parse_result_path is not None
                else None
            ),
            embedding_model=self._embedder.model_name,
            embedding_dimensions=self._embedder.output_dimensionality,
            embedding_fps=self._embedder.fps,
            channel_top_k=self._channel_top_k,
            rrf_k=self._rrf_k,
            text_weight=self._text_weight,
            vision_weight=self._vision_weight,
            retrieval_backend=(
                "elasticsearch" if self._elasticsearch_index is not None else "local"
            ),
            elasticsearch_search_mode=(
                self._elasticsearch_search_mode
                if self._elasticsearch_index is not None else None
            ),
            elasticsearch_index=(
                self._elasticsearch_index.index_name
                if self._elasticsearch_index is not None
                else None
            ),
            dataset_id=(
                self._elasticsearch_index.dataset_id
                if self._elasticsearch_index is not None
                else None
            ),
            query_count=len(query_results),
            chunk_count=len(documents),
            text_metrics=_aggregate_metrics(query_results, "text_metrics"),
            vision_metrics=_aggregate_metrics(query_results, "vision_metrics"),
            fused_metrics=_aggregate_metrics(query_results, "fused_metrics"),
            queries=query_results,
        )

    def _embed_video_chunks(
        self,
        documents: list[VideoChunkRetrievalDocument],
        *,
        media_by_sequence: dict[int, Path],
        source: _SourceFingerprint,
        vision_input_sha256: dict[int, str],
    ) -> tuple[dict[int, list[float]], dict[int, list[float]]]:
        text_vectors: dict[int, list[float]] = {}
        video_vectors: dict[int, list[float]] = {}

        def embed_one(
            modality: Literal["text", "video"],
            document: VideoChunkRetrievalDocument,
        ) -> tuple[Literal["text", "video"], int, list[float]]:
            chunk = _VideoChunkFingerprint(
                sequence_no=document.sequence_no,
                start_seconds=document.start_seconds,
                end_seconds=document.end_seconds,
                max_height=self._max_height,
            )
            if modality == "text":
                vector = self._cached_embedding(
                    cache_path=(
                        self._cache_dir
                        / "chunks"
                        / f"chunk-{document.sequence_no:04d}.text.json"
                    ),
                    legacy_cache_path=(
                        self._cache_dir
                        / "segments"
                        / f"segment-{document.sequence_no:04d}.text.json"
                    ),
                    modality="text",
                    input_sha256=_sha256_text(document.text),
                    source=source,
                    chunk=chunk,
                    embed=lambda: self._embedder.embed_text(document.text),
                )
            else:
                media_path = Path(media_by_sequence[document.sequence_no]).resolve()
                vector = self._cached_embedding(
                    cache_path=(
                        self._cache_dir
                        / "chunks"
                        / f"chunk-{document.sequence_no:04d}.video.json"
                    ),
                    legacy_cache_path=(
                        self._cache_dir
                        / "segments"
                        / f"segment-{document.sequence_no:04d}.video.json"
                    ),
                    modality="video",
                    input_sha256=vision_input_sha256[document.sequence_no],
                    source=source,
                    chunk=chunk,
                    embed=lambda: self._embedder.embed_video(media_path),
                )
            return modality, document.sequence_no, vector

        jobs = [
            (modality, document)
            for document in documents
            for modality in ("text", "video")
        ]
        if self._concurrency == 1:
            completed = [
                embed_one(modality, document) for modality, document in jobs
            ]
        else:
            completed = []
            with ThreadPoolExecutor(max_workers=self._concurrency) as executor:
                futures = {
                    executor.submit(embed_one, modality, document): (
                        modality,
                        document,
                    )
                    for modality, document in jobs
                }
                for future in as_completed(futures):
                    completed.append(future.result())

        for modality, sequence_no, vector in completed:
            target = text_vectors if modality == "text" else video_vectors
            target[sequence_no] = vector
        return text_vectors, video_vectors

    def _cached_query_embedding(
        self,
        query: str,
        source: _SourceFingerprint,
    ) -> list[float]:
        normalized_query = query.strip()
        input_sha256 = _sha256_text(normalized_query)
        return self._cached_embedding(
            cache_path=self._cache_dir / "queries" / f"{input_sha256}.json",
            modality="query",
            input_sha256=input_sha256,
            source=source,
            chunk=None,
            embed=lambda: self._embedder.embed_text(normalized_query),
        )

    def _cached_embedding(
        self,
        *,
        cache_path: Path,
        legacy_cache_path: Path | None = None,
        modality: Literal["text", "video", "query"],
        input_sha256: str,
        source: _SourceFingerprint,
        chunk: _VideoChunkFingerprint | None,
        embed: Callable[[], EmbeddingResult],
    ) -> list[float]:
        expected = {
            "cache_version": _CACHE_VERSION,
            "model": self._embedder.model_name,
            "dimensions": self._embedder.output_dimensionality,
            "fps": self._embedder.fps,
            "source": source,
            "modality": modality,
            "input_sha256": input_sha256,
            "chunk": chunk,
        }
        cached = _read_matching_cache(cache_path, expected)
        if cached is None and legacy_cache_path is not None:
            cached = _read_matching_cache(legacy_cache_path, expected)
        if cached is not None:
            return cached.vector

        result = embed()
        if result.model != self._embedder.model_name:
            raise RuntimeError("embedding provider returned an unexpected model")
        expected_modality = "video" if modality == "video" else "text"
        if result.modality != expected_modality:
            raise RuntimeError("embedding provider returned an unexpected modality")
        record = _EmbeddingCacheRecord(
            **expected,
            vector=result.vector,
        )
        _write_model_json_atomic(record, cache_path)
        return record.vector


def cosine_rankings(
    query_vector: list[float],
    chunk_vectors: dict[int, list[float]],
) -> list[RankedVideoChunk]:
    scored = [
        (sequence_no, _cosine_similarity(query_vector, vector))
        for sequence_no, vector in chunk_vectors.items()
    ]
    scored.sort(key=lambda item: (-item[1], item[0]))
    return [
        RankedVideoChunk(rank=rank, chunk_sequence_no=sequence_no, score=score)
        for rank, (sequence_no, score) in enumerate(scored, start=1)
    ]


def reciprocal_rank_fusion(
    text_rankings: list[RankedVideoChunk],
    vision_rankings: list[RankedVideoChunk],
    *,
    channel_top_k: int,
    rrf_k: int,
) -> list[RankedVideoChunk]:
    return weighted_reciprocal_rank_fusion(
        text_rankings,
        vision_rankings,
        text_weight=1.0,
        vision_weight=1.0,
        channel_top_k=channel_top_k,
        rrf_k=rrf_k,
    )


def _validate_canonical_chunks(parse_result: VideoParseResult) -> None:
    if not parse_result.chunks:
        raise ValueError("parse result contains no chunks")
    expected_start = 0
    seen_sequences: set[int] = set()
    for expected_sequence, chunk in enumerate(parse_result.chunks):
        window = chunk.window
        if window.sequence_no != expected_sequence:
            raise ValueError("parse result chunks must use contiguous sequence numbers")
        if window.sequence_no in seen_sequences:
            raise ValueError("parse result contains a duplicate chunk")
        seen_sequences.add(window.sequence_no)
        if window.chunk_start_seconds != expected_start:
            raise ValueError("parse result canonical chunks must be contiguous")
        expected_start = window.chunk_end_seconds
    if expected_start != parse_result.duration_seconds:
        raise ValueError("parse result does not cover the complete video duration")


def _validate_planned_media(
    parse_result: VideoParseResult,
    windows: list[AnalysisWindow],
) -> None:
    if len(windows) != len(parse_result.chunks):
        raise ValueError("planned canonical media does not match parsed chunks")
    for planned, parsed in zip(windows, parse_result.chunks, strict=True):
        parsed_window = parsed.window
        if (
            planned.sequence_no != parsed_window.sequence_no
            or planned.chunk_start_seconds != parsed_window.chunk_start_seconds
            or planned.chunk_end_seconds != parsed_window.chunk_end_seconds
            or planned.analysis_start_seconds != parsed_window.chunk_start_seconds
            or planned.analysis_end_seconds != parsed_window.chunk_end_seconds
        ):
            raise ValueError("planned media is not the canonical zero-overlap chunk")


def _source_fingerprint(source_video: str | Path) -> _SourceFingerprint:
    path = Path(source_video).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"source video does not exist: {path}")
    stat = path.stat()
    return _SourceFingerprint(
        resolved_path=str(path),
        size_bytes=stat.st_size,
        mtime_ns=stat.st_mtime_ns,
    )


def _video_input_sha256(
    document: VideoChunkRetrievalDocument,
    *,
    source: _SourceFingerprint,
    media_path: str | Path,
    max_height: int,
) -> str:
    resolved_media = Path(media_path).expanduser().resolve()
    media_stat = resolved_media.stat()
    identity = (
        f"{source.resolved_path}\0{source.size_bytes}\0{source.mtime_ns}"
        f"\0{document.start_seconds}\0{document.end_seconds}\0{max_height}"
        f"\0{resolved_media}\0{media_stat.st_size}\0{media_stat.st_mtime_ns}"
    )
    return _sha256_text(identity)


def _resolve_relevant_chunks(
    fixture: RecallQueryFixture,
    documents: list[VideoChunkRetrievalDocument],
    *,
    duration_seconds: int,
) -> set[int]:
    valid_sequences = {document.sequence_no for document in documents}
    relevant = set(fixture.relevant_chunk_sequence_nos)
    unknown = relevant - valid_sequences
    if unknown:
        raise ValueError(
            f"query {fixture.query_id} references unknown chunks: {sorted(unknown)}"
        )
    for time_range in fixture.relevant_time_ranges_seconds:
        if time_range.end_seconds > duration_seconds:
            raise ValueError(
                f"query {fixture.query_id} has a time range beyond video duration"
            )
        relevant.update(
            document.sequence_no
            for document in documents
            if document.start_seconds < time_range.end_seconds
            and document.end_seconds > time_range.start_seconds
        )
    if not relevant:
        raise ValueError(
            f"query {fixture.query_id} relevance does not overlap any video chunk"
        )
    return relevant


def _query_metrics(
    rankings: list[RankedVideoChunk],
    relevant: set[int],
) -> ChannelQueryMetrics:
    first_rank = next(
        (
            ranked.rank
            for ranked in rankings
            if ranked.chunk_sequence_no in relevant
        ),
        None,
    )
    return ChannelQueryMetrics(
        reciprocal_rank=0.0 if first_rank is None else 1.0 / first_rank,
        hit_at={
            cutoff: any(
                ranked.chunk_sequence_no in relevant
                for ranked in rankings[:cutoff]
            )
            for cutoff in _METRIC_CUTOFFS
        },
    )


def _aggregate_metrics(
    results: list[RecallQueryResult],
    field_name: Literal["text_metrics", "vision_metrics", "fused_metrics"],
) -> AggregateChannelMetrics:
    metrics = [getattr(result, field_name) for result in results]
    count = len(metrics)
    return AggregateChannelMetrics(
        mrr=sum(metric.reciprocal_rank for metric in metrics) / count,
        hit_at={
            cutoff: sum(metric.hit_at[cutoff] for metric in metrics) / count
            for cutoff in _METRIC_CUTOFFS
        },
    )


def _cosine_similarity(left: list[float], right: list[float]) -> float:
    if len(left) != len(right):
        raise ValueError("embedding vectors must have the same dimension")
    if not left:
        raise ValueError("embedding vectors must not be empty")
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0 or right_norm == 0:
        raise ValueError("embedding vectors must have non-zero norm")
    return sum(a * b for a, b in zip(left, right, strict=True)) / (
        left_norm * right_norm
    )


def _read_matching_cache(
    path: Path,
    expected: dict[str, object],
) -> _EmbeddingCacheRecord | None:
    if not path.is_file():
        return None
    try:
        record = _EmbeddingCacheRecord.model_validate_json(
            path.read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return None
    for field, value in expected.items():
        if getattr(record, field) != value:
            return None
    return record


def _write_model_json_atomic(
    model: BaseModel,
    output: Path,
    *,
    exclude: set[str] | None = None,
) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    rendered = json.dumps(
        model.model_dump(mode="json", exclude=exclude),
        ensure_ascii=False,
        indent=2,
    ) + "\n"
    temporary.write_text(rendered, encoding="utf-8")
    temporary.replace(output)


def write_recall_result(result: RecallEvalResult, output: str | Path) -> None:
    _write_model_json_atomic(
        result,
        Path(output).expanduser().resolve(),
        exclude={"qdrant_exact", "qdrant_collection"},
    )


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
