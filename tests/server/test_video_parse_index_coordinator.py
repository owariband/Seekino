from __future__ import annotations

import json
import math
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy.dialects import sqlite

from engine.common.usage import capture_parse_usage
from engine.rag import (
    ChunkIndexingContract,
    EncoderContract,
    OrderedFramesInputContract,
    SparseEncoderContract,
    VideoClipInputContract,
    VIDEO_CLIP_PREPROCESS_REVISION,
)
from gateway.embedding import EmbeddingResult, SparseEmbeddingResult
from gateway.usage import track_model_call
from server.model.dto.jobDTO import (
    IndexableChunkState,
    ChunkIndexingSnapshot,
    ChunkParseLease,
)
from server.model.job import ChunkParseContent
from server.service.video_parse_persistence import INDEX_EMBEDDINGS_DETAIL_KEY
from server.service.video_parse_index_coordinator import (
    VideoParseIndexCoordinator,
    VideoParseIndexCoordinationError,
)
from server.service.video_parse_media import VideoParseMediaService
from server.storage.elasticsearch import (
    ElasticsearchIndexSchemaError,
    StoredDocumentState,
    _external_version,
    document_id,
)
from server.utils.time import now_utc


def _content():
    return {
        "persons": ["千早爱音"],
        "timeline": [{
            "event_type": "DIALOGUE",
            "start_seconds": 1,
            "end_seconds": 3,
            "description": "千早爱音回答",
            "participants": ["千早爱音"],
            "visible_persons": ["千早爱音"],
            "speaker": "千早爱音",
            "text_variants": [{
                "language": "zh-Hans",
                "role": "ORIGINAL",
                "text": "我没事",
            }],
        }],
    }


def _lease(video_task_id, chunk_id, chunk_task_id):
    return ChunkParseLease(
        chunk_parse_task_id=chunk_task_id,
        video_chunk_id=chunk_id,
        video_parse_task_id=video_task_id,
        phase=3,
        worker_id="worker-1",
        lease_version=1,
        lease_expire_time=now_utc() + timedelta(minutes=5),
        retry_count=0,
        max_retry_count=2,
        retry_initial_delay_seconds=5.0, retry_max_delay_seconds=300.0,
    )


class _Repository:
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.saved = []
        self.completed = []

    def load_chunk_indexing_snapshot(self, lease, *, now=None):
        return self.snapshot

    def save_index_embeddings(
        self,
        lease,
        chunk_parse_task_id,
        content_version,
        representations,
        *,
        receipt=None,
        now=None,
    ):
        self.saved.append(
            (chunk_parse_task_id, content_version, representations, receipt)
        )
        chunk = self.snapshot.chunk
        assert chunk.chunk_parse_task_id == chunk_parse_task_id
        envelope = {
            "content_version": content_version,
            "representations": representations,
        }
        if receipt is not None:
            envelope["receipt"] = receipt
        chunk.content_detail[INDEX_EMBEDDINGS_DETAIL_KEY] = envelope

    def complete_chunk_indexing(self, lease, content_version, index_generation, *, now=None):
        self.completed.append({lease.chunk_parse_task_id: content_version})
        assert index_generation == self.snapshot.target_index_generation
        return "complete"


class _Store:
    vision_enabled = True
    semantic_dimensions = 2
    vision_dimensions = 2

    def __init__(self):
        self.ready = set()
        self.bulk_calls = []
        self.states = {}

    def inspect(self, keys):
        return {
            key: self.states.get(key, StoredDocumentState(present=False))
            for key in keys
        }

    def is_ready(self, documents):
        return all(self._ready_key(item) in self.ready for item in documents)

    def is_current(self, expected, *, semantic_required, sparse_required=False):
        key = (
            document_id(expected["parse_task_id"], expected["video_chunk_id"]),
            _external_version(expected["content_version"], expected["index_generation"]),
            expected["tenant_id"], expected["series_id"], expected["video_id"],
            expected["chunk_start_seconds"], expected["chunk_end_seconds"],
        )
        state = self.states[(expected["parse_task_id"], expected["video_chunk_id"])]
        return (key in self.ready and (state.vision_vector_present or not self.vision_enabled)
                and (not semantic_required or state.semantic_vector_present)
                and (not sparse_required or state.sparse_token_weights is not None))

    def verify_ready(self, documents):
        return all(self._ready_key(item) in self.ready for item in documents)

    def inspect_exact(self, documents):
        return {
            document_id(item.parse_task_id, item.video_chunk_id): (
                self._ready_key(item) in self.ready
            )
            for item in documents
        }

    def bulk_upsert(self, documents, *, refresh=False):
        self.bulk_calls.append((tuple(documents), refresh))
        self.ready.update(self._ready_key(item) for item in documents)
        for item in documents:
            self.states[(item.parse_task_id, item.video_chunk_id)] = StoredDocumentState(
                present=True,
                tenant_id=item.tenant_id,
                series_id=item.series_id,
                video_id=item.video_id,
                parse_task_id=item.parse_task_id,
                video_chunk_id=item.video_chunk_id,
                content_version=item.content_version,
                index_generation=item.index_generation,
                semantic_vector=tuple(item.semantic_vector) if item.semantic_vector else None,
                vision_vector=tuple(item.vision_vector) if item.vision_vector is not None else None,
                semantic_vector_present=item.semantic_vector is not None,
                vision_vector_present=item.vision_vector is not None,
                sparse_token_weights=item.sparse_token_weights,
            )
        return [
            document_id(item.parse_task_id, item.video_chunk_id)
            for item in documents
        ]

    @staticmethod
    def _ready_key(item):
        return (
            document_id(item.parse_task_id, item.video_chunk_id),
            _external_version(item.content_version, item.index_generation),
            item.tenant_id, item.series_id, item.video_id,
            item.chunk_start_seconds, item.chunk_end_seconds,
        )


