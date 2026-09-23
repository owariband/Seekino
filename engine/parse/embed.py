from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol

from gateway.embedding import EmbeddingResult

from engine.retrieval.video_retrieval import (
    VideoChunkRetrievalDocument,
    build_video_chunk_retrieval_documents,
)

from .schemas import VideoParseResult
from .usage import parse_chunk_scope

if TYPE_CHECKING:
    from .persist import ParsePersistence


class EmbeddingAgent(Protocol):
    def embed_text(self, text: str) -> EmbeddingResult: ...

    def embed_video(self, video_path: str | Path) -> EmbeddingResult: ...


@dataclass(frozen=True, slots=True)
class ChunkEmbedding:
    sequence_no: int
    modality: Literal["text", "video"]
    input_sha256: str
    result: EmbeddingResult

    def __post_init__(self) -> None:
        if self.sequence_no < 0:
            raise ValueError("sequence_no cannot be negative")
        if len(self.input_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in self.input_sha256
        ):
            raise ValueError("input_sha256 must be a lowercase SHA-256 hex digest")
        if self.result.modality != self.modality:
            raise ValueError("embedding result modality does not match")


@dataclass(frozen=True, slots=True)
class EmbeddingStageResult:
    documents: Mapping[int, VideoChunkRetrievalDocument]
    text_embeddings: Mapping[int, ChunkEmbedding]
    vision_embeddings: Mapping[int, ChunkEmbedding]


class EmbedStage:
    def __init__(
        self,
        agent: EmbeddingAgent,
        *,
        dimensions: int,
    ) -> None:
        if dimensions <= 0:
            raise ValueError("dimensions must be greater than zero")
        self._agent = agent
        self._dimensions = dimensions

    def run(
        self,
        parse_result: VideoParseResult,
        *,
        canonical_media_paths: Mapping[int, Path],
        canonical_media_sha256: Mapping[int, str],
        persistence: ParsePersistence,
    ) -> EmbeddingStageResult:
        documents = {
            document.sequence_no: document
            for document in build_video_chunk_retrieval_documents(parse_result)
        }
        expected_sequences = set(documents)
        if set(canonical_media_paths) != expected_sequences:
            raise ValueError(
                "canonical_media_paths must contain every parsed VideoChunk exactly once"
            )
        if set(canonical_media_sha256) != expected_sequences:
            raise ValueError(
                "canonical_media_sha256 must contain every parsed VideoChunk exactly once"
            )

        persistence.start_stage("text_embedding")
        text_embeddings = {
            sequence_no: self._embed_text(
                documents[sequence_no],
                persistence=persistence,
            )
            for sequence_no in sorted(documents)
        }

        persistence.start_stage("vision_embedding")
        vision_embeddings = {
            sequence_no: self._embed_video(
                sequence_no,
                canonical_media_paths[sequence_no],
                canonical_media_sha256[sequence_no],
                persistence=persistence,
            )
            for sequence_no in sorted(canonical_media_paths)
        }
        return EmbeddingStageResult(
            documents=documents,
            text_embeddings=text_embeddings,
            vision_embeddings=vision_embeddings,
        )

    def _embed_text(
        self,
        document: VideoChunkRetrievalDocument,
        *,
        persistence: ParsePersistence,
    ) -> ChunkEmbedding:
        input_sha256 = hashlib.sha256(document.text.encode("utf-8")).hexdigest()
        existing = persistence.load_embedding(
            sequence_no=document.sequence_no,
            modality="text",
            input_sha256=input_sha256,
        )
        if existing is not None:
            self._validate_embedding(existing, expected_modality="text")
            return existing

        with parse_chunk_scope(document.sequence_no, category="embedding"):
            result = self._agent.embed_text(document.text)
        persistence.ensure_active()
        self._validate_result(result, expected_modality="text")
        embedding = ChunkEmbedding(
            sequence_no=document.sequence_no,
            modality="text",
            input_sha256=input_sha256,
            result=result,
        )
        self._validate_embedding(embedding, expected_modality="text")
        persistence.save_embedding(embedding)
        return embedding

    def _embed_video(
        self,
        sequence_no: int,
        media_path: Path,
        input_sha256: str,
        *,
        persistence: ParsePersistence,
    ) -> ChunkEmbedding:
        existing = persistence.load_embedding(
            sequence_no=sequence_no,
            modality="video",
            input_sha256=input_sha256,
        )
        if existing is not None:
            self._validate_embedding(existing, expected_modality="video")
            return existing

        with parse_chunk_scope(sequence_no, category="embedding"):
            result = self._agent.embed_video(media_path)
        persistence.ensure_active()
        self._validate_result(result, expected_modality="video")
        embedding = ChunkEmbedding(
            sequence_no=sequence_no,
            modality="video",
            input_sha256=input_sha256,
            result=result,
        )
        self._validate_embedding(embedding, expected_modality="video")
        persistence.save_embedding(embedding)
        return embedding

    def _validate_embedding(
        self,
        embedding: ChunkEmbedding,
        *,
        expected_modality: Literal["text", "video"],
    ) -> None:
        if embedding.modality != expected_modality:
            raise RuntimeError(
                f"embedding persistence returned {embedding.modality!r} "
                f"for {expected_modality!r}"
            )
        self._validate_result(
            embedding.result,
            expected_modality=expected_modality,
        )

    def _validate_result(
        self,
        result: EmbeddingResult,
        *,
        expected_modality: Literal["text", "video"],
    ) -> None:
        if result.modality != expected_modality:
            raise RuntimeError(
                f"embedding gateway returned {result.modality!r} "
                f"for {expected_modality!r}"
            )
        if len(result.vector) != self._dimensions:
            raise RuntimeError(
                "embedding gateway returned an unexpected vector dimension"
            )
        if any(not math.isfinite(float(value)) for value in result.vector):
            raise RuntimeError("embedding gateway returned a non-finite vector")
