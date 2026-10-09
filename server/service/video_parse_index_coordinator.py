"""I/O coordination for the Engine-owned Parse V2 index pipeline."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Protocol

from engine.common.usage import parse_chunk_scope
from engine.parsev2.schemas import VideoParseResult
from engine.rag import (
    ChunkIndexSource,
    ChunkIndexReceipt,
    ChunkIndexingContract,
    EmbeddingArtifact,
    EmbeddingRequest,
    OrderedFramesInputContract,
    SparseEmbeddingArtifact,
    SparseEmbeddingRequest,
    VIDEO_CLIP_PREPROCESS_REVISION,
    VideoChunkIndexDocument,
    VideoClipInputContract,
    VideoClipManifest,
    accept_embedding_result,
    accept_sparse_result,
    finalize_chunk_index,
    plan_chunk_index,
)
from gateway.embedding import EmbeddingResult, SparseEmbeddingResult

from ..model.dto.jobDTO import (
    IndexableChunkState,
    IntervalIndexingSnapshot,
    IntervalParseLease,
)
from ..repository.video_parse import INDEX_EMBEDDINGS_DETAIL_KEY
from ..storage.elasticsearch import (
    ElasticsearchIndexSchemaError,
    ElasticsearchVideoChunkStore,
    StoredDocumentState,
    document_id,
)
from .video_parse_media import MediaLeaseIdentity, VideoParseMediaService


class VideoParseIndexCoordinatorRepository(Protocol):
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
        receipt: Mapping[str, Any] | None = None,
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

    def embed_sparse(self, text: str) -> SparseEmbeddingResult: ...


class VideoParseIndexCoordinationError(RuntimeError):
    """The current Interval could not make every released Chunk query-visible."""


class VideoParseIndexCoordinator:
    """Execute one Engine index plan against media, Gateway, MySQL, and ES."""

    def __init__(
        self,
        repository: VideoParseIndexCoordinatorRepository,
        store: ElasticsearchVideoChunkStore,
        embedder: MultimodalChunkEmbedder,
        *,
        media_resolver: Callable[[str], Path],
        media_service: VideoParseMediaService,
        contract: ChunkIndexingContract,
        check_active: Callable[[], None] | None = None,
    ) -> None:
        if store.semantic_dimensions != contract.semantic_encoder.dimensions:
            raise ValueError("semantic dimensions differ between contract and store")
        if store.vision_dimensions != contract.vision_encoder.dimensions:
            raise ValueError("vision dimensions differ between contract and store")
        if isinstance(contract.visual_input_contract, OrderedFramesInputContract):
            raise ValueError(
                "the current Server adapter does not implement ordered-frame encoding"
            )
        if contract.sparse_encoder is not None and not callable(getattr(embedder, "embed_sparse", None)):
            raise ValueError("the configured sparse encoder requires a Gateway sparse adapter")
        visual_contract = contract.visual_input_contract
        if (
            visual_contract.mime_type != "video/mp4"
            or visual_contract.preprocess_revision
            != VIDEO_CLIP_PREPROCESS_REVISION
        ):
            raise ValueError(
                "the current Server adapter requires its MP4 preprocessing contract"
            )
        self._repository = repository
        self._store = store
        self._embedder = embedder
        self._media_resolver = media_resolver
        self._media_service = media_service
        self._check_active = check_active or (lambda: None)
        self.contract = contract

    def index_interval(self, lease: IntervalParseLease) -> object:
        """Index released members, prove visibility, then complete the Interval."""

        self._check_active()
        snapshot = self._repository.load_interval_indexing_snapshot(lease)
        self._check_active()
        keys = [
            (snapshot.video_parse_task_id, chunk.video_chunk_id)
            for chunk in snapshot.chunks
        ]
        stored = self._store.inspect(keys)
        if set(stored) != set(keys):
            raise VideoParseIndexCoordinationError(
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
                raise
            except Exception:
                # A lost bulk response is ambiguous. Exact real-time readback
                # distinguishes a completed replay from a conflicting source.
                self._check_active()
                exact = self._store.inspect_exact(pending)
                expected_ids = {
                    document_id(item.parse_task_id, item.video_chunk_id)
                    for item in pending
                }
                if set(exact) != expected_ids or not all(exact.values()):
                    raise
            self._check_active()
            if not self._store.verify_ready(documents):
                raise VideoParseIndexCoordinationError(
                    "Elasticsearch documents are durable but not query-visible"
                )
        elif documents and not self._store.verify_ready(documents):
            raise VideoParseIndexCoordinationError(
                "Elasticsearch documents changed after the readiness inspection"
            )

        indexed_versions = {
            chunk.chunk_parse_task_id: chunk.content_version
            for chunk in snapshot.chunks
        }
        self._check_active()
        return self._repository.complete_interval_indexing(lease, indexed_versions)

    def _build_document(
        self,
        lease: IntervalParseLease,
        snapshot: IntervalIndexingSnapshot,
        chunk: IndexableChunkState,
        stored: StoredDocumentState,
    ) -> VideoChunkIndexDocument:
        self._check_active()
        result = VideoParseResult.model_validate(chunk.content)
        visual_contract = self.contract.visual_input_contract
        if not isinstance(visual_contract, VideoClipInputContract):
            raise VideoParseIndexCoordinationError(
                "the current Server adapter requires a video-clip input contract"
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
            max_height=visual_contract.max_height,
            check_active=self._check_active,
        ) as video_path:
            self._check_active()
            plan = plan_chunk_index(
                ChunkIndexSource(
                    tenant_id=chunk.tenant_id,
                    series_id=chunk.series_id,
                    video_id=chunk.video_id,
                    video_chunk_id=chunk.video_chunk_id,
                    parse_task_id=snapshot.video_parse_task_id,
                    content_version=chunk.content_version,
                    chunk_start_seconds=chunk.start_seconds,
                    chunk_end_seconds=chunk.end_seconds,
                    result=result,
                    visual_input=VideoClipManifest(
                        input_sha256=_file_sha256(video_path),
                        start_seconds=chunk.start_seconds,
                        end_seconds=chunk.end_seconds,
                        fps=visual_contract.fps,
                        max_height=visual_contract.max_height,
                        mime_type=visual_contract.mime_type,
                        preprocess_revision=visual_contract.preprocess_revision,
                    ),
                ),
                self.contract,
            )
            receipt = ChunkIndexReceipt(
                parse_task_id=plan.parse_task_id, video_chunk_id=plan.video_chunk_id,
                content_version=plan.content_version, index_generation=plan.index_generation,
            ).as_dict()
            cached = _cached_representations(chunk.content_detail, receipt=receipt)
            artifacts: dict[str, EmbeddingArtifact] = {}
            representations: dict[str, dict[str, object]] = {}

            for request in plan.encoding_requests:
                receipt_key = _receipt_key(request)
                artifact = _restore_cached(request, cached.get(receipt_key))
                if artifact is None:
                    artifact = self._restore_stored(
                        request,
                        stored,
                        snapshot=snapshot,
                        chunk=chunk,
                    )
                if artifact is not None:
                    artifacts[request.channel] = artifact
                    representations[receipt_key] = _representation(artifact)

            if plan.semantic_request is not None and "semantic" not in artifacts:
                request = plan.semantic_request
                self._check_active()
                with parse_chunk_scope(chunk.sequence_no, category="embedding"):
                    response = self._embedder.embed_text(request.semantic_text or "")
                artifact = _accept_result(request, response)
                self._check_active()
                artifacts[request.channel] = artifact
                representations[_receipt_key(request)] = _representation(artifact)
                self._save_representations(lease, chunk, representations, receipt=receipt)

            if "vision" not in artifacts:
                request = plan.vision_request
                self._check_active()
                with parse_chunk_scope(chunk.sequence_no, category="embedding"):
                    response = self._embedder.embed_video(video_path)
                artifact = _accept_result(request, response)
                self._check_active()
                artifacts[request.channel] = artifact
                representations[_receipt_key(request)] = _representation(artifact)
                self._save_representations(lease, chunk, representations, receipt=receipt)

            sparse_artifact = None
            if plan.sparse_request is not None:
                request = plan.sparse_request
                sparse_artifact = _restore_sparse(request, cached.get("sparse"))
                if (sparse_artifact is None and stored.sparse_token_weights is not None
                        and self._same_stored_version(stored, snapshot, chunk)):
                    sparse_artifact = _restore_sparse(request, {
                        "channel": "sparse", "weights": dict(stored.sparse_token_weights),
                    })
                if sparse_artifact is None:
                    self._check_active()
                    with parse_chunk_scope(chunk.sequence_no, category="embedding"):
                        response = self._embedder.embed_sparse(request.text)
                    try:
                        sparse_artifact = accept_sparse_result(
                            request, model=response.model, weights=response.weights,
                        )
                    except ValueError as exc:
                        raise VideoParseIndexCoordinationError(str(exc)) from exc
                    self._check_active()
                representations["sparse"] = {
                    "channel": "sparse", "weights": dict(sparse_artifact.token_weights),
                }
                self._save_representations(lease, chunk, representations, receipt=receipt)

        # This final repository call is also the last lease/content check before
        # the external write. ES external versions still order a following race.
        document = finalize_chunk_index(plan, tuple(artifacts.values()), sparse_artifact=sparse_artifact)
        self._save_representations(
            lease,
            chunk,
            representations,
            receipt=document.receipt.as_dict(),
        )
        return document

    def _restore_stored(
        self,
        request: EmbeddingRequest,
        stored: StoredDocumentState,
        *,
        snapshot: IntervalIndexingSnapshot,
        chunk: IndexableChunkState,
    ) -> EmbeddingArtifact | None:
        if not self._same_stored_version(stored, snapshot, chunk):
            return None
        vector = stored.semantic_vector if request.channel == "semantic" else stored.vision_vector
        try:
            return accept_embedding_result(
                request, modality="text" if request.channel == "semantic" else "video",
                model=request.encoder.model, vector=vector,
            )
        except ValueError:
            return None

    def _same_stored_version(
        self, stored: StoredDocumentState, snapshot: IntervalIndexingSnapshot,
        chunk: IndexableChunkState,
    ) -> bool:
        if not stored.present:
            return False
        expected_identity = (
            chunk.tenant_id,
            chunk.series_id,
            chunk.video_id,
            snapshot.video_parse_task_id,
            chunk.video_chunk_id,
        )
        stored_identity = (
            stored.tenant_id,
            stored.series_id,
            stored.video_id,
            stored.parse_task_id,
            stored.video_chunk_id,
        )
        if stored_identity != expected_identity:
            raise VideoParseIndexCoordinationError(
                "Elasticsearch document identity differs from the indexing snapshot"
            )

        return (stored.content_version == chunk.content_version
                and stored.index_generation == self.contract.index_generation)

    def _save_representations(
        self,
        lease: IntervalParseLease,
        chunk: IndexableChunkState,
        representations: Mapping[str, Any],
        *,
        receipt: Mapping[str, Any],
    ) -> None:
        self._check_active()
        self._repository.save_index_embeddings(
            lease,
            chunk.chunk_parse_task_id,
            chunk.content_version,
            representations,
            receipt=receipt,
        )


def _file_sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _cached_representations(
    detail: Mapping[str, Any], *, receipt: Mapping[str, object],
) -> Mapping[str, Any]:
    cached = detail.get(INDEX_EMBEDDINGS_DETAIL_KEY)
    if not isinstance(cached, Mapping):
        return {}
    proof = cached.get("receipt")
    if (type(cached.get("content_version")) is not int
            or cached["content_version"] != receipt["content_version"]
            or not isinstance(proof, Mapping)
            or any(type(proof.get(name)) is not type(value) or proof.get(name) != value
                   for name, value in receipt.items())):
        return {}
    representations = cached.get("representations")
    return representations if isinstance(representations, Mapping) else {}


def _restore_cached(
    request: EmbeddingRequest,
    representation: object,
) -> EmbeddingArtifact | None:
    if not isinstance(representation, Mapping) or representation.get("channel") != request.channel:
        return None
    try:
        return accept_embedding_result(
            request, modality="text" if request.channel == "semantic" else "video",
            model=request.encoder.model, vector=representation.get("vector"),
        )
    except ValueError:
        return None


def _restore_sparse(request: SparseEmbeddingRequest, representation: object) -> SparseEmbeddingArtifact | None:
    if not isinstance(representation, Mapping) or representation.get("channel") != "sparse":
        return None
    try:
        return accept_sparse_result(request, model=request.encoder.model, weights=representation.get("weights"))
    except ValueError:
        return None


def _representation(artifact: EmbeddingArtifact) -> dict[str, object]:
    return {"channel": artifact.channel, "vector": list(artifact.vector)}


def _accept_result(
    request: EmbeddingRequest,
    result: EmbeddingResult,
) -> EmbeddingArtifact:
    try:
        return accept_embedding_result(
            request,
            modality=result.modality,
            model=result.model,
            vector=result.vector,
        )
    except ValueError as exc:
        raise VideoParseIndexCoordinationError(str(exc)) from exc


def _receipt_key(request: EmbeddingRequest) -> str:
    return "text" if request.channel == "semantic" else "video"


__all__ = [
    "MultimodalChunkEmbedder",
    "VideoParseIndexCoordinator",
    "VideoParseIndexCoordinationError",
    "VideoParseIndexCoordinatorRepository",
]
