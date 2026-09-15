from __future__ import annotations

import json
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextvars import copy_context
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from .schemas import (
    AnalysisWindow,
    TranscriptAnalysis,
    TranscriptCheckpoint,
    VideoChunkCheckpoint,
    VideoChunkParseResult,
)
from .usage import parse_chunk_scope
from .video_chunk_parse import VideoChunkParser


def write_json(payload: BaseModel | dict[str, Any], output: Path) -> None:
    if isinstance(payload, BaseModel):
        data = payload.model_dump(mode="json")
    else:
        data = payload
    rendered = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(rendered, encoding="utf-8")
    temporary.replace(output)


def analysis_windows_match(
    checkpoint_window: AnalysisWindow,
    current_window: AnalysisWindow,
) -> bool:
    if checkpoint_window == current_window:
        return True
    checkpoint_data = checkpoint_window.model_dump()
    current_data = current_window.model_dump()
    checkpoint_data.pop("media_path")
    current_data.pop("media_path")
    return checkpoint_data == current_data


def load_completed_chunks(
    windows: list[AnalysisWindow],
    *,
    output_dir: Path,
    transcript_model: str,
    transcript_source: str,
    video_model: str,
    fallback_provider: str,
    fallback_model: str,
    fallback_transcript_model: str,
    fallback_frame_fps: float,
    fallback_group_duration_ms: int,
    fallback_max_frames_per_group: int,
) -> list[VideoChunkParseResult]:
    completed: list[VideoChunkParseResult] = []
    gap_found = False
    for window in windows:
        path = output_dir / "chunks" / f"chunk-{window.sequence_no:04d}.json"
        if not path.is_file():
            legacy_path = (
                output_dir
                / "segments"
                / f"segment-{window.sequence_no:04d}.json"
            )
            if legacy_path.is_file():
                path = legacy_path
            else:
                gap_found = True
                continue
        if gap_found:
            raise RuntimeError(f"chunk checkpoints contain a gap before {path}")

        checkpoint = VideoChunkCheckpoint.model_validate_json(
            path.read_text(encoding="utf-8")
        )
        if checkpoint.transcript_model != transcript_model:
            raise RuntimeError(f"transcript model changed for checkpoint: {path}")
        if checkpoint.transcript_source != transcript_source:
            raise RuntimeError(f"transcript source changed for checkpoint: {path}")
        if checkpoint.video_model != video_model:
            raise RuntimeError(f"video model changed for checkpoint: {path}")
        provenance = checkpoint.chunk.video_parse
        if provenance is not None:
            if provenance.provider == "doubao":
                expected_model = video_model
                expected_input_mode = "video"
            else:
                if provenance.provider != fallback_provider:
                    raise RuntimeError(
                        f"actual video parser provider changed for checkpoint: {path}"
                    )
                expected_model = fallback_model
                expected_input_mode = "ordered_frames"
            if provenance.model != expected_model:
                raise RuntimeError(
                    f"actual video parser changed for checkpoint: {path}"
                )
            if provenance.input_mode != expected_input_mode:
                raise RuntimeError(
                    f"video parser input mode changed for checkpoint: {path}"
                )
            if provenance.provider != "doubao":
                expected_fallback = {
                    "frame_fps": fallback_frame_fps,
                    "group_duration_ms": fallback_group_duration_ms,
                    "max_frames_per_group": fallback_max_frames_per_group,
                    "fallback_transcript_model": fallback_transcript_model,
                    "visual_model": fallback_model,
                    "refiner_model": fallback_model,
                }
                actual_fallback = {
                    key: getattr(provenance, key) for key in expected_fallback
                }
                if actual_fallback != expected_fallback:
                    raise RuntimeError(
                        f"fallback strategy changed for checkpoint: {path}"
                    )
        if not analysis_windows_match(checkpoint.chunk.window, window):
            raise RuntimeError(f"analysis window changed for checkpoint: {path}")
        completed.append(checkpoint.chunk.model_copy(update={"window": window}))
    return completed


def prepare_candidate_transcripts(
    parser: VideoChunkParser,
    windows: list[AnalysisWindow],
    *,
    output_dir: Path,
    transcript_model: str,
    transcript_source: str,
    concurrency: int,
    on_log: Callable[[str], None] | None = None,
) -> dict[int, TranscriptAnalysis]:
    if concurrency <= 0:
        raise ValueError("asr_concurrency must be greater than zero")
    log = on_log or (lambda message: print(message, flush=True))

    transcripts: dict[int, TranscriptAnalysis] = {}
    pending: list[AnalysisWindow] = []
    for window in windows:
        path = output_dir / "transcripts" / f"chunk-{window.sequence_no:04d}.json"
        if not path.is_file():
            legacy_path = (
                output_dir
                / "transcripts"
                / f"segment-{window.sequence_no:04d}.json"
            )
            if legacy_path.is_file():
                path = legacy_path
            else:
                pending.append(window)
                continue
        checkpoint = TranscriptCheckpoint.model_validate_json(
            path.read_text(encoding="utf-8")
        )
        if checkpoint.transcript_model != transcript_model:
            raise RuntimeError(f"transcript model changed for checkpoint: {path}")
        if checkpoint.transcript_source != transcript_source:
            raise RuntimeError(f"transcript source changed for checkpoint: {path}")
        if not analysis_windows_match(checkpoint.window, window):
            raise RuntimeError(f"analysis window changed for checkpoint: {path}")
        transcripts[window.sequence_no] = checkpoint.analysis

    if not pending:
        return transcripts

    if transcript_source == "none":
        for window in pending:
            analysis = TranscriptAnalysis(transcript=[])
            checkpoint = TranscriptCheckpoint(
                transcript_model=transcript_model,
                transcript_source=transcript_source,
                window=window,
                analysis=analysis,
            )
            path = (
                output_dir
                / "transcripts"
                / f"chunk-{window.sequence_no:04d}.json"
            )
            write_json(checkpoint, path)
            transcripts[window.sequence_no] = analysis
        log(
            f"candidate ASR disabled for {len(pending)} pending chunks; "
            "Pro will use video audio and hard subtitles"
        )
        return transcripts

    errors: list[tuple[AnalysisWindow, Exception]] = []

    def transcribe(window: AnalysisWindow) -> TranscriptAnalysis:
        with parse_chunk_scope(window.sequence_no):
            return parser.transcribe(window.media_path, duration_ms=window.analysis_duration_ms)

    with ThreadPoolExecutor(max_workers=min(concurrency, len(pending))) as executor:
        futures = {
            executor.submit(copy_context().run, transcribe, window): window
            for window in pending
        }
        for future in as_completed(futures):
            window = futures[future]
            try:
                analysis = future.result()
            except Exception as exc:
                errors.append((window, exc))
                continue
            checkpoint = TranscriptCheckpoint(
                transcript_model=transcript_model,
                transcript_source=transcript_source,
                window=window,
                analysis=analysis,
            )
            path = (
                output_dir
                / "transcripts"
                / f"chunk-{window.sequence_no:04d}.json"
            )
            write_json(checkpoint, path)
            transcripts[window.sequence_no] = analysis
            log(
                f"ASR ready {len(transcripts)}/{len(windows)}; "
                f"chunk={window.sequence_no}; cues={len(analysis.transcript)}"
            )
    if errors:
        window, error = errors[0]
        raise RuntimeError(
            f"candidate ASR failed for chunk {window.sequence_no}; "
            f"{len(errors)} chunk(s) failed"
        ) from error
    return transcripts
