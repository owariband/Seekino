"""I/O coordination for the Engine-owned Parse V2 index pipeline."""

from __future__ import annotations

import contextlib
import hashlib
import json
import time
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
from engine.rag.indexing.events import (
    EventIndexDocument, EventIndexingContract, EventSemanticSource, build_event_index_receipt,
    finalize_event_index, plan_event_index,
)
from engine.rag.vision.event_frames import (
    CaptureFrame, accept_event_frame_embedding, aggregate_event_frame_embeddings,
    plan_event_frame_embeddings,
)
from agent.video_parse_v2.state import ChunkTaskPhase
from gateway.embedding import EmbeddingResult, SparseEmbeddingResult

from ..model.dto.jobDTO import (
    IndexableChunkState,
    ChunkIndexingSnapshot,
    ChunkParseLease,
)
from .video_parse_persistence import INDEX_EMBEDDINGS_DETAIL_KEY
from ..storage.elasticsearch import (
    ElasticsearchIndexSchemaError,
    ElasticsearchVideoChunkStore,
    StoredDocumentState,
    document_id,
)
from ..storage.elasticsearch_events import ElasticsearchVideoEventStore
from .video_parse_media import MediaLeaseIdentity, VideoParseMediaService

# Every cache save rewrites the Chunk's whole Event cache row, so per-step saves
# grow quadratically with the Event count. Flush finished Events in batches;
# a hard crash re-encodes at most one batch.
EVENT_CACHE_FLUSH_EVENTS = 8
EVENT_CACHE_FLUSH_SECONDS = 30.0


class VideoParseIndexCoordinatorPersistence(Protocol):
    def load_chunk_indexing_snapshot(
        self, lease: ChunkParseLease, *, event_target=None, now=None,
    ) -> ChunkIndexingSnapshot: ...

    def save_index_embeddings(
        self,
        lease: ChunkParseLease,
        chunk_parse_task_id: str,
        content_version: int,
        representations: Mapping[str, Any],
        *,
        receipt: Mapping[str, Any] | None = None,
        event_target: Mapping[str, Any] | None = None,
        now=None,
    ) -> None: ...

    def complete_chunk_indexing(
        self,
        lease: ChunkParseLease,
        content_version: int,
        index_generation: int,
        *,
        event_target: Mapping[str, Any] | None = None,
        now=None,
    ) -> object: ...


class MultimodalChunkEmbedder(Protocol):
    def embed_text(self, text: str) -> EmbeddingResult: ...

    def embed_video(self, video_path: str | Path) -> EmbeddingResult: ...

    def embed_image(self, image_path: str | Path) -> EmbeddingResult: ...

    def embed_sparse(self, text: str) -> SparseEmbeddingResult: ...


class VideoParseIndexCoordinationError(RuntimeError):
    """The current Chunk could not be published at its required version."""


