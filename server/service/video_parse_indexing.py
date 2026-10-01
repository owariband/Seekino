"""Projection, embedding reuse and Elasticsearch convergence for Parse V2."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from engine.parsev2.schemas import VideoParseResult
from engine.parsev1.usage import parse_chunk_scope
from engine.retrieval.index_projection import (
    ChunkIndexProjection,
    project_chunk_index,
)
from gateway.embedding import EmbeddingResult

from ..model.dto.jobDTO import (
    IndexableChunkState,
    IntervalIndexingSnapshot,
    IntervalParseLease,
)
from ..storage.elasticsearch import (
    ElasticsearchIndexSchemaError,
    ElasticsearchVideoChunkStore,
    StoredDocumentState,
    VideoChunkIndexDocument,
)
from .embedding_reuse import EmbeddingReuseTarget, assess_embedding_reuse
from .video_parse_media import MediaLeaseIdentity, VideoParseMediaService


INDEX_EMBEDDINGS_DETAIL_KEY = "_index_embeddings_v2"
_ENCODER_OUTPUT_CONTRACT = "float-vector-v1"
_ENCODER_NORMALIZATION = "none"
_MAX_INDEX_GENERATION = (1 << 31) - 1
MEDIA_PREPROCESS_REVISION = "ffmpeg-h264-crf23-scale-cap-v1"


class VideoParseIndexingRepository(Protocol):
    def load_interval_indexing_snapshot(
        self, lease: IntervalParseLease, *, now=None,
    ) -> IntervalIndexingSnapshot: ...

    def save_index_embeddings(
        self,
        lease: IntervalParseLease,
        chunk_parse_task_id: str,
        content_version: int,
        representations: Mapping[str, Any],
        *,
        now=None,
    ) -> None: ...

    def complete_interval_indexing(
        self,
        lease: IntervalParseLease,
        indexed_versions: Mapping[str, int],
        *,
        now=None,
    ) -> object: ...


class MultimodalChunkEmbedder(Protocol):
    def embed_text(self, text: str) -> EmbeddingResult: ...

    def embed_video(self, video_path: str | Path) -> EmbeddingResult: ...


@dataclass(frozen=True, slots=True)
class VideoParseIndexingContract:
    retrieval_contract_id: str
    semantic_provider: str
    semantic_model: str
    semantic_encoder_revision: str
    semantic_dimensions: int
    vision_provider: str
    vision_model: str
    vision_encoder_revision: str
    vision_dimensions: int
    vision_fps: float
    vision_max_height: int
    vision_mime_type: str = "video/mp4"
    description_language: str | None = None
    index_generation: int = 1

    def __post_init__(self) -> None:
        strings = (
            self.retrieval_contract_id,
            self.semantic_provider,
            self.semantic_model,
            self.semantic_encoder_revision,
            self.vision_provider,
            self.vision_model,
            self.vision_encoder_revision,
            self.vision_mime_type,
        )
        if any(not isinstance(value, str) or not value.strip() for value in strings):
            raise ValueError("indexing contract strings must not be blank")
        for name, value in (
            ("semantic_dimensions", self.semantic_dimensions),
            ("vision_dimensions", self.vision_dimensions),
            ("vision_max_height", self.vision_max_height),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if (
            isinstance(self.index_generation, bool)
            or not isinstance(self.index_generation, int)
            or not 1 <= self.index_generation <= _MAX_INDEX_GENERATION
        ):
            raise ValueError(
                "index_generation must fit the Elasticsearch version contract"
            )
        if (
            isinstance(self.vision_fps, bool)
            or not isinstance(self.vision_fps, (int, float))
            or not math.isfinite(self.vision_fps)
            or self.vision_fps <= 0
        ):
            raise ValueError("vision_fps must be a positive finite number")
        if self.description_language is not None and not self.description_language.strip():
            raise ValueError("description_language must not be blank")

    @property
    def semantic_encoder_id(self) -> str:
        return _encoder_contract_id(
            "text",
            {
                "provider": self.semantic_provider,
                "model": self.semantic_model,
                "encoder_revision": self.semantic_encoder_revision,
                "dimensions": self.semantic_dimensions,
                "normalization": _ENCODER_NORMALIZATION,
                "output_contract": _ENCODER_OUTPUT_CONTRACT,
            },
        )

    @property
    def vision_encoder_id(self) -> str:
        return _encoder_contract_id(
            "video",
            {
                "provider": self.vision_provider,
                "model": self.vision_model,
                "encoder_revision": self.vision_encoder_revision,
                "dimensions": self.vision_dimensions,
                "normalization": _ENCODER_NORMALIZATION,
                "output_contract": _ENCODER_OUTPUT_CONTRACT,
                "video_fps": self.vision_fps,
                "mime_type": self.vision_mime_type,
                "max_height": self.vision_max_height,
                "media_preprocess_revision": MEDIA_PREPROCESS_REVISION,
            },
        )


class VideoParseIndexingError(RuntimeError):
    """The current Interval could not make every released Chunk query-visible."""


class VideoParseIndexingService:
    """Converge one claimed Interval without holding a database transaction."""

    def __init__(
        self,
        repository: VideoParseIndexingRepository,
        store: ElasticsearchVideoChunkStore,
        embedder: MultimodalChunkEmbedder,
        *,
        media_resolver: Callable[[str], Path],
        media_service: VideoParseMediaService,
        contract: VideoParseIndexingContract,
        check_active: Callable[[], None] | None = None,
    ) -> None:
        if store.semantic_dimensions != contract.semantic_dimensions:
            raise ValueError("semantic dimensions differ between contract and store")
        if store.vision_dimensions != contract.vision_dimensions:
            raise ValueError("vision dimensions differ between contract and store")
        self._repository = repository
        self._store = store
        self._embedder = embedder
        self._media_resolver = media_resolver
        self._media_service = media_service
        self._check_active = check_active or (lambda: None)
        self.contract = contract

    def index_interval(self, lease: IntervalParseLease) -> object:
        """Index all currently released members, then fenced-complete the Interval."""

        self._check_active()
        snapshot = self._repository.load_interval_indexing_snapshot(lease)
        self._check_active()
        keys = [
            (snapshot.video_parse_task_id, chunk.video_chunk_id)
            for chunk in snapshot.chunks
        ]
        stored = self._store.inspect(keys)
        if set(stored) != set(keys):
            raise VideoParseIndexingError(
                "Elasticsearch inspection omitted an expected Chunk"
            )
        self._check_active()
        documents = [
            self._build_document(
                lease,
                snapshot,
                chunk,
                stored[(snapshot.video_parse_task_id, chunk.video_chunk_id)],
            )
            for chunk in snapshot.chunks
        ]
        pending = [
            document
            for document in documents
            if not self._store.is_ready([document])
        ]
        if pending:
            self._check_active()
            try:
                self._store.bulk_upsert(pending, refresh="wait_for")
            except (ValueError, ElasticsearchIndexSchemaError):
                # Validation and mapping failures happen before an admissible
                # write and must never be converted into idempotent success.
                raise
            except Exception:
                # Bulk responses and request timeouts can be ambiguous: some
                # or all documents may already be durable. Real-time exact
                # readback distinguishes an idempotent replay from a same-
                # version conflict carrying different source data.
                self._check_active()
                exact = self._store.inspect_exact(pending)
                expected_ids = {document.document_id for document in pending}
                if set(exact) != expected_ids or not all(exact.values()):
                    raise
            self._check_active()
            if not self._store.verify_ready(documents):
                raise VideoParseIndexingError(
                    "Elasticsearch documents are durable but not query-visible"
                )
        elif documents and not self._store.verify_ready(documents):
            raise VideoParseIndexingError(
                "Elasticsearch documents changed after the readiness inspection"
            )
        indexed_versions = {
            chunk.chunk_parse_task_id: chunk.content_version
            for chunk in snapshot.chunks
        }
        self._check_active()
        return self._repository.complete_interval_indexing(
            lease, indexed_versions
        )

    def _build_document(
        self,
        lease: IntervalParseLease,
        snapshot: IntervalIndexingSnapshot,
        chunk: IndexableChunkState,
        stored: StoredDocumentState,
    ) -> VideoChunkIndexDocument:
        self._check_active()
        result = VideoParseResult.model_validate(chunk.content)
        projection = project_chunk_index(
            result,
            chunk_start_seconds=chunk.start_seconds,
            chunk_end_seconds=chunk.end_seconds,
            description_language=self.contract.description_language,
        )
        source_revision = _source_revision(result, chunk.content_version)
        semantic_target = self._semantic_target(projection)
        cached = _cached_representations(
            chunk.content_detail, content_version=chunk.content_version
        )
        source_video = self._media_resolver(chunk.source_media_ref)
        media_identity = MediaLeaseIdentity(
            snapshot.video_parse_task_id,
            snapshot.interval_parse_task_id,
            lease.lease_version,
            execution_kind="interval",
        )
        with self._media_service.prepare_video_segment(
            media_identity,
            source_video=source_video,
            start_seconds=chunk.start_seconds,
            end_seconds=chunk.end_seconds,
            max_height=self.contract.vision_max_height,
            check_active=self._check_active,
        ) as video_path:
            self._check_active()
            vision_target = self._vision_target(video_path)
            semantic_vector = (
                _reuse(cached.get("text"), semantic_target)
                if semantic_target is not None
                else None
            )
            if semantic_target is not None and semantic_vector is None:
                semantic_vector = self._reuse_stored(
                    stored,
                    snapshot=snapshot,
                    chunk=chunk,
                    target=semantic_target,
                    modality="text",
                )
            vision_vector = _reuse(cached.get("video"), vision_target)
            if vision_vector is None:
                vision_vector = self._reuse_stored(
                    stored,
                    snapshot=snapshot,
                    chunk=chunk,
                    target=vision_target,
                    modality="video",
                )
            representations: dict[str, dict[str, Any]] = {}
            if semantic_target is not None and semantic_vector is not None:
                representations["text"] = _representation(
                    semantic_target, semantic_vector
                )
            if vision_vector is not None:
                representations["video"] = _representation(
                    vision_target, vision_vector
                )
            if semantic_target is not None and semantic_vector is None:
                self._check_active()
                with parse_chunk_scope(
                    chunk.sequence_no, category="embedding"
                ):
                    response = self._embedder.embed_text(
                        projection.semantic_text
                    )
                semantic_vector = _validate_embedding(response, semantic_target)
                self._check_active()
                representations["text"] = _representation(
                    semantic_target, semantic_vector
                )
                self._save_representations(lease, chunk, representations)
            if vision_vector is None:
                self._check_active()
                with parse_chunk_scope(
                    chunk.sequence_no, category="embedding"
                ):
                    response = self._embedder.embed_video(video_path)
                vision_vector = _validate_embedding(response, vision_target)
                self._check_active()
                representations["video"] = _representation(
                    vision_target, vision_vector
                )
                self._save_representations(lease, chunk, representations)

        # Besides caching new vectors, this is the last DB lease/content check
        # before the external write. Elasticsearch external versions still
        # decide ordering if the lease changes in the following network window.
        self._save_representations(lease, chunk, representations)
        return VideoChunkIndexDocument(
            workspace_id=chunk.workspace_id,
            series_id=chunk.series_id,
            video_id=chunk.video_id,
            video_chunk_id=chunk.video_chunk_id,
            parse_task_id=snapshot.video_parse_task_id,
            content_version=chunk.content_version,
            source_revision=source_revision,
            retrieval_contract_id=self.contract.retrieval_contract_id,
            index_generation=self.contract.index_generation,
            semantic_encoder_id=self.contract.semantic_encoder_id,
            vision_encoder_id=self.contract.vision_encoder_id,
            semantic_input_sha256=(
                semantic_target.input_sha256
                if semantic_target is not None
                else None
            ),
            vision_input_sha256=vision_target.input_sha256,
            chunk_start_seconds=chunk.start_seconds,
            chunk_end_seconds=chunk.end_seconds,
            projection=projection,
            semantic_vector=semantic_vector,
            vision_vector=vision_vector,
        )

    def _reuse_stored(
        self,
        stored: StoredDocumentState,
        *,
        snapshot: IntervalIndexingSnapshot,
        chunk: IndexableChunkState,
        target: EmbeddingReuseTarget,
        modality: str,
    ) -> tuple[float, ...] | None:
        if not stored.present:
            return None
        expected_identity = (
            chunk.workspace_id,
            chunk.series_id,
            chunk.video_id,
            snapshot.video_parse_task_id,
            chunk.video_chunk_id,
        )
        stored_identity = (
            stored.workspace_id,
            stored.series_id,
            stored.video_id,
            stored.parse_task_id,
            stored.video_chunk_id,
        )
        if stored_identity != expected_identity:
            raise VideoParseIndexingError(
                "Elasticsearch document identity differs from the indexing snapshot"
            )
        if modality == "text":
            input_sha256 = stored.semantic_input_sha256
            encoder_id = stored.semantic_encoder_id
            vector = stored.semantic_vector
            expected_encoder_id = self.contract.semantic_encoder_id
        else:
            input_sha256 = stored.vision_input_sha256
            encoder_id = stored.vision_encoder_id
            vector = stored.vision_vector
            expected_encoder_id = self.contract.vision_encoder_id
        if encoder_id != expected_encoder_id:
            return None

        # The stored canonical encoder ID proves the full contract represented
        # by ``target``; the source also carries the exact input digest. Re-run
        # the common validator for digest and vector validation.
        metadata = _representation(target, vector or ())
        metadata["input_sha256"] = input_sha256
        return _reuse(metadata, target)

    def _save_representations(
        self,
        lease: IntervalParseLease,
        chunk: IndexableChunkState,
        representations: Mapping[str, Any],
    ) -> None:
        self._check_active()
        self._repository.save_index_embeddings(
            lease,
            chunk.chunk_parse_task_id,
            chunk.content_version,
            representations,
        )

    def _semantic_target(
        self, projection: ChunkIndexProjection
    ) -> EmbeddingReuseTarget | None:
        text = projection.semantic_text.strip()
        if not text:
            return None
        return EmbeddingReuseTarget(
            modality="text",
            input_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            provider=self.contract.semantic_provider,
            model=self.contract.semantic_model,
            encoder_revision=self.contract.semantic_encoder_revision,
            dimensions=self.contract.semantic_dimensions,
            normalization=_ENCODER_NORMALIZATION,
            output_contract=_ENCODER_OUTPUT_CONTRACT,
        )

    def _vision_target(self, video_path: Path) -> EmbeddingReuseTarget:
        with video_path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        return EmbeddingReuseTarget(
            modality="video",
            input_sha256=digest,
            provider=self.contract.vision_provider,
            model=self.contract.vision_model,
            encoder_revision=self.contract.vision_encoder_revision,
            dimensions=self.contract.vision_dimensions,
            normalization=_ENCODER_NORMALIZATION,
            output_contract=_ENCODER_OUTPUT_CONTRACT,
            video_fps=self.contract.vision_fps,
            mime_type=self.contract.vision_mime_type,
        )

def _encoder_contract_id(modality: str, contract: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        contract,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"{modality}:sha256:{hashlib.sha256(encoded).hexdigest()}"


def _source_revision(result: VideoParseResult, content_version: int) -> str:
    payload = {
        "content_version": content_version,
        "content": result.model_dump(mode="json"),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _cached_representations(
    detail: Mapping[str, Any], *, content_version: int
) -> Mapping[str, Any]:
    cached = detail.get(INDEX_EMBEDDINGS_DETAIL_KEY)
    if not isinstance(cached, Mapping):
        return {}
    cached_version = cached.get("content_version")
    if (
        isinstance(cached_version, bool)
        or not isinstance(cached_version, int)
        or not 1 <= cached_version <= content_version
    ):
        return {}
    representations = cached.get("representations")
    return representations if isinstance(representations, Mapping) else {}


def _reuse(
    representation: object, target: EmbeddingReuseTarget
) -> tuple[float, ...] | None:
    if not isinstance(representation, Mapping):
        return None
    decision = assess_embedding_reuse(
        target,
        source_kind="v2_index_cache",
        metadata=representation,
        vector=representation.get("vector"),  # type: ignore[arg-type]
    )
    return decision.vector


def _validate_embedding(
    result: EmbeddingResult, target: EmbeddingReuseTarget
) -> tuple[float, ...]:
    if result.modality != target.modality or result.model != target.model:
        raise VideoParseIndexingError(
            f"{target.modality} embedding response contract differs"
        )
    vector = _reuse(_representation(target, result.vector), target)
    if vector is None:
        raise VideoParseIndexingError(
            f"{target.modality} embedding vector is invalid"
        )
    return vector


def _representation(
    target: EmbeddingReuseTarget, vector: Sequence[float]
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "input_sha256": target.input_sha256,
        "modality": target.modality,
        "provider": target.provider,
        "model": target.model,
        "encoder_revision": target.encoder_revision,
        "dimensions": target.dimensions,
        "normalization": target.normalization,
        "output_contract": target.output_contract,
        "vector": list(vector),
    }
    if target.modality == "video":
        result.update(video_fps=target.video_fps, mime_type=target.mime_type)
    return result


__all__ = [
    "INDEX_EMBEDDINGS_DETAIL_KEY",
    "MEDIA_PREPROCESS_REVISION",
    "MultimodalChunkEmbedder",
    "VideoParseIndexingContract",
    "VideoParseIndexingError",
    "VideoParseIndexingRepository",
    "VideoParseIndexingService",
]
