from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from engine.retrieval.video_retrieval import VideoChunkRetrievalDocument

from .background import BackgroundImage
from .embed import ChunkEmbedding, EmbedStage
from .persist import ParsePersistence
from .schemas import (
    AnalysisWindow,
    VideoChunkParseResult,
    VideoParseModels,
    VideoParseResult,
)
from .transcribe import TranscribeStage
from .understand import UnderstandStage


@dataclass(frozen=True, slots=True)
class ParseChunkInput:
    window: AnalysisWindow
    canonical_media_path: Path
    canonical_media_sha256: str

    def __post_init__(self) -> None:
        if len(self.canonical_media_sha256) != 64 or any(
            character not in "0123456789abcdef"
            for character in self.canonical_media_sha256
        ):
            raise ValueError(
                "canonical_media_sha256 must be a lowercase SHA-256 hex digest"
            )


@dataclass(frozen=True, slots=True)
class ParsePipelineInput:
    source_video: Path
    duration_seconds: int
    chunk_duration_seconds: int
    context_overlap_seconds: int
    chunks: tuple[ParseChunkInput, ...]
    series_bible: str | None = None
    background_images: tuple[BackgroundImage, ...] = ()

    def __post_init__(self) -> None:
        if self.duration_seconds <= 0:
            raise ValueError("duration_seconds must be greater than zero")
        if self.chunk_duration_seconds <= 0:
            raise ValueError("chunk_duration_seconds must be greater than zero")
        if self.context_overlap_seconds < 0:
            raise ValueError("context_overlap_seconds cannot be negative")
        if not self.chunks:
            raise ValueError("at least one ParseChunkInput is required")
        if [item.window.sequence_no for item in self.chunks] != list(
            range(len(self.chunks))
        ):
            raise ValueError("ParseChunkInputs must have contiguous sequence numbers")


@dataclass(frozen=True, slots=True)
class ParsePipelineResult:
    parse_result: VideoParseResult
    documents: Mapping[int, VideoChunkRetrievalDocument]
    text_embeddings: Mapping[int, ChunkEmbedding]
    vision_embeddings: Mapping[int, ChunkEmbedding]


class ParsePipeline:
    """Production algorithm flow with durability delegated to one port."""

    def __init__(
        self,
        *,
        models: VideoParseModels,
        transcribe: TranscribeStage,
        understand: UnderstandStage,
        embed: EmbedStage,
    ) -> None:
        self._models = models
        self._transcribe = transcribe
        self._understand = understand
        self._embed = embed

    @property
    def models(self) -> VideoParseModels:
        return self._models

    def run(
        self,
        request: ParsePipelineInput,
        *,
        persistence: ParsePersistence,
    ) -> list[str]:
        windows = tuple(item.window for item in request.chunks)
        persistence.start_stage("transcribe")
        completed_chunks = tuple(
            persistence.load_completed_chunks(windows, models=self._models)
        )
        if len(completed_chunks) > len(windows):
            raise ValueError("completed chunks cannot outnumber analysis windows")
        pending_windows = windows[len(completed_chunks) :]

        transcripts = self._transcribe.run(
            pending_windows,
            ensure_active=persistence.ensure_active,
        )

        persistence.start_stage("understand")

        def save_chunk(chunk: VideoChunkParseResult) -> None:
            persistence.ensure_active()
            persistence.save_chunk(chunk, models=self._models)

        parse_result = self._understand.run(
            source_video=str(request.source_video),
            duration_seconds=request.duration_seconds,
            chunk_duration_seconds=request.chunk_duration_seconds,
            context_overlap_seconds=request.context_overlap_seconds,
            windows=windows,
            series_bible=request.series_bible,
            background_images=request.background_images,
            candidate_transcripts=transcripts,
            completed_chunks=completed_chunks,
            ensure_active=persistence.ensure_active,
            on_chunk_complete=save_chunk,
        ).model_copy(update={"models": self._models})
        persistence.ensure_active()
        persistence.save_parse_result(parse_result)

        embedded = self._embed.run(
            parse_result,
            canonical_media_paths={
                item.window.sequence_no: item.canonical_media_path
                for item in request.chunks
            },
            canonical_media_sha256={
                item.window.sequence_no: item.canonical_media_sha256
                for item in request.chunks
            },
            persistence=persistence,
        )
        result = ParsePipelineResult(
            parse_result=parse_result,
            documents=embedded.documents,
            text_embeddings=embedded.text_embeddings,
            vision_embeddings=embedded.vision_embeddings,
        )

        persistence.start_stage("persist")
        persistence.ensure_active()
        return list(persistence.persist(result))