class _Embedder:
    def __init__(self):
        self.text_calls = 0
        self.video_calls = 0

    def embed_text(self, text):
        self.text_calls += 1
        with track_model_call(
            provider="ark", model="embed-v1", operation="text_embedding"
        ):
            return EmbeddingResult(
                model="embed-v1", modality="text", vector=[1, 0]
            )

    def embed_video(self, video_path):
        self.video_calls += 1
        with track_model_call(
            provider="ark", model="embed-v1", operation="video_embedding"
        ):
            return EmbeddingResult(
                model="embed-v1", modality="video", vector=[0, 1]
            )


def _contract(*, index_generation=1, visual_input_contract=None):
    encoder = EncoderContract(
        provider="ark",
        model="embed-v1",
        encoder_revision="gateway-ark-embedding-v1",
        dimensions=2,
    )
    return ChunkIndexingContract(
        retrieval_contract_id="parse-v2-retrieval-v1",
        semantic_encoder=encoder,
        vision_encoder=encoder,
        visual_input_contract=visual_input_contract or VideoClipInputContract(
            fps=1,
            max_height=720,
            mime_type="video/mp4",
            preprocess_revision=VIDEO_CLIP_PREPROCESS_REVISION,
        ),
        index_generation=index_generation,
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("mime_type", "video/webm"),
        ("preprocess_revision", "another-preprocess-v2"),
    ],
)
def test_coordinator_rejects_clip_contract_the_media_adapter_cannot_produce(
    tmp_path, field, value,
):
    ids, media, snapshot = _fixture(tmp_path)
    clip = replace(_contract().visual_input_contract, **{field: value})

    with pytest.raises(ValueError, match="MP4 preprocessing contract"):
        VideoParseIndexCoordinator(
            _Repository(snapshot),
            _Store(),
            _Embedder(),
            media_resolver=lambda _uri: media,
            media_service=_media_service(tmp_path),
            contract=_contract(visual_input_contract=clip),
        )


def test_coordinator_rejects_ordered_frames_until_an_adapter_implements_them(
    tmp_path,
):
    _, media, snapshot = _fixture(tmp_path)
    frames = OrderedFramesInputContract(
        sampling_contract_id="uniform-four-v1",
        mime_type="image/jpeg",
        preprocess_revision="jpeg-resize-v1",
    )

    with pytest.raises(ValueError, match="does not implement ordered-frame"):
        VideoParseIndexCoordinator(
            _Repository(snapshot),
            _Store(),
            _Embedder(),
            media_resolver=lambda _uri: media,
            media_service=_media_service(tmp_path),
            contract=_contract(visual_input_contract=frames),
        )


def _media_service(tmp_path):
    def run(command):
        Path(command[-1]).write_bytes(b"deterministic-video-segment")

    return VideoParseMediaService(tmp_path / "media-work", command_runner=run)


def _fixture(tmp_path):
    ids = {name: str(uuid4()) for name in (
        "workspace", "series", "video", "chunk_task", "chunk", "video_task", "interval"
    )}
    media = tmp_path / "chunk.mp4"
    media.write_bytes(b"video-bytes")
    chunk = IndexableChunkState(
        tenant_id=ids["workspace"],
        series_id=ids["series"],
        video_id=ids["video"],
        chunk_parse_task_id=ids["chunk_task"],
        video_chunk_id=ids["chunk"],
        sequence_no=0,
        start_seconds=0,
        end_seconds=10,
        content=_content(),
        content_version=2,
        content_detail={},
        source_media_ref="artifact://test/source.mp4",
        chunk_media_ref="artifact://test/chunk.mp4",
        analysis_window_media_ref="artifact://test/window.mp4",
    )
    snapshot = ChunkIndexingSnapshot(
        video_parse_task_id=ids["video_task"],
        chunk=chunk,
        target_index_generation=1,
    )
    return ids, media, snapshot


