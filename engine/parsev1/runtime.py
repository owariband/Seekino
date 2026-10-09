from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Literal

from engine.common.config import VideoParseRuntimeConfig
from engine.common.model_factory import build_asr_agent
from engine.common.transcription import TranscriptAnalysis, VideoASRAgentProtocol
from engine.common.usage import parse_chunk_scope
from gateway.ark.multimodal import ArkMultimodalModel
from gateway.config import (
    GatewayConfig,
    OpenAIVisionSourceConfig,
    VideoFallbackConfig,
    load_config,
)
from gateway.openai.multimodal import DeepSeekMultimodalModel

from .analysis_windows import materialize_analysis_windows, plan_analysis_windows
from .background import BackgroundImage
from .checkpoints import (
    load_completed_chunks,
    stream_candidate_transcripts,
    write_json,
)
from .embed import EmbeddingAgent, EmbedStage
from .model_factory import build_fallback_model, build_vision_model
from .pipeline import ParsePipeline
from .schemas import (
    AnalysisWindow,
    ContextSnapshot,
    VideoChunkCheckpoint,
    VideoChunkParseResult,
    VideoEvidence,
    VideoFallbackModels,
    VideoParseModels,
    VideoParseResult,
)
from .transcribe import TranscribeStage
from .understand import UnderstandStage
from .video_chunk_parse import (
    ContentFilterVideoUnderstandingAgent,
    FrameFallbackVideoUnderstandingAgent,
    OrderedFrameVideoUnderstandingAgent,
    VideoUnderstandingAgent,
    VideoUnderstandingAgentProtocol,
)


@dataclass(frozen=True, slots=True)
class _ResolvedRuntimeConfig:
    gateway: GatewayConfig
    transcript_model: str
    video_model: str
    transcript_fps: float
    video_fps: float
    fallback_enabled: bool
    fallback: VideoFallbackConfig


class EngineASRAgent:
    """Server-facing ASR port backed by one Engine composition runtime."""

    def __init__(self, runtime: EngineVideoParseRuntime) -> None:
        self._runtime = runtime

    def transcribe_chunk(
        self,
        video_chunk: AnalysisWindow,
    ) -> TranscriptAnalysis:
        return self._runtime.transcribe_chunk(video_chunk)


class EngineVideoUnderstandingAgent:
    """Server-facing understanding port backed by one Engine runtime."""

    def __init__(self, runtime: EngineVideoParseRuntime) -> None:
        self._runtime = runtime

    def understand_chunk(
        self,
        compact: ContextSnapshot,
        video_chunk: AnalysisWindow,
        *,
        transcript: TranscriptAnalysis,
        series_bible: str | None = None,
        background_images: Sequence[BackgroundImage] = (),
    ) -> VideoChunkParseResult:
        return self._runtime.understand_chunk(
            compact,
            video_chunk,
            transcript=transcript,
            series_bible=series_bible,
            background_images=background_images,
        )


