from __future__ import annotations

import argparse
import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from gateway import (
    ArkMultimodalModel,
    DeepSeekMultimodalModel,
    load_config,
)
from pydantic import BaseModel

from .analysis_windows import (
    materialize_analysis_windows,
    plan_analysis_windows,
    probe_video_duration_ms,
)
from .segment_parse import (
    ContentFilterFallbackSegmentParser,
    DeepSeekFallbackSegmentParser,
    SegmentEvidenceParser,
    SegmentParser,
)
from .schemas import (
    AnalysisWindow,
    SegmentCheckpoint,
    SegmentParseResult,
    TranscriptAnalysis,
    TranscriptCheckpoint,
    VideoFallbackModels,
    VideoParseModels,
)
from .video_parse import ParsePipeline

_RUNTIME_CONTEXT_START = "<!-- ANIVAG_RUNTIME_CONTEXT_START -->"
_RUNTIME_CONTEXT_END = "<!-- ANIVAG_RUNTIME_CONTEXT_END -->"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="anivag-engine",
        description="Run AniVAG domain pipelines.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        help="Model config YAML; defaults to repository-root config.yaml.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    parse_video = subparsers.add_parser(
        "parse-video",
        help="Parse one video as an ordered multi-segment timeline.",
    )
    parse_video.add_argument("--video", type=Path, required=True)
    parse_video.add_argument("--output-dir", type=Path, required=True)
    parse_video.add_argument("--segment-duration-ms", type=int, default=60_000)
    parse_video.add_argument("--context-overlap-ms", type=int, default=3_000)
    parse_video.add_argument("--max-height", type=int, default=720)
    parse_video.add_argument("--asr-concurrency", type=int, default=4)
    parse_video.add_argument(
        "--asr-mode",
        choices=("lite", "none"),
        default="lite",
        help="Use Lite candidate ASR or pass an explicit empty candidate to Pro.",
    )
    parse_video.add_argument("--request-timeout-seconds", type=float, default=600)
    parse_video.add_argument("--max-context-events", type=int, default=8)
    parse_video.add_argument("--max-context-chars", type=int, default=2000)
    context_group = parse_video.add_mutually_exclusive_group()
    context_group.add_argument("--series-bible")
    context_group.add_argument("--series-bible-file", type=Path)
    parse_video.add_argument("--transcript-model")
    parse_video.add_argument("--video-model")
    parse_video.add_argument("--transcript-fps", type=float)
    parse_video.add_argument("--video-fps", type=float)
    parse_video.add_argument(
        "--fallback",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "Use the configured evidence fallback once when Doubao explicitly "
            "rejects generated output via content policy."
        ),
    )
    return parser


def _write_json(payload: BaseModel | dict[str, Any], output: Path) -> None:
    if isinstance(payload, BaseModel):
        data = payload.model_dump(mode="json")
    else:
        data = payload
    rendered = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(rendered, encoding="utf-8")
    temporary.replace(output)


def _read_series_bible(args: argparse.Namespace) -> str | None:
    if args.series_bible:
        return args.series_bible.strip()
    if args.series_bible_file is None:
        return None

    path = args.series_bible_file.expanduser().resolve()
    text = path.read_text(encoding="utf-8")
    start = text.find(_RUNTIME_CONTEXT_START)
    end = text.find(_RUNTIME_CONTEXT_END)
    if start >= 0 and end > start:
        start += len(_RUNTIME_CONTEXT_START)
        text = text[start:end]
    return text.strip()


def _load_completed_segments(
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
) -> list[SegmentParseResult]:
    completed: list[SegmentParseResult] = []
    gap_found = False
    for window in windows:
        path = output_dir / "segments" / f"segment-{window.sequence_no:04d}.json"
        if not path.is_file():
            gap_found = True
            continue
        if gap_found:
            raise RuntimeError(f"segment checkpoints contain a gap before {path}")

        checkpoint = SegmentCheckpoint.model_validate_json(
            path.read_text(encoding="utf-8")
        )
        if checkpoint.transcript_model != transcript_model:
            raise RuntimeError(f"transcript model changed for checkpoint: {path}")
        if checkpoint.transcript_source != transcript_source:
            raise RuntimeError(f"transcript source changed for checkpoint: {path}")
        if checkpoint.video_model != video_model:
            raise RuntimeError(f"video model changed for checkpoint: {path}")
        provenance = checkpoint.segment.video_parse
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
        if checkpoint.segment.window != window:
            raise RuntimeError(f"analysis window changed for checkpoint: {path}")
        completed.append(checkpoint.segment)
    return completed