def test_indexing_projects_encodes_caches_writes_and_checks_visibility(tmp_path):
    ids, media, snapshot = _fixture(tmp_path)
    repository = _Repository(snapshot)
    store = _Store()
    embedder = _Embedder()
    service = VideoParseIndexCoordinator(
        repository, store, embedder,
        media_resolver=lambda _uri: media,
        media_service=_media_service(tmp_path),
        contract=_contract(),
    )
    lease = _lease(ids["video_task"], ids["chunk"], ids["chunk_task"])

    model_calls = []
    with capture_parse_usage(model_calls.append):
        assert service.index_chunk(lease) == "complete"
    assert embedder.text_calls == embedder.video_calls == 1
    assert [
        (call.sequence_no, call.category, call.call.operation)
        for call in model_calls
    ] == [
        (0, "embedding", "text_embedding"),
        (0, "embedding", "video_embedding"),
    ]
    assert repository.completed == [{ids["chunk_task"]: 2}]
    document = store.bulk_calls[0][0][0]
    assert document_id(document.parse_task_id, document.video_chunk_id) == (
        f'{ids["video_task"]}:{ids["chunk"]}'
    )
    assert document.projection.semantic_text
    assert store.bulk_calls[0][1] == "wait_for"
    representations = repository.saved[0][2]
    assert representations["text"]["vector"] == [1.0, 0.0]
    final_representations = repository.saved[-1][2]
    assert final_representations["video"]["vector"] == [0.0, 1.0]
    receipt = repository.saved[-1][3]
    assert receipt == document.receipt.as_dict()
    assert snapshot.chunk.content_detail[
        INDEX_EMBEDDINGS_DETAIL_KEY
    ]["receipt"] == receipt

    # Losing the DB cache still reuses the exactly proven ES representations.
    snapshot.chunk.content_detail.clear()
    with capture_parse_usage(model_calls.append):
        assert service.index_chunk(lease) == "complete"
    assert embedder.text_calls == embedder.video_calls == 1
    assert len(model_calls) == 2
    assert len(store.bulk_calls) == 1


def test_indexing_writes_the_configured_generation(tmp_path):
    ids, media, snapshot = _fixture(tmp_path)
    snapshot = replace(snapshot, target_index_generation=7)
    repository = _Repository(snapshot)
    store = _Store()
    service = VideoParseIndexCoordinator(
        repository, store, _Embedder(),
        media_resolver=lambda _uri: media,
        media_service=_media_service(tmp_path),
        contract=_contract(index_generation=7),
    )

    service.index_chunk(
        _lease(ids["video_task"], ids["chunk"], ids["chunk_task"])
    )

    document = store.bulk_calls[0][0][0]
    assert document.index_generation == 7


@pytest.mark.parametrize("vector", [
    [math.nextafter(1.0, math.inf), 0.0], [0.25, 0.75],
])
def test_visible_current_version_ignores_cached_values_and_skips_media(tmp_path, vector):
    ids, media, snapshot = _fixture(tmp_path)
    repository, store, embedder = _Repository(snapshot), _Store(), _Embedder()
    service = VideoParseIndexCoordinator(
        repository, store, embedder, media_resolver=lambda _: media,
        media_service=_media_service(tmp_path), contract=_contract(),
    )
    lease = _lease(ids["video_task"], ids["chunk"], ids["chunk_task"])
    service.index_chunk(lease)
    cached = snapshot.chunk.content_detail[INDEX_EMBEDDINGS_DETAIL_KEY]
    cached["representations"]["text"]["vector"] = vector
    cached["representations"]["video"]["vector"] = vector
    saved_count = len(repository.saved)
    service._media_resolver = lambda _: pytest.fail("current version must not prepare media")

    assert service.index_chunk(lease) == "complete"
    assert embedder.text_calls == embedder.video_calls == 1
    assert len(store.bulk_calls) == 1
    assert len(repository.saved) == saved_count
    assert repository.completed[-1] == {ids["chunk_task"]: 2}