class _LazyVideoUnderstandingAgent:
    """Delay provider construction until an unfinished chunk needs a model."""

    def __init__(self, runtime: EngineVideoParseRuntime) -> None:
        self._runtime = runtime

    def understand(
        self,
        video_path: str | Path,
        *,
        transcript: TranscriptAnalysis,
        context: str | None = None,
        duration_seconds: int | None = None,
        background_images: Sequence[BackgroundImage] = (),
    ) -> VideoEvidence:
        return self._runtime._get_understanding_agent().understand(
            video_path,
            transcript=transcript,
            context=context,
            duration_seconds=duration_seconds,
            background_images=background_images,
        )


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
        self._assembly_lock = Lock()
        self._resolved: _ResolvedRuntimeConfig | None = None
        self._asr_agent: VideoASRAgentProtocol | None = None
        self._understanding_agent: VideoUnderstandingAgentProtocol | None = None
        self._understand_stage: UnderstandStage | None = None
        self._server_asr_agent = EngineASRAgent(self)
        self._server_understanding_agent = EngineVideoUnderstandingAgent(self)

    @property
    def server_asr_agent(self) -> EngineASRAgent:
        return self._server_asr_agent

    @property
    def server_understanding_agent(self) -> EngineVideoUnderstandingAgent:
        return self._server_understanding_agent

    def describe_models(
        self,
        *,
        asr_enabled: bool,
    ) -> VideoParseModels:
        resolved = self._resolve_config()
        fallback = resolved.fallback
        transcript_mode: Literal["lite", "none"] = (
            "lite" if asr_enabled else "none"
        )
        return VideoParseModels(
            transcript=(
                resolved.transcript_model if transcript_mode == "lite" else None
            ),
            transcript_mode=transcript_mode,
            video_parser=resolved.video_model,
            fallback=VideoFallbackModels(
                enabled=resolved.fallback_enabled,
                transcript_model=resolved.transcript_model,
                provider=fallback.deepseek.provider,
                visual_model=fallback.deepseek.model,
                refiner_model=fallback.deepseek.model,
                input_mode="ordered_frames",
                frame_fps=fallback.frame_fps,
                group_duration_seconds=fallback.group_duration_seconds,
                max_frames_per_group=fallback.max_frames_per_group,
                max_height=fallback.max_height,
            ),
        )

    def build_pipeline(
        self,
        embedding_agent: EmbeddingAgent,
        embedding_dimensions: int,
    ) -> ParsePipeline:
        """Compose the production Parse pipeline around the configured models."""

        asr_agent = self if self._config.asr_mode == "lite" else None
        return ParsePipeline(
            models=self.describe_models(
                asr_enabled=self._config.asr_mode == "lite"
            ),
            transcribe=TranscribeStage(
                asr_agent,
                concurrency=self._config.asr_concurrency,
            ),
            understand=UnderstandStage(
                _LazyVideoUnderstandingAgent(self),
                max_context_events=self._config.max_context_events,
                max_context_chars=self._config.max_context_chars,
            ),
            embed=EmbedStage(
                embedding_agent,
                dimensions=embedding_dimensions,
            ),
        )

    def transcribe_chunk(
        self,
        video_chunk: AnalysisWindow,
    ) -> TranscriptAnalysis:
        if self._config.asr_mode != "lite":
            raise RuntimeError("ASR is disabled for this runtime")
        asr_agent = self._get_asr_agent()
        with parse_chunk_scope(video_chunk.sequence_no):
            return asr_agent.transcribe(
                video_chunk.media_path,
                duration_seconds=video_chunk.analysis_duration_seconds,
            )

    def understand_chunk(
        self,
        compact: ContextSnapshot,
        video_chunk: AnalysisWindow,
        *,
        transcript: TranscriptAnalysis,
        series_bible: str | None = None,
        background_images: Sequence[BackgroundImage] = (),
    ) -> VideoChunkParseResult:
        return self._get_understand_stage().understand_chunk(
            compact,
            video_chunk,
            transcript=transcript,
            series_bible=series_bible,
            background_images=background_images,
        )

    def run(
        self,
        source_video: str | Path,
        *,
        duration_seconds: int,
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
        resolved = self._resolve_config()
        transcript_model = resolved.transcript_model
        video_model = resolved.video_model
        fallback_config = resolved.fallback

        windows = plan_analysis_windows(
            video_path,
            duration_seconds=duration_seconds,
            output_dir=work_directory / "windows",
            chunk_duration_seconds=self._config.chunk_duration_seconds,
            context_overlap_seconds=self._config.context_overlap_seconds,
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
            fallback_transcript_model=(
                transcript_model if self._config.asr_mode == "lite" else None
            ),
            fallback_frame_fps=fallback_config.frame_fps,
            fallback_group_duration_seconds=fallback_config.group_duration_seconds,
            fallback_max_frames_per_group=fallback_config.max_frames_per_group,
        )
        models = self.describe_models(
            asr_enabled=self._config.asr_mode == "lite"
        )
        if len(completed_chunks) == len(windows):
            completed = VideoParseResult(
                models=models,
                source_video=video_path,
                duration_seconds=duration_seconds,
                chunk_duration_seconds=self._config.chunk_duration_seconds,
                context_overlap_seconds=self._config.context_overlap_seconds,
                chunks=completed_chunks,
                events=sorted(
                    (
                        event
                        for chunk in completed_chunks
                        for event in chunk.events
                    ),
                    key=lambda event: (event.start_seconds, event.end_seconds),
                ),
            )
            write_json(completed, work_directory / "pipeline.parse.json")
            return completed

        asr_agent = (
            self._get_asr_agent()
            if self._config.asr_mode == "lite"
            else None
        )
        understand_stage = self._get_understand_stage()

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

        with stream_candidate_transcripts(
            asr_agent,
            windows[len(completed_chunks) :],
            output_dir=work_directory,
            transcript_model=transcript_model,
            transcript_source=self._config.asr_mode,
            concurrency=self._config.asr_concurrency,
            on_log=self._on_log,
        ) as candidate_transcripts:
            result = understand_stage.run(
                source_video=str(video_path),
                duration_seconds=duration_seconds,
                chunk_duration_seconds=self._config.chunk_duration_seconds,
                context_overlap_seconds=self._config.context_overlap_seconds,
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

    def _resolve_config(self) -> _ResolvedRuntimeConfig:
        with self._assembly_lock:
            if self._resolved is None:
                gateway_config = load_config(self._model_config_path)
                parser_config = gateway_config.video_parser
                fallback_enabled = self._config.fallback_enabled
                if fallback_enabled is None:
                    fallback_enabled = _environment_bool(
                        "ANIVAG_FALLBACK_ENABLED",
                        parser_config.fallback.enabled,
                    )
                self._resolved = _ResolvedRuntimeConfig(
                    gateway=gateway_config,
                    transcript_model=(
                        self._config.transcript_model
                        or os.getenv("ANIVAG_TRANSCRIPT_MODEL")
                        or parser_config.transcript_model
                    ),
                    video_model=(
                        self._config.video_model
                        or os.getenv("ANIVAG_VIDEO_MODEL")
                        or parser_config.video_model
                    ),
                    transcript_fps=(
                        self._config.transcript_fps
                        or float(
                            os.getenv("ANIVAG_TRANSCRIPT_FPS")
                            or parser_config.transcript_fps
                        )
                    ),
                    video_fps=(
                        self._config.video_fps
                        or float(
                            os.getenv("ANIVAG_VIDEO_FPS")
                            or parser_config.video_fps
                        )
                    ),
                    fallback_enabled=fallback_enabled,
                    fallback=self._config.fallback_config or parser_config.fallback,
                )
            return self._resolved

    def _get_asr_agent(self) -> VideoASRAgentProtocol:
        resolved = self._resolve_config()
        with self._assembly_lock:
            if self._asr_agent is None:
                parser = resolved.gateway.video_parser
                self._asr_agent = build_asr_agent(
                    resolved.gateway, model=resolved.transcript_model,
                    fps=resolved.transcript_fps,
                    timeout=parser.transcription_timeout_seconds or self._config.request_timeout_seconds,
                    max_output_tokens=self._config.transcript_max_output_tokens or parser.transcription_max_output_tokens,
                    ark_model_type=ArkMultimodalModel,
                )
            return self._asr_agent

    def _get_understanding_agent(self) -> VideoUnderstandingAgentProtocol:
        resolved = self._resolve_config()
        with self._assembly_lock:
            if self._understanding_agent is not None:
                return self._understanding_agent

            parser = resolved.gateway.video_parser
            video_client = build_vision_model(
                resolved.gateway, model=resolved.video_model,
                timeout=parser.vision_timeout_seconds or self._config.request_timeout_seconds,
                max_output_tokens=self._config.video_max_output_tokens or parser.vision_max_output_tokens,
                ark_model_type=ArkMultimodalModel,
            )
            if self._config.video_input_mode == "ordered_frames" or isinstance(resolved.gateway.stage_source("vision"), OpenAIVisionSourceConfig):
                understanding_agent: VideoUnderstandingAgentProtocol = OrderedFrameVideoUnderstandingAgent(
                    video_client,
                    frame_fps=resolved.video_fps,
                    max_frames=self._config.video_max_frames_per_window or parser.max_frames_per_window,
                    max_height=self._config.max_height,
                )
            else:
                understanding_agent = VideoUnderstandingAgent(
                    video_client,
                    fps=resolved.video_fps,
                )
            if resolved.fallback_enabled:
                deepseek = resolved.fallback.deepseek
                fallback_model = build_fallback_model(
                    resolved.gateway, deepseek, deepseek_model_type=DeepSeekMultimodalModel,
                )
                fallback_agent = FrameFallbackVideoUnderstandingAgent(
                    fallback_model,
                    fallback_model,
                    asr_model_name=(
                        resolved.transcript_model
                        if self._config.asr_mode == "lite"
                        else None
                    ),
                    frame_fps=resolved.fallback.frame_fps,
                    group_duration_seconds=resolved.fallback.group_duration_seconds,
                    max_frames_per_group=(
                        resolved.fallback.max_frames_per_group
                    ),
                    max_height=resolved.fallback.max_height,
                )
                understanding_agent = ContentFilterVideoUnderstandingAgent(
                    understanding_agent,
                    fallback_agent,
                    primary_model=resolved.video_model,
                    on_fallback=lambda error: self._on_log(
                        "content-filter fallback; "
                        f"code={error.code or 'content_filter'}"
                    ),
                )
            self._understanding_agent = understanding_agent
            return self._understanding_agent

    def _get_understand_stage(self) -> UnderstandStage:
        understanding_agent = self._get_understanding_agent()
        with self._assembly_lock:
            if self._understand_stage is None:
                self._understand_stage = UnderstandStage(
                    understanding_agent,
                    max_context_events=self._config.max_context_events,
                    max_context_chars=self._config.max_context_chars,
                )
            return self._understand_stage


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
