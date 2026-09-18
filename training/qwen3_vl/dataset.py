from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from engine.parse.prompts import (
    build_video_parse_context,
    build_video_parse_prompt,
)
from engine.parse.schemas import (
    TranscriptAnalysis,
    VideoChunkCheckpoint,
    VideoTimeline,
)

_RUNTIME_CONTEXT_START = "<!-- ANIVAG_RUNTIME_CONTEXT_START -->"
_RUNTIME_CONTEXT_END = "<!-- ANIVAG_RUNTIME_CONTEXT_END -->"
_MAX_SERIES_CONTEXT_CHARACTERS = 6000


def select_training_series_context(series_bible: str | None) -> str | None:
    """Select the bounded runtime section instead of training on full docs."""

    if series_bible is None or not series_bible.strip():
        return None
    has_start = _RUNTIME_CONTEXT_START in series_bible
    has_end = _RUNTIME_CONTEXT_END in series_bible
    if has_start != has_end:
        raise ValueError("SeriesBible runtime context markers must be paired")
    if has_start:
        context = series_bible.split(_RUNTIME_CONTEXT_START, 1)[1].split(
            _RUNTIME_CONTEXT_END,
            1,
        )[0]
    else:
        context = series_bible
    context = context.strip()
    if len(context) > _MAX_SERIES_CONTEXT_CHARACTERS:
        raise ValueError(
            "SeriesBible training context exceeds 6000 characters; add a bounded "
            "ANIVAG_RUNTIME_CONTEXT block"
        )
    return context or None


def build_swift_record(
    checkpoint: VideoChunkCheckpoint,
    *,
    video_reference: str,
    series_bible: str | None,
) -> dict[str, Any]:
    """Build one portable ms-swift SFT row from a committed parse checkpoint."""

    if not video_reference.strip():
        raise ValueError("video_reference must not be empty")
    if Path(video_reference).is_absolute():
        raise ValueError("video_reference must be relative to the dataset directory")

    chunk = checkpoint.chunk
    context = build_video_parse_context(
        chunk.window,
        chunk.context_before,
        series_bible=select_training_series_context(series_bible),
    )
    prompt = build_video_parse_prompt(
        chunk.window.analysis_duration_ms,
        TranscriptAnalysis(transcript=chunk.candidate_transcript),
        context=context,
        ordered_frames=False,
        # Qwen3-VL decodes the video frames but does not consume the audio track.
        video_audio_available=False,
    )
    target = VideoTimeline(
        summary=chunk.window_summary,
        events=sorted(
            (item.relative_event for item in chunk.observed_events),
            key=lambda item: (item.start_ms, item.end_ms),
        ),
    )
    response = json.dumps(
        target.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return {
        "messages": [
            {"role": "user", "content": f"<video>\n{prompt}"},
            {"role": "assistant", "content": response},
        ],
        "videos": [video_reference],
    }


def build_manifest_record(
    checkpoint: VideoChunkCheckpoint,
    *,
    sample_id: str,
    split: str,
    job_id: str,
    series_id: str,
    video_id: str,
    episode_order: int,
    video_reference: str,
    video_sha256: str,
    parse_artifact_uri: str,
) -> dict[str, Any]:
    provenance = checkpoint.chunk.video_parse
    return {
        "sample_id": sample_id,
        "split": split,
        "job_id": job_id,
        "series_id": series_id,
        "video_id": video_id,
        "episode_order": episode_order,
        "sequence_no": checkpoint.chunk.window.sequence_no,
        "analysis_start_ms": checkpoint.chunk.window.analysis_start_ms,
        "analysis_end_ms": checkpoint.chunk.window.analysis_end_ms,
        "analysis_duration_ms": checkpoint.chunk.window.analysis_duration_ms,
        "video": video_reference,
        "video_sha256": video_sha256,
        "parse_artifact_uri": parse_artifact_uri,
        "teacher_provider": provenance.provider if provenance else None,
        "teacher_model": provenance.model if provenance else checkpoint.video_model,
        "teacher_input_mode": provenance.input_mode if provenance else None,
        "is_fallback": checkpoint.chunk.is_fallback,
    }