def test_durable_current_version_waits_for_visibility_without_rewriting(tmp_path):
    ids, media, snapshot = _fixture(tmp_path)
    repository, store, embedder = _Repository(snapshot), _Store(), _Embedder()
    service = VideoParseIndexCoordinator(
        repository, store, embedder, media_resolver=lambda _: media,
        media_service=_media_service(tmp_path), contract=_contract(),
    )
    lease = _lease(ids["video_task"], ids["chunk"], ids["chunk_task"])
    service.index_chunk(lease)
    visible = set(store.ready)
    store.ready.clear()
    service._media_resolver = lambda _: pytest.fail("durable version must not prepare media")

    with pytest.raises(VideoParseIndexCoordinationError, match="query-visible"):
        service.index_chunk(lease)
    assert len(store.bulk_calls) == len(repository.completed) == 1
    assert embedder.text_calls == embedder.video_calls == 1
    store.ready.update(visible)
    assert service.index_chunk(lease) == "complete"


def test_current_version_is_rechecked_before_completion(tmp_path, monkeypatch):
    ids, media, snapshot = _fixture(tmp_path)
    repository, store, embedder = _Repository(snapshot), _Store(), _Embedder()
    service = VideoParseIndexCoordinator(
        repository, store, embedder, media_resolver=lambda _: media,
        media_service=_media_service(tmp_path), contract=_contract(),
    )
    lease = _lease(ids["video_task"], ids["chunk"], ids["chunk_task"])
    service.index_chunk(lease)
    is_current = store.is_current

    def disappear_after_read(expected, **requirements):
        ready = is_current(expected, **requirements)
        store.ready.clear()
        return ready

    monkeypatch.setattr(store, "is_current", disappear_after_read)
    with pytest.raises(VideoParseIndexCoordinationError, match="changed before completion"):
        service.index_chunk(lease)
    assert len(store.bulk_calls) == len(repository.completed) == 1
    assert embedder.text_calls == embedder.video_calls == 1


@pytest.mark.parametrize("field,value", [
    ("content_version", 3), ("index_generation", 2), ("tenant_id", "another-tenant"),
])
def test_existing_newer_or_foreign_version_stops_before_building(tmp_path, field, value):
    ids, media, snapshot = _fixture(tmp_path)
    repository, store, embedder = _Repository(snapshot), _Store(), _Embedder()
    service = VideoParseIndexCoordinator(
        repository, store, embedder, media_resolver=lambda _: media,
        media_service=_media_service(tmp_path), contract=_contract(),
    )
    lease = _lease(ids["video_task"], ids["chunk"], ids["chunk_task"])
    service.index_chunk(lease)
    key = (ids["video_task"], ids["chunk"])
    store.states[key] = replace(store.states[key], **{field: value})
    service._media_resolver = lambda _: pytest.fail("stale or foreign target must not build")

    with pytest.raises(VideoParseIndexCoordinationError, match="newer|identity"):
        service.index_chunk(lease)
    assert len(store.bulk_calls) == len(repository.completed) == 1
    assert embedder.text_calls == embedder.video_calls == 1


def test_generation_rebuild_reencodes_each_representation(tmp_path):
    ids, media, snapshot = _fixture(tmp_path)
    repository = _Repository(snapshot)
    store = _Store()
    embedder = _Embedder()
    lease = _lease(ids["video_task"], ids["chunk"], ids["chunk_task"])

    VideoParseIndexCoordinator(
        repository, store, embedder,
        media_resolver=lambda _uri: media,
        media_service=_media_service(tmp_path),
        contract=_contract(index_generation=1),
    ).index_chunk(lease)
    snapshot.chunk.content_detail.clear()
    repository.snapshot = replace(snapshot, target_index_generation=2)

    VideoParseIndexCoordinator(
        repository, store, embedder,
        media_resolver=lambda _uri: media,
        media_service=_media_service(tmp_path),
        contract=_contract(index_generation=2),
    ).index_chunk(lease)

    assert embedder.text_calls == embedder.video_calls == 2
    assert len(store.bulk_calls) == 2
    assert store.bulk_calls[1][0][0].index_generation == 2


def test_unknown_bulk_result_completes_after_exact_readback(tmp_path):
    ids, media, snapshot = _fixture(tmp_path)
    repository = _Repository(snapshot)

    class TimedOutAfterWriteStore(_Store):
        def bulk_upsert(self, documents, *, refresh=False):
            super().bulk_upsert(documents, refresh=refresh)
            raise TimeoutError("bulk response was lost")

    store = TimedOutAfterWriteStore()
    embedder = _Embedder()
    service = VideoParseIndexCoordinator(
        repository, store, embedder,
        media_resolver=lambda _uri: media,
        media_service=_media_service(tmp_path),
        contract=_contract(),
    )

    assert service.index_chunk(
        _lease(ids["video_task"], ids["chunk"], ids["chunk_task"])
    ) == "complete"
    assert embedder.text_calls == embedder.video_calls == 1
    assert repository.completed == [{ids["chunk_task"]: 2}]