def _prepare_candidate_transcripts(
    parser: SegmentParser,
    windows: list[AnalysisWindow],
    *,
    output_dir: Path,
    transcript_model: str,
    transcript_source: str,
    concurrency: int,
) -> dict[int, TranscriptAnalysis]:
    if concurrency <= 0:
        raise ValueError("asr_concurrency must be greater than zero")

    transcripts: dict[int, TranscriptAnalysis] = {}
    pending: list[AnalysisWindow] = []
    for window in windows:
        path = (
            output_dir
            / "transcripts"
            / f"segment-{window.sequence_no:04d}.json"
        )
        if not path.is_file():
            pending.append(window)
            continue
        checkpoint = TranscriptCheckpoint.model_validate_json(
            path.read_text(encoding="utf-8")
        )
        if checkpoint.transcript_model != transcript_model:
            raise RuntimeError(f"transcript model changed for checkpoint: {path}")
        if checkpoint.transcript_source != transcript_source:
            raise RuntimeError(f"transcript source changed for checkpoint: {path}")
        if checkpoint.window != window:
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
                / f"segment-{window.sequence_no:04d}.json"
            )
            _write_json(checkpoint, path)
            transcripts[window.sequence_no] = analysis
        print(
            f"candidate ASR disabled for {len(pending)} pending segments; "
            "Pro will use video audio and hard subtitles",
            flush=True,
        )
        return transcripts

    errors: list[tuple[AnalysisWindow, Exception]] = []
    with ThreadPoolExecutor(max_workers=min(concurrency, len(pending))) as executor:
        futures = {
            executor.submit(
                parser.transcribe,
                window.media_path,
                duration_ms=window.analysis_duration_ms,
            ): window
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
                / f"segment-{window.sequence_no:04d}.json"
            )
            _write_json(checkpoint, path)
            transcripts[window.sequence_no] = analysis
            print(
                f"ASR ready {len(transcripts)}/{len(windows)}; "
                f"segment={window.sequence_no}; cues={len(analysis.transcript)}",
                flush=True,
            )
    if errors:
        window, error = errors[0]
        raise RuntimeError(
            f"candidate ASR failed for segment {window.sequence_no}; "
            f"{len(errors)} segment(s) failed"
        ) from error
    return transcripts


