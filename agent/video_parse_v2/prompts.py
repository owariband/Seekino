"""Deterministic prompts for Video Parse V2 model stages."""

from __future__ import annotations

import json
from collections.abc import Sequence

from engine.parsev2.observations import ASRResult, OCRResult
from engine.parsev2.schemas import VideoParseResult

from .host import ChunkExecution


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True)


def build_ocr_prompt(frame_timestamps: Sequence[int]) -> str:
    return (
        "Read only visible non-dialogue on-screen text from the ordered frames. "
        "Use absolute integer source-video milliseconds inside the authorized range. "
        f"Frame timestamps: {list(frame_timestamps)}."
    )


def build_understanding_prompt(
    execution: ChunkExecution,
    *,
    asr_result: ASRResult,
    ocr_result: OCRResult | None,
) -> str:
    """Describe trusted media bounds while leaving the formal Schema to Engine."""

    return "\n".join((
        "Analyze the prepared media for one fixed video chunk.",
        (
            "Only report events inside the authorized half-open source-video range "
            f"[{execution.allowed_start_seconds}, {execution.allowed_end_seconds})."
        ),
        "Use absolute integer source-video milliseconds and preserve uncertain attribution as null.",
        "ASR/OCR timestamps are already normalized to absolute integer source-video milliseconds.",
        "The legacy start_seconds/end_seconds fields contain milliseconds.",
        "Use these global timestamps directly; do not multiply or divide by 1000 or add a Chunk/window offset again.",
        "Return only the injected VideoParseResult schema; do not invent task IDs or versions.",
        "ASR input: " + _json(asr_result),
        "OCR input: " + _json(
            ocr_result.model_dump(mode="json") if ocr_result is not None else None
        ),
    ))


def build_refine_prompt(
    *,
    start_sequence_no: int,
    chunk_count: int,
    members: list[dict[str, object]],
) -> str:
    """Expose member content and scope while keeping write authority explicit."""

    end_sequence_no = start_sequence_no + chunk_count
    return "\n".join((
        f"Refine the fixed Interval [{start_sequence_no}, {end_sequence_no}).",
        "Each member includes its current content and the ordered Chunk scope already consulted.",
        "Return updates only for Interval members and only for persons or timeline.",
        (
            "Omit every member and field that should remain unchanged; "
            "never return scope, IDs, or versions."
        ),
        "Resolve names, references, and cross-boundary events only from the supplied evidence.",
        "All member timeline timestamps are already absolute integer source-video milliseconds; the legacy start_seconds/end_seconds fields contain milliseconds.",
        "Preserve existing timestamps and durations unless the supplied evidence establishes a timing correction; person or reference corrections must retain their time bounds.",
        "Do not multiply or divide timestamps by 1000, apply a Chunk/window offset again, or reset later Chunk timestamps to zero.",
        "Members: " + _json(members),
    ))


def build_prefix_prompt(
    *,
    sequence_no: int,
    scope: list[int],
    content: VideoParseResult,
    series_prefix_compact: str,
    video_prefix_compact: str,
) -> str:
    """Bind the optional background Agent to one current target."""

    return "\n".join((
        f"Maintain prefix context for the bound Chunk {sequence_no}.",
        f"The target has already consulted ordered scope {scope}.",
        "Update the two compact summaries within their configured budgets.",
        "Any factual correction may target only this Chunk and only persons or timeline.",
        "Omit an update when the current content should stay unchanged.",
        "Series prefix: " + series_prefix_compact,
        "Video prefix: " + video_prefix_compact,
        "Current content: " + content.model_dump_json(),
    ))