def test_schema_failure_is_not_reconciled_as_an_unknown_write(tmp_path):
    ids, media, snapshot = _fixture(tmp_path)
    repository = _Repository(snapshot)

    class IncompatibleStore(_Store):
        def bulk_upsert(self, documents, *, refresh=False):
            self.ready.update(self._ready_key(item) for item in documents)
            raise ElasticsearchIndexSchemaError("mapping differs")

        def inspect_exact(self, documents):
            raise AssertionError("deterministic schema errors must not be read back")

    service = VideoParseIndexCoordinator(
        repository, IncompatibleStore(), _Embedder(),
        media_resolver=lambda _uri: media,
        media_service=_media_service(tmp_path),
        contract=_contract(),
    )

    with pytest.raises(ElasticsearchIndexSchemaError, match="mapping differs"):
        service.index_chunk(
            _lease(ids["video_task"], ids["chunk"], ids["chunk_task"])
        )
    assert repository.completed == []


def test_failed_bulk_retries_with_cached_vectors(tmp_path):
    ids, media, snapshot = _fixture(tmp_path)
    repository = _Repository(snapshot)

    class FailedBeforeWriteStore(_Store):
        def __init__(self):
            super().__init__()
            self.attempts = 0

        def bulk_upsert(self, documents, *, refresh=False):
            self.attempts += 1
            if self.attempts == 1:
                self.bulk_calls.append((tuple(documents), refresh))
                raise RuntimeError("bulk unavailable")
            return super().bulk_upsert(documents, refresh=refresh)

    store = FailedBeforeWriteStore()
    embedder = _Embedder()
    service = VideoParseIndexCoordinator(
        repository, store, embedder,
        media_resolver=lambda _uri: media,
        media_service=_media_service(tmp_path),
        contract=_contract(),
    )
    lease = _lease(ids["video_task"], ids["chunk"], ids["chunk_task"])

    with pytest.raises(RuntimeError, match="bulk unavailable"):
        service.index_chunk(lease)
    assert repository.completed == []
    assert repository.saved[-1][3] is not None

    assert service.index_chunk(lease) == "complete"
    assert embedder.text_calls == embedder.video_calls == 1
    assert store.attempts == 2


def test_database_string_cache_retry_after_bulk_keeps_exact_dense_and_sparse_vectors(tmp_path):
    ids, media, snapshot = _fixture(tmp_path)
    negative = -0.00010395050048828125
    positive = 1.9311904907226562e-05
    detail_type = ChunkParseContent.__table__.c.detail.type
    dialect = sqlite.dialect()
    bind_detail = detail_type.bind_processor(dialect)
    restore_detail = detail_type.result_processor(dialect, None)

    class DatabaseRepository(_Repository):
        persisted_detail = None
        restored_representations = None
        complete_attempts = 0

        def save_index_embeddings(self, *args, **kwargs):
            super().save_index_embeddings(*args, **kwargs)
            self.persisted_detail = bind_detail(self.snapshot.chunk.content_detail)

        def load_chunk_indexing_snapshot(self, lease, *, now=None):
            if self.persisted_detail is not None:
                chunk = replace(self.snapshot.chunk, content_detail=restore_detail(self.persisted_detail))
                self.restored_representations = chunk.content_detail[INDEX_EMBEDDINGS_DETAIL_KEY]["representations"]
                self.snapshot = replace(self.snapshot, chunk=chunk)
            return super().load_chunk_indexing_snapshot(lease, now=now)

        def complete_chunk_indexing(self, lease, content_version, index_generation, *, now=None):
            self.complete_attempts += 1
            if self.complete_attempts == 1:
                raise RuntimeError("completion transaction failed after successful bulk")
            return super().complete_chunk_indexing(lease, content_version, index_generation, now=now)

    class PreciseEmbedder(_Embedder):
        sparse_calls = 0

        def embed_text(self, text):
            return super().embed_text(text).model_copy(update={"vector": [negative, positive]})

        def embed_video(self, path):
            return super().embed_video(path).model_copy(update={"vector": [positive, negative]})

        def embed_sparse(self, text):
            self.sparse_calls += 1
            with track_model_call(provider="test", model="sparse-v1", operation="sparse_embedding"):
                return SparseEmbeddingResult(model="sparse-v1", weights={"utterance": positive})

    repository, store, embedder = DatabaseRepository(snapshot), _Store(), PreciseEmbedder()
    contract = replace(_contract(), sparse_encoder=SparseEncoderContract(
        provider="test", model="sparse-v1", encoder_revision="v1",
    ))
    service = VideoParseIndexCoordinator(
        repository, store, embedder, media_resolver=lambda _: media,
        media_service=_media_service(tmp_path), contract=contract,
    )
    lease = _lease(ids["video_task"], ids["chunk"], ids["chunk_task"])
    model_calls = []
    with capture_parse_usage(model_calls.append):
        with pytest.raises(RuntimeError, match="completion transaction failed"):
            service.index_chunk(lease)

    assert len(store.bulk_calls) == 1 and repository.completed == []
    persisted = json.loads(repository.persisted_detail)[INDEX_EMBEDDINGS_DETAIL_KEY]
    assert isinstance(persisted["representations"], str)
    assert type(persisted["content_version"]) is int
    assert type(persisted["receipt"]["index_generation"]) is int
    with capture_parse_usage(model_calls.append):
        assert service.index_chunk(lease) == "complete"

    restored = repository.restored_representations
    assert restored["text"]["vector"] == [negative, positive]
    assert restored["video"]["vector"] == [positive, negative]
    assert all(type(value) is float for result in (restored["text"], restored["video"]) for value in result["vector"])
    assert restored["sparse"]["weights"] == {"utterance": positive}
    assert type(restored["sparse"]["weights"]["utterance"]) is float
    assert embedder.text_calls == embedder.video_calls == embedder.sparse_calls == 1
    assert len(model_calls) == 3 and len(store.bulk_calls) == 1
    assert repository.completed == [{ids["chunk_task"]: 2}]
    document = store.bulk_calls[0][0][0]
    assert (document.content_version, document.index_generation) == (2, contract.index_generation)
    assert document.semantic_vector == (negative, positive)
    assert document.vision_vector == (positive, negative)
    assert document.sparse_token_weights == (("utterance", positive),)


