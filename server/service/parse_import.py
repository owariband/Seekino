from __future__ import annotations

import hashlib
import json
import math
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from engine.parse.schemas import SegmentParseResult, VideoParseResult
from engine.retrieval.video_retrieval import (
    SegmentRetrievalChunk,
    build_segment_retrieval_chunks,
)


@dataclass(frozen=True)
class EmbeddingArtifact:
    vector: list[float]
    metadata: dict[str, Any]


@dataclass(frozen=True)
class PreparedSegment:
    video_segment_id: str
    snapshot_id: str
    parse_result: SegmentParseResult
    retrieval_chunk: SegmentRetrievalChunk
    segment_media_ref: str
    analysis_window_media_ref: str
    text_embedding: EmbeddingArtifact | None
    vision_embedding: EmbeddingArtifact | None


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
    parsed_at: datetime
    parse_result: VideoParseResult
    segments: tuple[PreparedSegment, ...]


def prepare_video_parse_import(
    parse_result_path: str | Path,
    canonical_segment_directory: str | Path,
    *,
    series_name: str,
    episode_order: int,
    pipeline_version: str,
    workspace_id: str = "local-default",
    upload_sequence: int | None = None,
    series_id: str | None = None,
    video_id: str | None = None,
    series_description: str | None = None,
    important_extra_data: dict[str, Any] | None = None,
    character_bible: list[dict[str, Any]] | None = None,
    embedding_cache_directory: str | Path | None = None,
) -> PreparedVideoParseImport:
    parse_path = Path(parse_result_path).expanduser().resolve(strict=True)
    canonical_directory = Path(canonical_segment_directory).expanduser().resolve(
        strict=True
    )
    if not canonical_directory.is_dir():
        raise NotADirectoryError(canonical_directory)
    if episode_order <= 0:
        raise ValueError("episode_order must be greater than zero")
    resolved_upload_sequence = (
        episode_order if upload_sequence is None else upload_sequence
    )
    if resolved_upload_sequence <= 0:
        raise ValueError("upload_sequence must be greater than zero")
    for name, value in (
        ("series_name", series_name),
        ("workspace_id", workspace_id),
        ("pipeline_version", pipeline_version),
    ):
        if not value.strip():
            raise ValueError(f"{name} must not be empty")

    parse_result = VideoParseResult.model_validate_json(
        parse_path.read_text(encoding="utf-8")
    )
    source_video = Path(parse_result.source_video).expanduser()
    if not source_video.is_absolute():
        source_video = parse_path.parent / source_video
    source_video = source_video.resolve(strict=True)
    if not source_video.is_file():
        raise FileNotFoundError(source_video)

    content_hash = _sha256_file(source_video)
    resolved_series_id = _resolve_id(
        series_id,
        namespace_value=(
            f"anivag:series:{workspace_id.strip()}:{series_name.strip().casefold()}"
        ),
        field_name="series_id",
    )
    resolved_video_id = _resolve_id(
        video_id,
        namespace_value=f"anivag:video:{resolved_series_id}:{content_hash}",
        field_name="video_id",
    )

    retrieval_chunks = {
        chunk.sequence_no: chunk
        for chunk in build_segment_retrieval_chunks(parse_result)
    }
    cache_directory = (
        Path(embedding_cache_directory).expanduser().resolve(strict=True)
        if embedding_cache_directory is not None
        else None
    )
    if cache_directory is not None and not cache_directory.is_dir():
        raise NotADirectoryError(cache_directory)

    source_stat = source_video.stat()
    segments: list[PreparedSegment] = []
    for segment in parse_result.segments:
        window = segment.window
        sequence_no = window.sequence_no
        segment_media = canonical_directory / (
            f"segment-{sequence_no:04d}.analysis-"
            f"{window.segment_start_ms:010d}-{window.segment_end_ms:010d}.mp4"
        )
        segment_media = segment_media.resolve(strict=True)
        if not segment_media.is_file():
            raise FileNotFoundError(
                f"canonical Segment is not a file: {segment_media}"
            )
        analysis_window_media = Path(window.media_path).expanduser()
        if not analysis_window_media.is_absolute():
            analysis_window_media = parse_path.parent / analysis_window_media
        analysis_window_media = analysis_window_media.resolve(strict=True)
        if not analysis_window_media.is_file():
            raise FileNotFoundError(
                f"AnalysisWindow is not a file: {analysis_window_media}"
            )

        video_segment_id = str(
            uuid5(
                NAMESPACE_URL,
                f"anivag:video-segment:{resolved_video_id}:{sequence_no}",
            )
        )
        snapshot_id = str(
            uuid5(
                NAMESPACE_URL,
                f"anivag:context-snapshot:{resolved_video_id}:{sequence_no}",
            )
        )
        text_embedding = None
        vision_embedding = None
        if cache_directory is not None:
            text_embedding = _load_embedding(
                cache_directory / f"segment-{sequence_no:04d}.text.json",
                expected_modality="text",
                expected_chunk=retrieval_chunks[sequence_no],
                source_video=source_video,
                source_stat=source_stat,
                segment_media=segment_media,
            )
            vision_embedding = _load_embedding(
                cache_directory / f"segment-{sequence_no:04d}.video.json",
                expected_modality="video",
                expected_chunk=retrieval_chunks[sequence_no],
                source_video=source_video,
                source_stat=source_stat,
                segment_media=segment_media,
            )

        segments.append(
            PreparedSegment(
                video_segment_id=video_segment_id,
                snapshot_id=snapshot_id,
                parse_result=segment,
                retrieval_chunk=retrieval_chunks[sequence_no],
                segment_media_ref=segment_media.as_uri(),
                analysis_window_media_ref=analysis_window_media.as_uri(),
                text_embedding=text_embedding,
                vision_embedding=vision_embedding,
            )
        )

    parsed_at = datetime.fromtimestamp(parse_path.stat().st_mtime, UTC).replace(
        tzinfo=None
    )
    return PreparedVideoParseImport(
        series_id=resolved_series_id,
        workspace_id=workspace_id.strip(),
        series_name=series_name.strip(),
        series_description=series_description,
        important_extra_data=important_extra_data,
        character_bible=character_bible or [],
        video_id=resolved_video_id,
        episode_order=episode_order,
        upload_sequence=resolved_upload_sequence,
        original_filename=source_video.name,
        original_source_uri=source_video.as_uri(),
        content_hash=content_hash,
        total_bytes=source_stat.st_size,
        pipeline_version=pipeline_version.strip(),
        parsed_at=parsed_at,
        parse_result=parse_result,
        segments=tuple(segments),
    )