class VideoParseIndexCoordinator:
    """Execute one Engine index plan against media, Gateway, MySQL, and ES."""

    def __init__(
        self,
        persistence: VideoParseIndexCoordinatorPersistence,
        store: ElasticsearchVideoChunkStore,
        embedder: MultimodalChunkEmbedder,
        *,
        media_resolver: Callable[[str], Path],
        media_service: VideoParseMediaService,
        contract: ChunkIndexingContract,
        check_active: Callable[[], None] | None = None,
        event_store: ElasticsearchVideoEventStore | None = None,
        event_target: Mapping[str, Any] | None = None,
    ) -> None:
        if store.semantic_dimensions != contract.semantic_encoder.dimensions:
            raise ValueError("semantic dimensions differ between contract and store")
        if getattr(store, "vision_enabled", True) != (contract.vision_encoder is not None):
            raise ValueError("vision mode differs between contract and store")
        if contract.vision_encoder is not None and store.vision_dimensions != contract.vision_encoder.dimensions:
            raise ValueError("vision dimensions differ between contract and store")
        if isinstance(contract.visual_input_contract, OrderedFramesInputContract):
            raise ValueError(
                "the current Server adapter does not implement ordered-frame encoding"
            )
        if contract.sparse_encoder is not None and not callable(getattr(embedder, "embed_sparse", None)):
            raise ValueError("the configured sparse encoder requires a Gateway sparse adapter")
        visual_contract = contract.visual_input_contract
        if visual_contract is not None and (
            visual_contract.mime_type != "video/mp4"
            or visual_contract.preprocess_revision
            != VIDEO_CLIP_PREPROCESS_REVISION
        ):
            raise ValueError(
                "the current Server adapter requires its MP4 preprocessing contract"
            )
        self._persistence = persistence
        self._store = store
        self._embedder = embedder
        self._media_resolver = media_resolver
        self._media_service = media_service
        self._check_active = check_active or (lambda: None)
        self.contract = contract
        if (event_store is None) != (event_target is None):
            raise ValueError("Event store and frozen target must be provided together")
        self._event_store = event_store
        self._event_target = dict(event_target) if event_target is not None else None
        self._event_contract = (EventIndexingContract.model_validate(event_target["contract"])
                                if event_target is not None else None)
        if event_store is not None and event_store.index_name != event_target["index_name"]:
            raise ValueError("Event store differs from the frozen target index")

    def index_chunk(self, lease: ChunkParseLease) -> object:
        """Publish one leased Chunk and prove its exact query-visible version."""

        self._check_active()
        event_options = {"event_target": self._event_target} if self._event_target is not None else {}
        snapshot = self._persistence.load_chunk_indexing_snapshot(lease, **event_options)
        if snapshot.target_index_generation != self.contract.index_generation:
            raise VideoParseIndexCoordinationError("index contract differs from the task target generation")
        chunk = snapshot.chunk
        if (lease.phase != ChunkTaskPhase.INDEX or lease.chunk_parse_task_id != chunk.chunk_parse_task_id
                or lease.video_parse_task_id != snapshot.video_parse_task_id
                or lease.video_chunk_id != chunk.video_chunk_id):
            raise VideoParseIndexCoordinationError("index snapshot differs from the Chunk execution")
        self._check_active()
        key = (snapshot.video_parse_task_id, chunk.video_chunk_id)
        stored = self._store.inspect([key])
        if set(stored) != {key}:
            raise VideoParseIndexCoordinationError("Elasticsearch inspection omitted the expected Chunk")
        state = stored[key]
        current = self._same_stored_version(state, snapshot, chunk)
        if state.present and (state.index_generation or 0, state.content_version or 0) > (
            snapshot.target_index_generation, chunk.content_version,
        ):
            raise VideoParseIndexCoordinationError("Elasticsearch already contains a newer index version")
        self._check_active()
        if current:
            expected = {
                "tenant_id": chunk.tenant_id, "series_id": chunk.series_id,
                "video_id": chunk.video_id, "parse_task_id": snapshot.video_parse_task_id,
                "video_chunk_id": chunk.video_chunk_id, "content_version": chunk.content_version,
                "index_generation": snapshot.target_index_generation,
                "chunk_start_seconds": chunk.start_seconds, "chunk_end_seconds": chunk.end_seconds,
            }
            semantic_required = any(
                event["start_seconds"] < chunk.end_seconds and event["end_seconds"] > chunk.start_seconds
                for event in chunk.content["timeline"]
            )
            if not self._store.is_current(
                expected, semantic_required=semantic_required,
                sparse_required=self.contract.sparse_encoder is not None,
            ):
                raise VideoParseIndexCoordinationError("current Elasticsearch version is not query-visible or complete")
            receipt = ChunkIndexReceipt(
                parse_task_id=snapshot.video_parse_task_id,
                video_chunk_id=chunk.video_chunk_id,
                content_version=chunk.content_version,
                index_generation=snapshot.target_index_generation,
            ).as_dict()
            if (not _cached_representations(chunk.content_detail, receipt=receipt)
                    or self.contract.sparse_encoder is not None):
                representations = {}
                if self.contract.sparse_encoder is not None:
                    representations["sparse"] = {
                        "channel": "sparse", "weights": dict(state.sparse_token_weights or ()),
                    }
                self._save_representations(lease, chunk, representations, receipt=receipt)
            self._check_active()
            if not self._store.is_current(
                expected, semantic_required=semantic_required,
                sparse_required=self.contract.sparse_encoder is not None,
            ):
                raise VideoParseIndexCoordinationError("Elasticsearch current version changed before completion")
        else:
            document = self._build_document(lease, snapshot, chunk, state)
            if not self._store.is_ready([document]):
                self._check_active()
                try:
                    self._store.bulk_upsert([document], refresh="wait_for")
                except (ValueError, ElasticsearchIndexSchemaError):
                    raise
                except Exception:
                    self._check_active()
                    exact = self._store.inspect_exact([document])
                    expected_id = document_id(document.parse_task_id, document.video_chunk_id)
                    if exact != {expected_id: True}:
                        raise
            self._check_active()
            if not self._store.verify_ready([document]):
                raise VideoParseIndexCoordinationError("Elasticsearch document is not query-visible at the expected version")
        if self._event_store is not None:
            self._index_events(lease, snapshot)
        self._check_active()
        return self._persistence.complete_chunk_indexing(
            lease, chunk.content_version, snapshot.target_index_generation, **event_options,
        )

    def _index_events(self, lease: ChunkParseLease, snapshot: ChunkIndexingSnapshot) -> None:
        """Restore per-Event work, then prove the complete searchable source set."""
        store, contract = self._event_store, self._event_contract
        assert store is not None and contract is not None and self._event_target is not None
        store.ensure_index(contract)
        chunk = snapshot.chunk
        plans = plan_event_index(
            snapshot.video_parse_task_id, chunk.video_chunk_id, chunk.content_version,
            VideoParseResult.model_validate(chunk.content), chunk.start_seconds, chunk.end_seconds, contract,
        )
        base_receipt = ChunkIndexReceipt(
            parse_task_id=snapshot.video_parse_task_id, video_chunk_id=chunk.video_chunk_id,
            content_version=chunk.content_version, index_generation=snapshot.target_index_generation,
        ).as_dict()
        cached = _cached_representations(chunk.content_detail, receipt=base_receipt).get("events", {})
        cached = cached if isinstance(cached, Mapping) else {}
        documents = []
        image_vectors = {}
        identity = MediaLeaseIdentity(snapshot.video_parse_task_id, chunk.chunk_parse_task_id, lease.lease_version)
        # Partial Events are kept only in memory and flushed with finished ones
        # or on failure, so a retry still reuses every vector already encoded.
        pending: dict[str, dict[str, Any]] = {}

        def flush() -> float:
            if pending:
                self._save_representations(lease, chunk, {"events": dict(pending)}, receipt=base_receipt)
                pending.clear()
            return time.monotonic()

        flushed_at = time.monotonic()
        try:
            for plan in plans:
                self._check_active()
                semantic_inputs = {
                    _event_semantic_key(item.source): hashlib.sha256(
                        item.request.semantic_text.encode("utf-8")
                    ).hexdigest()
                    for item in plan.semantic_requests
                }
                proof = {"contract_id": contract.contract_id, "source_media_ref": chunk.source_media_ref}
                previous = cached.get(plan.event_id)
                entry = dict(previous) if isinstance(previous, Mapping) and all(
                    previous.get(key) == value for key, value in proof.items()
                ) else dict(proof)
                # A completed cache binds the source, exact text, realized frames and encoder contract.
                restored = None
                semantic_cache = entry.get("semantic_artifacts", {})
                semantic_cache = dict(semantic_cache) if isinstance(semantic_cache, Mapping) else {}
                if isinstance(entry.get("document"), Mapping):
                    try:
                        saved = EventIndexDocument.model_validate(entry["document"])
                        saved_inputs = entry.get("semantic_inputs", {})
                        if isinstance(saved_inputs, Mapping):
                            for representation in saved.semantic_vectors:
                                key = _event_semantic_key(representation)
                                if key in saved_inputs:
                                    semantic_cache.setdefault(key, {"input_sha256": saved_inputs[key],
                                                                    "vector": representation.vector})
                        if saved_inputs == semantic_inputs:
                            semantic = [
                                accept_embedding_result(item.request, modality="text",
                                    model=contract.semantic_encoder.model, vector=representation.vector)
                                for item, representation in zip(
                                    plan.semantic_requests, saved.semantic_vectors, strict=True,
                                )
                            ]
                            rebuilt = finalize_event_index(plan, semantic, saved.vision_vector, saved.detail.capture.frames)
                            if rebuilt == saved:
                                restored = rebuilt
                    except (TypeError, ValueError):
                        pass
                if restored is not None:
                    documents.append(restored)
                    continue
                semantic = []
                for item in plan.semantic_requests:
                    self._check_active()
                    key = _event_semantic_key(item.source)
                    artifact, cached_text = None, semantic_cache.get(key)
                    if isinstance(cached_text, Mapping) and cached_text.get("input_sha256") == semantic_inputs[key]:
                        try:
                            artifact = accept_embedding_result(item.request, modality="text",
                                model=contract.semantic_encoder.model, vector=cached_text.get("vector"))
                        except (TypeError, ValueError):
                            pass
                    if artifact is None:
                        with parse_chunk_scope(chunk.sequence_no, category="embedding"):
                            result = self._embedder.embed_text(item.request.semantic_text)
                        artifact = _accept_result(item.request, result)
                        semantic_cache[key] = {"input_sha256": semantic_inputs[key], "vector": list(artifact.vector)}
                        entry["semantic_artifacts"] = semantic_cache
                        pending[plan.event_id] = entry
                    semantic.append(artifact)
                vision, frames = None, ()
                if contract.vision_encoder is not None:
                    source = self._media_resolver(chunk.source_media_ref)
                    with self._media_service.prepare_event_frames(
                        identity, source_video=source, timestamps_ms=plan.frame_timestamps_ms,
                        max_height=contract.capture.max_height, check_active=self._check_active,
                    ) as paths:
                        frames = tuple(CaptureFrame(requested_at_ms=timestamp, image_sha256=_file_sha256(path))
                                       for timestamp, path in paths)
                        requests = plan_event_frame_embeddings(contract.vision_encoder, frames, contract.capture)
                        frame_cache = entry.get("frame_artifacts", {})
                        frame_cache = dict(frame_cache) if isinstance(frame_cache, Mapping) else {}
                        artifacts = []
                        for request, (_, path) in zip(requests, paths, strict=True):
                            self._check_active()
                            key = request.encoder_id + ":" + request.input_sha256
                            vector = image_vectors.get(key, frame_cache.get(key))
                            artifact = None
                            if vector is not None:
                                try:
                                    artifact = accept_event_frame_embedding(request, modality="image",
                                        model=request.encoder.model, vector=vector)
                                except (TypeError, ValueError):
                                    pass
                            if artifact is None:
                                with parse_chunk_scope(chunk.sequence_no, category="embedding"):
                                    result = self._embedder.embed_image(path)
                                artifact = accept_event_frame_embedding(request, modality=result.modality,
                                    model=result.model, vector=result.vector)
                                frame_cache[key] = list(artifact.vector)
                                entry["frame_artifacts"] = frame_cache
                                pending[plan.event_id] = entry
                            image_vectors[key] = artifact.vector
                            artifacts.append(artifact)
                        vision = aggregate_event_frame_embeddings(requests, artifacts, contract.capture)
                document = finalize_event_index(plan, semantic, vision, frames)
                entry = {**proof, "semantic_inputs": semantic_inputs, "document": document.as_source()}
                pending[plan.event_id] = entry
                if (len(pending) >= EVENT_CACHE_FLUSH_EVENTS
                        or time.monotonic() - flushed_at >= EVENT_CACHE_FLUSH_SECONDS):
                    flushed_at = flush()
                documents.append(document)
        except Exception:
            # Best effort: the original failure (often a lost lease) wins.
            with contextlib.suppress(Exception):
                flush()
            raise
        flush()
        self._check_active()
        if not store.verify_ready(documents):
            try:
                store.bulk_upsert(documents, write_version=lease.lease_version, refresh="wait_for")
            except (ValueError, ElasticsearchIndexSchemaError):
                raise
            except Exception:
                self._check_active()
                if not store.verify_ready(documents):
                    raise
        if not store.verify_ready(documents):
            raise VideoParseIndexCoordinationError("Event documents are not query-visible at the expected source")
        self._check_active()
        store.delete_older(snapshot.video_parse_task_id, chunk.video_chunk_id, chunk.content_version)
        receipt = build_event_index_receipt(store.index_name, contract, plans)
        if not store.verify_set(snapshot.video_parse_task_id, chunk.video_chunk_id,
                                chunk.content_version, receipt.event_ids):
            raise VideoParseIndexCoordinationError("Event publication differs from the complete expected set")
        self._save_representations(lease, chunk, {}, receipt={**base_receipt, "events": receipt.as_dict()})

    def _build_document(
        self,
        lease: ChunkParseLease,
        snapshot: ChunkIndexingSnapshot,
        chunk: IndexableChunkState,
        stored: StoredDocumentState,
    ) -> VideoChunkIndexDocument:
        self._check_active()
        result = VideoParseResult.model_validate(chunk.content)
        visual_contract = self.contract.visual_input_contract
        if visual_contract is not None and not isinstance(visual_contract, VideoClipInputContract):
            raise VideoParseIndexCoordinationError(
                "the current Server adapter requires a video-clip input contract"
            )

        media_identity = MediaLeaseIdentity(
            snapshot.video_parse_task_id,
            chunk.chunk_parse_task_id,
            lease.lease_version,
            execution_kind="chunk",
        )
        media_context = contextlib.nullcontext(None)
        if visual_contract is not None:
            media_context = self._media_service.prepare_video_segment(
                media_identity,
                source_video=self._media_resolver(chunk.source_media_ref),
                start_seconds=chunk.start_seconds,
                end_seconds=chunk.end_seconds,
                max_height=visual_contract.max_height,
                check_active=self._check_active,
            )
        with media_context as video_path:
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
                    ) if visual_contract is not None else None,
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

            if plan.vision_request is not None and "vision" not in artifacts:
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

        # This final persistence call is also the last lease/content check before
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
        snapshot: ChunkIndexingSnapshot,
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
        self, stored: StoredDocumentState, snapshot: ChunkIndexingSnapshot,
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
        lease: ChunkParseLease,
        chunk: IndexableChunkState,
        representations: Mapping[str, Any],
        *,
        receipt: Mapping[str, Any],
    ) -> None:
        self._check_active()
        self._persistence.save_index_embeddings(
            lease,
            chunk.chunk_parse_task_id,
            chunk.content_version,
            representations,
            receipt=receipt,
            **({"event_target": self._event_target} if self._event_target is not None else {}),
        )


def _event_semantic_key(source: EventSemanticSource) -> str:
    return json.dumps((source.source, source.language.casefold() if source.language else None, source.role))


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
    "VideoParseIndexCoordinatorPersistence",
]