def test_cached_vectors_without_current_generation_proof_are_reencoded(tmp_path):
    ids, media, snapshot = _fixture(tmp_path)
    repository = _Repository(snapshot)
    store = _Store()
    embedder = _Embedder()
    service = VideoParseIndexCoordinator(
        repository,
        store,
        embedder,
        media_resolver=lambda _uri: media,
        media_service=_media_service(tmp_path),
        contract=_contract(),
    )
    lease = _lease(ids["video_task"], ids["chunk"], ids["chunk_task"])
    service.index_chunk(lease)

    cached = snapshot.chunk.content_detail[INDEX_EMBEDDINGS_DETAIL_KEY]
    cached["receipt"].pop("index_generation")
    store.ready.clear()
    store.states.clear()
    store.bulk_calls.clear()

    service.index_chunk(lease)

    assert embedder.text_calls == 2
    assert embedder.video_calls == 2
    refreshed = snapshot.chunk.content_detail[INDEX_EMBEDDINGS_DETAIL_KEY]
    assert refreshed["representations"]["text"] == {"channel": "semantic", "vector": [1.0, 0.0]}
    assert refreshed["representations"]["video"] == {"channel": "vision", "vector": [0.0, 1.0]}
    assert refreshed["receipt"] == store.bulk_calls[0][0][0].receipt.as_dict()


def test_snapshot_generation_mismatch_fails_before_embedding_or_write(tmp_path):
    ids, media, snapshot = _fixture(tmp_path)
    repository, store, embedder = _Repository(snapshot), _Store(), _Embedder()
    service = VideoParseIndexCoordinator(
        repository, store, embedder, media_resolver=lambda _: media,
        media_service=_media_service(tmp_path), contract=_contract(index_generation=2),
    )
    with pytest.raises(VideoParseIndexCoordinationError, match="target generation"):
        service.index_chunk(_lease(ids["video_task"], ids["chunk"], ids["chunk_task"]))
    assert embedder.text_calls == embedder.video_calls == 0
    assert store.bulk_calls == repository.completed == []


def test_invalid_parse_content_stops_before_embedding_or_external_write(tmp_path):
    ids, media, snapshot = _fixture(tmp_path)
    snapshot.chunk.content["timeline"][0]["speaker"] = "未知人物"
    snapshot.chunk.content["timeline"][0]["participants"].append("未知人物")
    repository = _Repository(snapshot)
    store = _Store()
    embedder = _Embedder()
    service = VideoParseIndexCoordinator(
        repository, store, embedder,
        media_resolver=lambda _uri: media,
        media_service=_media_service(tmp_path),
        contract=_contract(),
    )

    with pytest.raises(ValueError, match="unknown person"):
        service.index_chunk(
            _lease(ids["video_task"], ids["chunk"], ids["chunk_task"])
        )
    assert embedder.text_calls == embedder.video_calls == 0
    assert repository.saved == repository.completed == []
    assert store.bulk_calls == []


