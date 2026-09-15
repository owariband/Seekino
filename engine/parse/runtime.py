from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from gateway.config import load_config
from gateway.models import ArkMultimodalModel, DeepSeekMultimodalModel

from .analysis_windows import materialize_analysis_windows, plan_analysis_windows
from .background import BackgroundImage
from .checkpoints import (
    load_completed_chunks,
    prepare_candidate_transcripts,
    write_json,
)
from .schemas import (
    AnalysisWindow,
    VideoChunkCheckpoint,
    VideoChunkParseResult,
    VideoFallbackModels,
    VideoParseModels,
    VideoParseResult,
)
from .video_chunk_parse import (
    ContentFilterFallbackVideoChunkParser,
    DeepSeekFallbackVideoChunkParser,
    VideoChunkEvidenceParser,
    VideoChunkParser,
)
from .video_parse import ParsePipeline


@dataclass(frozen=True, slots=True)
class VideoParseRuntimeConfig:
    chunk_duration_ms: int = 60_000
    context_overlap_ms: int = 3_000
    max_height: int = 720
    asr_concurrency: int = 4
    asr_mode: Literal["lite", "none"] = "lite"
    request_timeout_seconds: float = 600
    max_context_events: int = 8
    max_context_chars: int = 2000
    transcript_model: str | None = None
    video_model: str | None = None
    transcript_fps: float | None = None
    video_fps: float | None = None
    fallback_enabled: bool | None = None

    def __post_init__(self) -> None:
        if self.chunk_duration_ms <= 0:
            raise ValueError("chunk_duration_ms must be greater than zero")
        if self.context_overlap_ms < 0:
            raise ValueError("context_overlap_ms cannot be negative")
        if self.max_height <= 0:
            raise ValueError("max_height must be greater than zero")
        if self.asr_concurrency <= 0:
            raise ValueError("asr_concurrency must be greater than zero")
        if self.request_timeout_seconds <= 0:
            raise ValueError("request_timeout_seconds must be greater than zero")


