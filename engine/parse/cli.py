from __future__ import annotations

import argparse
import os
from pathlib import Path

from gateway.config import load_config
from gateway.models import ArkMultimodalModel, DeepSeekMultimodalModel

from .analysis_windows import (
    materialize_analysis_windows,
    plan_analysis_windows,
    probe_video_duration_ms,
)
from .checkpoints import (
    load_completed_chunks as _load_completed_chunks,
    stream_candidate_transcripts as _stream_candidate_transcripts,
    write_json as _write_json,
)
from .video_chunk_parse import (
    ContentFilterFallbackVideoChunkParser,
    DeepSeekFallbackVideoChunkParser,
    VideoChunkEvidenceParser,
    VideoChunkParser,
)
from .schemas import (
    VideoChunkCheckpoint,
    VideoChunkParseResult,
    VideoFallbackModels,
    VideoParseModels,
)
from .video_parse import ParsePipeline

_RUNTIME_CONTEXT_START = "<!-- ANIVAG_RUNTIME_CONTEXT_START -->"
_RUNTIME_CONTEXT_END = "<!-- ANIVAG_RUNTIME_CONTEXT_END -->"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m engine.parse.cli",
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
        help="Parse one video as an ordered multi-chunk timeline.",
    )
    parse_video.add_argument("--video", type=Path, required=True)
    parse_video.add_argument("--output-dir", type=Path, required=True)
    parse_video.add_argument("--chunk-duration-ms", type=int, default=60_000)
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
        chunk_duration_ms=args.chunk_duration_ms,
        context_overlap_ms=args.context_overlap_ms,
    )
    materialize_analysis_windows(windows, max_height=args.max_height)
    print(
        f"prepared {len(windows)} temporary analysis windows for {duration_ms} ms; "
        f"ASR runs with up to {args.asr_concurrency} workers while semantic "
        "parsing runs serially",
        flush=True,
    )

    transcript_model_client = ArkMultimodalModel(
        transcript_model,
        request_timeout_seconds=args.request_timeout_seconds,
    )
    video_model_client = ArkMultimodalModel(
        video_model,
        request_timeout_seconds=args.request_timeout_seconds,
        thinking_mode=parser_config.video_thinking,
    )
    model_parser = VideoChunkParser(
        transcript_model_client,
        video_model_client,
        transcript_fps=transcript_fps,
        video_fps=video_fps,
    )
    semantic_parser: VideoChunkEvidenceParser = model_parser
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
        fallback_parser: VideoChunkEvidenceParser = DeepSeekFallbackVideoChunkParser(
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

        semantic_parser = ContentFilterFallbackVideoChunkParser(
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
    completed_chunks = _load_completed_chunks(
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
    if completed_chunks:
        print(
            f"resuming after {len(completed_chunks)}/{len(windows)} "
            "completed chunks",
            flush=True,
        )
    pipeline = ParsePipeline(
        semantic_parser,
        max_context_events=args.max_context_events,
        max_context_chars=args.max_context_chars,
    )

    def checkpoint(chunk: VideoChunkParseResult) -> None:
        path = (
            output_dir
            / "chunks"
            / f"chunk-{chunk.window.sequence_no:04d}.json"
        )
        _write_json(
            VideoChunkCheckpoint(
                transcript_model=transcript_model,
                transcript_source=args.asr_mode,
                video_model=video_model,
                chunk=chunk,
            ),
            path,
        )
        print(
            f"completed chunk {chunk.window.sequence_no + 1}/{len(windows)}; "
            f"owned_events={len(chunk.events)}; "
            f"context={chunk.context_after.snapshot_id}",
            flush=True,
        )

    with _stream_candidate_transcripts(
        model_parser,
        windows[len(completed_chunks) :],
        output_dir=output_dir,
        transcript_model=transcript_model,
        transcript_source=args.asr_mode,
        concurrency=args.asr_concurrency,
    ) as candidate_transcripts:
        result = pipeline.run(
            source_video=str(video_path),
            duration_ms=duration_ms,
            chunk_duration_ms=args.chunk_duration_ms,
            context_overlap_ms=args.context_overlap_ms,
            windows=windows,
            series_bible=_read_series_bible(args),
            candidate_transcripts=candidate_transcripts,
            completed_chunks=completed_chunks,
            on_chunk_complete=checkpoint,
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


if __name__ == "__main__":
    main()