def test_empty_timeline_needs_only_the_required_vision_representation(tmp_path):
    ids, media, snapshot = _fixture(tmp_path)
    snapshot.chunk.content.clear()
    snapshot.chunk.content.update({"persons": [], "timeline": []})
    repository = _Repository(snapshot)
    store = _Store()
    embedder = _Embedder()
    service = VideoParseIndexCoordinator(
        repository, store, embedder,
        media_resolver=lambda _uri: media,
        media_service=_media_service(tmp_path),
        contract=_contract(),
    )

    service.index_chunk(
        _lease(ids["video_task"], ids["chunk"], ids["chunk_task"])
    )
    assert embedder.text_calls == 0
    assert embedder.video_calls == 1
    document = store.bulk_calls[0][0][0]
    assert document.semantic_vector is None
    assert document.projection.semantic_text == ""


def test_partial_embedding_success_is_fenced_and_reused_on_retry(tmp_path):
    ids, media, snapshot = _fixture(tmp_path)
    repository = _Repository(snapshot)
    store = _Store()

    class FailVideoOnce(_Embedder):
        def embed_video(self, video_path):
            self.video_calls += 1
            if self.video_calls == 1:
                raise RuntimeError("video provider unavailable")
            return EmbeddingResult(
                model="embed-v1", modality="video", vector=[0, 1]
            )

    embedder = FailVideoOnce()
    service = VideoParseIndexCoordinator(
        repository, store, embedder,
        media_resolver=lambda _uri: media,
        media_service=_media_service(tmp_path),
        contract=_contract(),
    )
    lease = _lease(ids["video_task"], ids["chunk"], ids["chunk_task"])

    with pytest.raises(RuntimeError, match="provider unavailable"):
        service.index_chunk(lease)
    assert embedder.text_calls == embedder.video_calls == 1
    assert repository.saved[-1][2].keys() == {"text"}
    assert store.bulk_calls == repository.completed == []

    assert service.index_chunk(lease) == "complete"
    assert embedder.text_calls == 1
    assert embedder.video_calls == 2
    assert len(store.bulk_calls) == 1


def test_content_change_reencodes_all_representations(tmp_path):
    ids, media, snapshot = _fixture(tmp_path)
    repository = _Repository(snapshot)
    store = _Store()
    embedder = _Embedder()
    service = VideoParseIndexCoordinator(
        repository, store, embedder,
        media_resolver=lambda _uri: media,
        media_service=_media_service(tmp_path),
        contract=_contract(),
    )
    lease = _lease(ids["video_task"], ids["chunk"], ids["chunk_task"])
    service.index_chunk(lease)

    chunk = snapshot.chunk
    chunk.content["timeline"][0]["text_variants"][0]["text"] = "我很好"
    repository.snapshot = replace(
        snapshot, chunk=replace(chunk, content_version=3)
    )
    service.index_chunk(lease)

    assert embedder.text_calls == 2
    assert embedder.video_calls == 2
    assert repository.completed[-1] == {ids["chunk_task"]: 3}
    assert len(store.bulk_calls) == 2


def test_lost_execution_lease_stops_before_external_write_or_complete(tmp_path):
    ids, media, snapshot = _fixture(tmp_path)
    repository = _Repository(snapshot)
    store = _Store()
    embedder = _Embedder()

    def check_active():
        if embedder.video_calls and len(repository.saved) >= 2:
            raise RuntimeError("execution lease lost")

    service = VideoParseIndexCoordinator(
        repository, store, embedder,
        media_resolver=lambda _uri: media,
        media_service=_media_service(tmp_path),
        contract=_contract(),
        check_active=check_active,
    )

    with pytest.raises(RuntimeError, match="lease lost"):
        service.index_chunk(
            _lease(ids["video_task"], ids["chunk"], ids["chunk_task"])
        )
    assert embedder.text_calls == embedder.video_calls == 1
    assert store.bulk_calls == repository.completed == []


def test_coordinator_calls_configured_model_without_a_local_fps_capability_gate(tmp_path):
    ids, media, snapshot = _fixture(tmp_path)
    repository = _Repository(snapshot)
    store = _Store()
    embedder = _Embedder()
    # A local capability flag must not reject the user-configured model.
    embedder.supports_video_fps = False
    service = VideoParseIndexCoordinator(
        repository, store, embedder, media_resolver=lambda _: media,
        media_service=_media_service(tmp_path), contract=_contract(),
    )

    assert service.index_chunk(
        _lease(ids["video_task"], ids["chunk"], ids["chunk_task"])
    ) == "complete"
    assert embedder.text_calls == embedder.video_calls == 1
    assert repository.completed == [{ids["chunk_task"]: 2}]
    assert len(store.bulk_calls) == 1