class EngineVideoParseRuntime:
    """Programmatic adapter over the same Engine pipeline used by the CLI."""

    def __init__(
        self,
        *,
        model_config_path: str | Path | None = None,
        config: VideoParseRuntimeConfig | None = None,
        on_log: Callable[[str], None] | None = None,
    ) -> None:
        self._model_config_path = model_config_path
        self._config = config or VideoParseRuntimeConfig()
        self._on_log = on_log or (lambda _message: None)

    def run(
        self,
        source_video: str | Path,
        *,
        duration_ms: int,
        output_dir: str | Path,
        series_bible: str | None = None,
        background_images: Sequence[BackgroundImage] = (),
        on_windows_ready: Callable[[list[AnalysisWindow]], None] | None = None,
        on_chunk_complete: (
            Callable[[VideoChunkParseResult, Path], None] | None
        ) = None,
    ) -> VideoParseResult:
        video_path = Path(source_video).expanduser().resolve(strict=True)
        work_directory = Path(output_dir).expanduser().resolve()
        gateway_config = load_config(self._model_config_path)
        parser_config = gateway_config.video_parser
        transcript_model = (
            self._config.transcript_model
            or os.getenv("ANIVAG_TRANSCRIPT_MODEL")
            or parser_config.transcript_model
        )
        video_model = (
            self._config.video_model
            or os.getenv("ANIVAG_VIDEO_MODEL")
            or parser_config.video_model
        )
        transcript_fps = self._config.transcript_fps or float(
            os.getenv("ANIVAG_TRANSCRIPT_FPS") or parser_config.transcript_fps
        )
        video_fps = self._config.video_fps or float(
            os.getenv("ANIVAG_VIDEO_FPS") or parser_config.video_fps
        )
        fallback_config = parser_config.fallback
        fallback_enabled = self._config.fallback_enabled
        if fallback_enabled is None:
            fallback_enabled = _environment_bool(
                "ANIVAG_FALLBACK_ENABLED",
                fallback_config.enabled,
            )

        windows = plan_analysis_windows(
            video_path,
            duration_ms=duration_ms,
            output_dir=work_directory / "windows",
            chunk_duration_ms=self._config.chunk_duration_ms,
            context_overlap_ms=self._config.context_overlap_ms,
        )
        materialize_analysis_windows(
            windows,
            max_height=self._config.max_height,
        )
        if on_windows_ready is not None:
            on_windows_ready(windows)

        deepseek = fallback_config.deepseek
        completed_chunks = load_completed_chunks(
            windows,
            output_dir=work_directory,
            transcript_model=transcript_model,
            transcript_source=self._config.asr_mode,
            video_model=video_model,
            fallback_provider=deepseek.provider,
            fallback_model=deepseek.model,
            fallback_transcript_model=transcript_model,
            fallback_frame_fps=fallback_config.frame_fps,
            fallback_group_duration_ms=fallback_config.group_duration_ms,
            fallback_max_frames_per_group=fallback_config.max_frames_per_group,
        )
        models = VideoParseModels(
            transcript=(
                transcript_model if self._config.asr_mode == "lite" else None
            ),
            transcript_mode=self._config.asr_mode,
            video_parser=video_model,
            fallback=VideoFallbackModels(
                enabled=fallback_enabled,
                transcript_model=transcript_model,
                provider=deepseek.provider,
                visual_model=deepseek.model,
                refiner_model=deepseek.model,
                input_mode="ordered_frames",
                frame_fps=fallback_config.frame_fps,
                group_duration_ms=fallback_config.group_duration_ms,
                max_frames_per_group=fallback_config.max_frames_per_group,
                max_height=fallback_config.max_height,
            ),
        )
        if len(completed_chunks) == len(windows):
            completed = VideoParseResult(
                models=models,
                source_video=video_path,
                duration_ms=duration_ms,
                chunk_duration_ms=self._config.chunk_duration_ms,
                context_overlap_ms=self._config.context_overlap_ms,
                chunks=completed_chunks,
                events=sorted(
                    (
                        event
                        for chunk in completed_chunks
                        for event in chunk.events
                    ),
                    key=lambda event: (event.start_ms, event.end_ms),
                ),
            )
            write_json(completed, work_directory / "pipeline.parse.json")
            return completed

        transcript_client = ArkMultimodalModel(
            transcript_model,
            request_timeout_seconds=self._config.request_timeout_seconds,
        )
        video_client = ArkMultimodalModel(
            video_model,
            request_timeout_seconds=self._config.request_timeout_seconds,
        )
        model_parser = VideoChunkParser(
            transcript_client,
            video_client,
            transcript_fps=transcript_fps,
            video_fps=video_fps,
        )
        semantic_parser: VideoChunkEvidenceParser = model_parser
        if fallback_enabled:
            api_key = os.getenv(deepseek.api_key_env)
            if not api_key:
                raise RuntimeError(
                    "missing DeepSeek fallback credential in environment variable "
                    f"{deepseek.api_key_env}"
                )
            fallback_model = DeepSeekMultimodalModel(
                deepseek.model,
                base_url=deepseek.base_url,
                api_key=api_key,
                max_output_tokens=deepseek.max_output_tokens,
                request_timeout_seconds=deepseek.request_timeout_seconds,
                reasoning_effort=deepseek.reasoning_effort,
            )
            fallback_parser = DeepSeekFallbackVideoChunkParser(
                transcript_client,
                fallback_model,
                fallback_model,
                transcript_fps=transcript_fps,
                frame_fps=fallback_config.frame_fps,
                group_duration_ms=fallback_config.group_duration_ms,
                max_frames_per_group=fallback_config.max_frames_per_group,
                max_height=fallback_config.max_height,
            )
            semantic_parser = ContentFilterFallbackVideoChunkParser(
                model_parser,
                fallback_parser,
                primary_model=video_model,
                on_fallback=lambda error: self._on_log(
                    "content-filter fallback; "
                    f"code={error.code or 'content_filter'}"
                ),
            )

        candidate_transcripts = prepare_candidate_transcripts(
            model_parser,
            windows[len(completed_chunks) :],
            output_dir=work_directory,
            transcript_model=transcript_model,
            transcript_source=self._config.asr_mode,
            concurrency=self._config.asr_concurrency,
            on_log=self._on_log,
        )
        pipeline = ParsePipeline(
            semantic_parser,
            max_context_events=self._config.max_context_events,
            max_context_chars=self._config.max_context_chars,
        )

        def checkpoint(chunk: VideoChunkParseResult) -> None:
            path = (
                work_directory
                / "chunks"
                / f"chunk-{chunk.window.sequence_no:04d}.json"
            )
            write_json(
                VideoChunkCheckpoint(
                    transcript_model=transcript_model,
                    transcript_source=self._config.asr_mode,
                    video_model=video_model,
                    chunk=chunk,
                ),
                path,
            )
            if on_chunk_complete is not None:
                on_chunk_complete(chunk, path)

        result = pipeline.run(
            source_video=str(video_path),
            duration_ms=duration_ms,
            chunk_duration_ms=self._config.chunk_duration_ms,
            context_overlap_ms=self._config.context_overlap_ms,
            windows=windows,
            series_bible=series_bible,
            background_images=background_images,
            candidate_transcripts=candidate_transcripts,
            completed_chunks=completed_chunks,
            on_chunk_complete=checkpoint,
        )
        completed = result.model_copy(update={"models": models})
        write_json(completed, work_directory / "pipeline.parse.json")
        return completed


def _environment_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean value")
