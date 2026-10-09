from __future__ import annotations

import hashlib
import json
import math
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from engine.common.media_probe import probe_video_duration_seconds
from engine.parsev1.schemas import VideoParseResult
from engine.parsev1.retrieval import (
    VideoChunkRetrievalDocument,
    build_video_chunk_retrieval_documents,
)

from ...constants.workspace import DEFAULT_LOCAL_WORKSPACE_ID
from ...model.identifiers import (
    derive_context_snapshot_id,
    derive_series_id,
    derive_video_chunk_id,
    derive_video_id,
)
from ...model.dto.parseDTO import (
    PreparedEmbedding,
    PreparedVideoChunk,
    PreparedVideoParseImport,
)
from ...schema import VideoChunkKeyframeReference
from ...utils.time import now_unix_time_ms, require_unix_time_ms
from ...utils.validation import canonical_uuid


def prepare_completed_video_parse_import(
    parse_result: VideoParseResult,
    *,
    workspace_id: str,
    series_id: str,
    series_name: str,
    series_description: str | None,
    important_extra_data: dict[str, Any] | None,
    character_bible: list[dict[str, Any]],
    video_id: str,
    episode_order: int,
    upload_sequence: int,
    original_filename: str,
    original_source_uri: str,
    content_hash: str,
    total_bytes: int,
    pipeline_version: str,
    chunk_media_refs: Mapping[int, str],
    analysis_window_media_refs: Mapping[int, str],
    keyframes_by_sequence: Mapping[
        int,
        tuple[VideoChunkKeyframeReference, ...],
    ],
    text_embeddings: Mapping[int, PreparedEmbedding],
    vision_embeddings: Mapping[int, PreparedEmbedding],
    parsed_at: int | None = None,
) -> PreparedVideoParseImport:
    """Prepare already-verified Worker outputs without transitional file caches."""

    canonical_series_id = canonical_uuid(series_id, "series_id")
    canonical_video_id = canonical_uuid(video_id, "video_id")
    if derive_video_id(canonical_series_id, content_hash) != canonical_video_id:
        raise ValueError("video_id does not match Series and source content hash")
    if not workspace_id.strip() or not series_name.strip():
        raise ValueError("workspace_id and series_name must not be empty")
    if not pipeline_version.strip():
        raise ValueError("pipeline_version must not be empty")
    if episode_order <= 0 or upload_sequence <= 0:
        raise ValueError("episode_order and upload_sequence must be positive")
    if total_bytes <= 0:
        raise ValueError("total_bytes must be greater than zero")
    if len(content_hash) != 64:
        raise ValueError("content_hash must be a SHA-256 hex digest")
    if not original_filename or not original_source_uri:
        raise ValueError("original media metadata must not be empty")

    retrieval_documents = {
        document.sequence_no: document
        for document in build_video_chunk_retrieval_documents(parse_result)
    }
    expected_sequences = set(retrieval_documents)
    for name, values in (
        ("chunk_media_refs", chunk_media_refs),
        ("analysis_window_media_refs", analysis_window_media_refs),
        ("keyframes_by_sequence", keyframes_by_sequence),
        ("text_embeddings", text_embeddings),
        ("vision_embeddings", vision_embeddings),
    ):
        if set(values) != expected_sequences:
            raise ValueError(f"{name} must contain every parsed VideoChunk exactly once")

    prepared_chunks: list[PreparedVideoChunk] = []
    for chunk in parse_result.chunks:
        sequence_no = chunk.window.sequence_no
        chunk_media_ref = chunk_media_refs[sequence_no]
        analysis_media_ref = analysis_window_media_refs[sequence_no]
        if not chunk_media_ref or not analysis_media_ref:
            raise ValueError("VideoChunk media references must not be empty")
        prepared_chunks.append(
            PreparedVideoChunk(
                video_chunk_id=derive_video_chunk_id(
                    canonical_video_id,
                    sequence_no,
                ),
                snapshot_id=derive_context_snapshot_id(
                    canonical_video_id,
                    sequence_no,
                ),
                parse_result=chunk,
                retrieval_document=retrieval_documents[sequence_no],
                chunk_media_ref=chunk_media_ref,
                analysis_window_media_ref=analysis_media_ref,
                keyframes=keyframes_by_sequence[sequence_no],
                text_embedding=text_embeddings[sequence_no],
                vision_embedding=vision_embeddings[sequence_no],
            )
        )

    return PreparedVideoParseImport(
        series_id=canonical_series_id,
        workspace_id=workspace_id.strip(),
        series_name=series_name.strip(),
        series_description=series_description,
        important_extra_data=important_extra_data,
        character_bible=character_bible,
        video_id=canonical_video_id,
        episode_order=episode_order,
        upload_sequence=upload_sequence,
        original_filename=original_filename,
        original_source_uri=original_source_uri,
        content_hash=content_hash,
        total_bytes=total_bytes,
        pipeline_version=pipeline_version.strip(),
        parsed_at=(
            now_unix_time_ms()
            if parsed_at is None
            else require_unix_time_ms(parsed_at, "parsed_at")
        ),
        parse_result=parse_result,
        chunks=tuple(prepared_chunks),
    )