@pytest.mark.parametrize("field,value", [
    ("parse_task_id", "other-task"), ("video_chunk_id", "other-chunk"),
    ("content_version", 1), ("index_generation", 2),
])
def test_cached_results_require_the_current_outer_plan_identity(tmp_path, field, value):
    ids, media, snapshot = _fixture(tmp_path)
    repository, store, embedder = _Repository(snapshot), _Store(), _Embedder()
    service = VideoParseIndexCoordinator(
        repository, store, embedder, media_resolver=lambda _: media,
        media_service=_media_service(tmp_path), contract=_contract(),
    )
    lease = _lease(ids["video_task"], ids["chunk"], ids["chunk_task"])
    service.index_chunk(lease)
    snapshot.chunk.content_detail[INDEX_EMBEDDINGS_DETAIL_KEY]["receipt"][field] = value
    store.states.clear()
    store.ready.clear()
    service.index_chunk(lease)
    assert embedder.text_calls == embedder.video_calls == 2
    for _, _, representations, receipt in repository.saved:
        assert receipt is not None
        assert set(receipt) == {"parse_task_id", "video_chunk_id", "content_version", "index_generation"}
        assert all(set(result) == {"channel", "vector"} for result in representations.values())


def test_sparse_partial_failure_restores_dense_results_and_binds_current_sparse_request(tmp_path):
    ids, media, snapshot = _fixture(tmp_path)
    repository, store = _Repository(snapshot), _Store()

    class SparseEmbedder(_Embedder):
        sparse_calls = 0

        def embed_sparse(self, text):
            assert "我没事" in text
            self.sparse_calls += 1
            if self.sparse_calls == 1:
                raise RuntimeError("sparse provider unavailable")
            return SparseEmbeddingResult(model="sparse-v1", weights={"utterance": 0.5, "inactive": 0.0})

    embedder = SparseEmbedder()
    contract = replace(_contract(), sparse_encoder=SparseEncoderContract(
        provider="test", model="sparse-v1", encoder_revision="v1",
    ))
    service = VideoParseIndexCoordinator(
        repository, store, embedder, media_resolver=lambda _: media,
        media_service=_media_service(tmp_path), contract=contract,
    )
    lease = _lease(ids["video_task"], ids["chunk"], ids["chunk_task"])
    with pytest.raises(RuntimeError, match="sparse provider unavailable"):
        service.index_chunk(lease)
    assert store.bulk_calls == repository.completed == []
    assert repository.saved[-1][3]["index_generation"] == contract.index_generation
    assert service.index_chunk(lease) == "complete"
    assert embedder.text_calls == embedder.video_calls == 1
    assert embedder.sparse_calls == 2
    document = store.bulk_calls[-1][0][0]
    assert document.sparse_token_weights == (("utterance", 0.5),)
    assert repository.saved[-1][2]["sparse"] == {"channel": "sparse", "weights": {"utterance": 0.5}}
    snapshot.chunk.content_detail.clear()
    assert service.index_chunk(lease) == "complete"
    assert embedder.text_calls == embedder.video_calls == 1
    assert embedder.sparse_calls == 2


def test_generation_change_does_not_reuse_a_stale_channel_after_partial_failure(tmp_path):
    ids, media, snapshot = _fixture(tmp_path)
    repository, store = _Repository(snapshot), _Store()

    class FailVideoOnce(_Embedder):
        fail = False

        def embed_video(self, path):
            result = super().embed_video(path)
            if self.fail:
                self.fail = False
                raise RuntimeError("video provider unavailable")
            return result

    embedder = FailVideoOnce()
    lease = _lease(ids["video_task"], ids["chunk"], ids["chunk_task"])
    VideoParseIndexCoordinator(
        repository, store, embedder, media_resolver=lambda _: media,
        media_service=_media_service(tmp_path), contract=_contract(),
    ).index_chunk(lease)
    repository.snapshot = replace(snapshot, target_index_generation=2)
    service = VideoParseIndexCoordinator(
        repository, store, embedder, media_resolver=lambda _: media,
        media_service=_media_service(tmp_path), contract=_contract(index_generation=2),
    )
    embedder.fail = True
    with pytest.raises(RuntimeError, match="video provider unavailable"):
        service.index_chunk(lease)
    assert set(repository.saved[-1][2]) == {"text"}
    assert repository.saved[-1][3]["index_generation"] == 2
    assert service.index_chunk(lease) == "complete"
    assert (embedder.text_calls, embedder.video_calls) == (2, 3)
    assert store.bulk_calls[-1][0][0].index_generation == 2