def main() -> None:
    args = _build_parser().parse_args()
    if args.command != "parse-video":
        raise RuntimeError(f"unsupported command: {args.command}")

    config = load_config(args.config)
    parser_config = config.video_parser
    transcript_model = (
        args.transcript_model
        or os.getenv("ANIVAG_TRANSCRIPT_MODEL")
        or parser_config.transcript_model
    )
    video_model = (
        args.video_model
        or os.getenv("ANIVAG_VIDEO_MODEL")
        or parser_config.video_model
    )
    transcript_fps = args.transcript_fps
    if transcript_fps is None:
        transcript_fps = float(
            os.getenv("ANIVAG_TRANSCRIPT_FPS") or parser_config.transcript_fps
        )
    video_fps = args.video_fps
    if video_fps is None:
        video_fps = float(
            os.getenv("ANIVAG_VIDEO_FPS") or parser_config.video_fps
        )

    fallback_config = parser_config.fallback
    fallback_enabled = args.fallback
    if fallback_enabled is None:
        fallback_enabled = _environment_bool(
            "ANIVAG_FALLBACK_ENABLED",
            fallback_config.enabled,
        )
    deepseek_config = fallback_config.deepseek
    deepseek_api_key: str | None = None
    if fallback_enabled:
        deepseek_api_key = os.getenv(deepseek_config.api_key_env)
        if not deepseek_api_key:
            raise RuntimeError(
                "missing DeepSeek fallback credential in environment variable "
                f"{deepseek_config.api_key_env}"
            )

    video_path = args.video.expanduser().resolve()
    output_dir = args.output_dir.expanduser().resolve()
    duration_ms = probe_video_duration_ms(video_path)
    windows = plan_analysis_windows(
        video_path,
        duration_ms=duration_ms,
        output_dir=output_dir / "windows",
        segment_duration_ms=args.segment_duration_ms,
        context_overlap_ms=args.context_overlap_ms,
    )
    materialize_analysis_windows(windows, max_height=args.max_height)
    print(
        f"prepared {len(windows)} temporary analysis windows for {duration_ms} ms; "
        "ASR can run concurrently and semantic parsing will run serially",
        flush=True,
    )

    transcript_model_client = ArkMultimodalModel(
        transcript_model,
        request_timeout_seconds=args.request_timeout_seconds,
    )
    video_model_client = ArkMultimodalModel(
        video_model,
        request_timeout_seconds=args.request_timeout_seconds,
    )
    model_parser = SegmentParser(
        transcript_model_client,
        video_model_client,
        transcript_fps=transcript_fps,
        video_fps=video_fps,
    )
    semantic_parser: SegmentEvidenceParser = model_parser
    if fallback_enabled:
        assert deepseek_api_key is not None
        deepseek_model = DeepSeekMultimodalModel(
            deepseek_config.model,
            base_url=deepseek_config.base_url,
            api_key=deepseek_api_key,
            max_output_tokens=deepseek_config.max_output_tokens,
            request_timeout_seconds=deepseek_config.request_timeout_seconds,
            reasoning_effort=deepseek_config.reasoning_effort,
        )
        fallback_parser: SegmentEvidenceParser = DeepSeekFallbackSegmentParser(
            transcript_model_client,
            deepseek_model,
            deepseek_model,
            transcript_fps=transcript_fps,
            frame_fps=fallback_config.frame_fps,
            group_duration_ms=fallback_config.group_duration_ms,
            max_frames_per_group=fallback_config.max_frames_per_group,
            max_height=fallback_config.max_height,
        )

        def report_fallback(error) -> None:
            print(
                "Doubao output content filter triggered; "
                f"code={error.code or 'content_filter'}; "
                f"partial_output_chars={error.partial_output_chars}; "
                f"running Lite ASR and grouped {deepseek_config.model} "
                "visual/refiner fallback once",
                flush=True,
            )

        semantic_parser = ContentFilterFallbackSegmentParser(
            model_parser,
            fallback_parser,
            primary_model=video_model,
            on_fallback=report_fallback,
        )
        print(
            "content-filter fallback enabled; "
            f"provider={deepseek_config.provider}; model={deepseek_config.model}; "
            f"input_mode=ordered_frames; fps={fallback_config.frame_fps}; "
            f"group_duration_ms={fallback_config.group_duration_ms}; "
            f"max_frames_per_group={fallback_config.max_frames_per_group}",
            flush=True,
        )
    completed_segments = _load_completed_segments(
        windows,
        output_dir=output_dir,
        transcript_model=transcript_model,
        transcript_source=args.asr_mode,
        video_model=video_model,
        fallback_provider=deepseek_config.provider,
        fallback_model=deepseek_config.model,
        fallback_transcript_model=transcript_model,
        fallback_frame_fps=fallback_config.frame_fps,
        fallback_group_duration_ms=fallback_config.group_duration_ms,
        fallback_max_frames_per_group=fallback_config.max_frames_per_group,
    )
    if completed_segments:
        print(
            f"resuming after {len(completed_segments)}/{len(windows)} "
            "completed segments",
            flush=True,
        )
    candidate_transcripts = _prepare_candidate_transcripts(
        model_parser,
        windows[len(completed_segments) :],
        output_dir=output_dir,
        transcript_model=transcript_model,
        transcript_source=args.asr_mode,
        concurrency=args.asr_concurrency,
    )
    pipeline = ParsePipeline(
        semantic_parser,
        max_context_events=args.max_context_events,
        max_context_chars=args.max_context_chars,
    )

    def checkpoint(segment: SegmentParseResult) -> None:
        path = (
            output_dir
            / "segments"
            / f"segment-{segment.window.sequence_no:04d}.json"
        )
        _write_json(
            SegmentCheckpoint(
                transcript_model=transcript_model,
                transcript_source=args.asr_mode,
                video_model=video_model,
                segment=segment,
            ),
            path,
        )
        print(
            f"completed segment {segment.window.sequence_no + 1}/{len(windows)}; "
            f"owned_events={len(segment.events)}; "
            f"context={segment.context_after.snapshot_id}",
            flush=True,
        )

    result = pipeline.run(
        source_video=str(video_path),
        duration_ms=duration_ms,
        segment_duration_ms=args.segment_duration_ms,
        context_overlap_ms=args.context_overlap_ms,
        windows=windows,
        series_bible=_read_series_bible(args),
        candidate_transcripts=candidate_transcripts,
        completed_segments=completed_segments,
        on_segment_complete=checkpoint,
    )
    final_output = output_dir / "pipeline.parse.json"
    models = VideoParseModels(
        transcript=transcript_model if args.asr_mode == "lite" else None,
        transcript_mode=args.asr_mode,
        video_parser=video_model,
        fallback=VideoFallbackModels(
            enabled=fallback_enabled,
            transcript_model=transcript_model,
            provider=deepseek_config.provider,
            visual_model=deepseek_config.model,
            refiner_model=deepseek_config.model,
            input_mode="ordered_frames",
            frame_fps=fallback_config.frame_fps,
            group_duration_ms=fallback_config.group_duration_ms,
            max_frames_per_group=fallback_config.max_frames_per_group,
            max_height=fallback_config.max_height,
        ),
    )
    _write_json(result.model_copy(update={"models": models}), final_output)
    print(f"wrote {final_output}", flush=True)


def _environment_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean value")