def prepare_video_parse_import(
    parse_result_path: str | Path,
    canonical_chunk_directory: str | Path,
    *,
    series_name: str,
    episode_order: int,
    pipeline_version: str,
    workspace_id: str = DEFAULT_LOCAL_WORKSPACE_ID,
    upload_sequence: int | None = None,
    series_id: str | None = None,
    video_id: str | None = None,
    series_description: str | None = None,
    important_extra_data: dict[str, Any] | None = None,
    character_bible: list[dict[str, Any]] | None = None,
    embedding_cache_directory: str | Path | None = None,
    series_bible_file: str | Path | None = None,
) -> PreparedVideoParseImport:
    if series_bible_file is not None:
        if important_extra_data is not None:
            raise ValueError("provide either series_bible_file or important_extra_data")
        bible_path = Path(series_bible_file).expanduser().resolve(strict=True)
        important_extra_data = {
            "seriesBible": bible_path.read_text(encoding="utf-8"),
            "sourceUri": bible_path.as_uri(),
        }
    parse_path = Path(parse_result_path).expanduser().resolve(strict=True)
    canonical_directory = Path(canonical_chunk_directory).expanduser().resolve(
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
    # File imports have no database unit marker. Verify their millisecond
    # duration against the source instead of guessing a historical unit.
    if parse_result.duration_seconds != probe_video_duration_seconds(source_video):
        raise ValueError("parse_result duration_seconds must match the source video duration in milliseconds")

    content_hash = _sha256_file(source_video)
    resolved_series_id = (
        derive_series_id(workspace_id, series_name)
        if series_id is None
        else canonical_uuid(series_id, "series_id")
    )
    resolved_video_id = (
        derive_video_id(resolved_series_id, content_hash)
        if video_id is None
        else canonical_uuid(video_id, "video_id")
    )

    retrieval_documents = {
        document.sequence_no: document
        for document in build_video_chunk_retrieval_documents(parse_result)
    }
    cache_directory = (
        Path(embedding_cache_directory).expanduser().resolve(strict=True)
        if embedding_cache_directory is not None
        else None
    )
    if cache_directory is not None and not cache_directory.is_dir():
        raise NotADirectoryError(cache_directory)

    source_stat = source_video.stat()
    chunks: list[PreparedVideoChunk] = []
    for chunk in parse_result.chunks:
        window = chunk.window
        sequence_no = window.sequence_no
        chunk_media = _first_existing_file(
            canonical_directory / f"chunk-{sequence_no:04d}.mp4",
            canonical_directory
            / (
                f"chunk-{sequence_no:04d}.analysis-"
                f"{window.chunk_start_seconds:010d}-{window.chunk_end_seconds:010d}.mp4"
            ),
            canonical_directory
            / (
                f"segment-{sequence_no:04d}.analysis-"
                f"{window.chunk_start_seconds:010d}-{window.chunk_end_seconds:010d}.mp4"
            ),
            canonical_directory / f"segment-{sequence_no:04d}.mp4",
        )
        if not chunk_media.is_file():
            raise FileNotFoundError(
                f"canonical VideoChunk is not a file: {chunk_media}"
            )
        declared_analysis_window = Path(window.media_path).expanduser()
        if declared_analysis_window.is_absolute():
            analysis_window_media = declared_analysis_window
        else:
            analysis_window_media = parse_path.parent / declared_analysis_window
        relocated_window = parse_path.parent / "windows" / declared_analysis_window.name
        renamed_window = relocated_window.with_name(
            relocated_window.name.replace("segment-", "chunk-", 1)
        )
        analysis_window_media = _first_existing_file(
            analysis_window_media,
            relocated_window,
            renamed_window,
        )
        if not analysis_window_media.is_file():
            raise FileNotFoundError(
                f"AnalysisWindow is not a file: {analysis_window_media}"
            )

        video_chunk_id = derive_video_chunk_id(resolved_video_id, sequence_no)
        snapshot_id = derive_context_snapshot_id(resolved_video_id, sequence_no)
        text_embedding = None
        vision_embedding = None
        if cache_directory is not None:
            text_embedding = _load_embedding(
                _first_existing_file(
                    cache_directory / f"chunk-{sequence_no:04d}.text.json",
                    cache_directory / f"segment-{sequence_no:04d}.text.json",
                ),
                expected_modality="text",
                expected_document=retrieval_documents[sequence_no],
                source_video=source_video,
                source_stat=source_stat,
                chunk_media=chunk_media,
            )
            vision_embedding = _load_embedding(
                _first_existing_file(
                    cache_directory / f"chunk-{sequence_no:04d}.video.json",
                    cache_directory / f"segment-{sequence_no:04d}.video.json",
                ),
                expected_modality="video",
                expected_document=retrieval_documents[sequence_no],
                source_video=source_video,
                source_stat=source_stat,
                chunk_media=chunk_media,
            )

        chunks.append(
            PreparedVideoChunk(
                video_chunk_id=video_chunk_id,
                snapshot_id=snapshot_id,
                parse_result=chunk,
                retrieval_document=retrieval_documents[sequence_no],
                chunk_media_ref=chunk_media.as_uri(),
                analysis_window_media_ref=analysis_window_media.as_uri(),
                keyframes=(),
                text_embedding=text_embedding,
                vision_embedding=vision_embedding,
            )
        )

    parsed_at = parse_path.stat().st_mtime_ns // 1_000_000
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
        chunks=tuple(chunks),
    )


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
    expected_document: VideoChunkRetrievalDocument,
    source_video: Path,
    source_stat: os.stat_result,
    chunk_media: Path,
) -> PreparedEmbedding:
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

    chunk = payload.pop("chunk", None)
    if chunk is None:
        chunk = payload.pop("segment", None)
    else:
        payload.pop("segment", None)
    expected_chunk = {
        "sequence_no": expected_document.sequence_no,
        "start_seconds": expected_document.start_seconds,
        "end_seconds": expected_document.end_seconds,
    }
    if not isinstance(chunk, dict) or any(
        chunk.get(key) != value for key, value in expected_chunk.items()
    ):
        raise ValueError(
            f"embedding cache VideoChunk does not match parse result: {path}"
        )

    source = payload.get("source")
    if not isinstance(source, dict) or (
        source.get("resolved_path") != str(source_video)
        or source.get("size_bytes") != source_stat.st_size
        or source.get("mtime_ns") != source_stat.st_mtime_ns
    ):
        raise ValueError(f"embedding cache source does not match video: {path}")

    if expected_modality == "text":
        expected_input_hash = hashlib.sha256(
            expected_document.text.encode("utf-8")
        ).hexdigest()
        valid_input_hashes = {expected_input_hash}
    else:
        max_height = chunk.get("max_height")
        if not isinstance(max_height, int) or max_height <= 0:
            raise ValueError(f"embedding cache has invalid max_height: {path}")
        media_stat = chunk_media.stat()
        media_paths = [chunk_media]
        current_path = str(chunk_media)
        output_marker = f"{os.sep}outputs{os.sep}engine{os.sep}"
        if output_marker in current_path:
            legacy_path = Path(
                current_path.replace(
                    output_marker,
                    f"{os.sep}packages{os.sep}engine{os.sep}outputs{os.sep}",
                    1,
                )
            )
            legacy_path = legacy_path.with_name(
                legacy_path.name.replace("chunk-", "segment-", 1)
            )
            media_paths.append(legacy_path)

        input_hashes = [
            hashlib.sha256(
                (
                    f"{source_video}\0{source_stat.st_size}\0{source_stat.st_mtime_ns}"
                    f"\0{expected_document.start_seconds}\0{expected_document.end_seconds}"
                    f"\0{max_height}\0{media_path}\0{media_stat.st_size}"
                    f"\0{media_stat.st_mtime_ns}"
                ).encode("utf-8")
            ).hexdigest()
            for media_path in media_paths
        ]
        expected_input_hash = input_hashes[0]
        valid_input_hashes = set(input_hashes)
    if payload.get("input_sha256") not in valid_input_hashes:
        raise ValueError(f"embedding cache input hash does not match: {path}")

    payload["input_sha256"] = expected_input_hash
    payload["chunk"] = chunk

    return PreparedEmbedding(
        vector=[float(value) for value in vector],
        metadata=payload,
    )


def _first_existing_file(*paths: Path) -> Path:
    for path in paths:
        if path.is_file():
            return path.resolve(strict=True)
    return paths[0].resolve(strict=True)