def _resolve_id(
    explicit_id: str | None,
    *,
    namespace_value: str,
    field_name: str,
) -> str:
    if explicit_id is None:
        return str(uuid5(NAMESPACE_URL, namespace_value))
    try:
        return str(UUID(explicit_id))
    except ValueError as exc:
        raise ValueError(f"{field_name} must be a UUID") from exc


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _load_embedding(
    path: Path,
    *,
    expected_modality: str,
    expected_chunk: SegmentRetrievalChunk,
    source_video: Path,
    source_stat: os.stat_result,
    segment_media: Path,
) -> EmbeddingArtifact:
    payload = json.loads(path.resolve(strict=True).read_text())
    vector = payload.pop("vector", None)
    if not isinstance(vector, list) or not vector:
        raise ValueError(f"embedding cache has no vector: {path}")
    if any(
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        for value in vector
    ):
        raise ValueError(f"embedding cache contains an invalid vector: {path}")
    dimensions = payload.get("dimensions")
    if not isinstance(dimensions, int) or dimensions != len(vector):
        raise ValueError(f"embedding dimensions do not match vector length: {path}")
    if payload.get("modality") != expected_modality:
        raise ValueError(f"embedding cache modality does not match: {path}")

    segment = payload.get("segment")
    expected_segment = {
        "sequence_no": expected_chunk.sequence_no,
        "start_ms": expected_chunk.start_ms,
        "end_ms": expected_chunk.end_ms,
    }
    if not isinstance(segment, dict) or any(
        segment.get(key) != value for key, value in expected_segment.items()
    ):
        raise ValueError(f"embedding cache Segment does not match parse result: {path}")

    source = payload.get("source")
    if not isinstance(source, dict) or (
        source.get("resolved_path") != str(source_video)
        or source.get("size_bytes") != source_stat.st_size
        or source.get("mtime_ns") != source_stat.st_mtime_ns
    ):
        raise ValueError(f"embedding cache source does not match video: {path}")

    if expected_modality == "text":
        expected_input_hash = hashlib.sha256(
            expected_chunk.text.encode("utf-8")
        ).hexdigest()
    else:
        max_height = segment.get("max_height")
        if not isinstance(max_height, int) or max_height <= 0:
            raise ValueError(f"embedding cache has invalid max_height: {path}")
        media_stat = segment_media.stat()
        media_identity = (
            f"{source_video}\0{source_stat.st_size}\0{source_stat.st_mtime_ns}"
            f"\0{expected_chunk.start_ms}\0{expected_chunk.end_ms}\0{max_height}"
            f"\0{segment_media}\0{media_stat.st_size}\0{media_stat.st_mtime_ns}"
        )
        expected_input_hash = hashlib.sha256(media_identity.encode("utf-8")).hexdigest()
    if payload.get("input_sha256") != expected_input_hash:
        raise ValueError(f"embedding cache input hash does not match: {path}")

    return EmbeddingArtifact(
        vector=[float(value) for value in vector],
        metadata=payload,
    )
