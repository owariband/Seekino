from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from engine.parsev1.usage import capture_parse_usage
from engine.rag import (
    ChunkIndexingContract,
    EncoderContract,
    OrderedFramesInputContract,
    VideoClipInputContract,
    VIDEO_CLIP_PREPROCESS_REVISION,
)
from gateway.embedding import EmbeddingResult
from gateway.usage import track_model_call
from server.model.dto.jobDTO import (
    IndexableChunkState,
    IntervalIndexingSnapshot,
    IntervalParseLease,
)
from server.repository.video_parse import INDEX_EMBEDDINGS_DETAIL_KEY
from server.service.video_parse_index_coordinator import (
    VideoParseIndexCoordinator,
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


def _lease(video_task_id, interval_id, chunk_task_id):
    return IntervalParseLease(
        interval_parse_task_id=interval_id,
        video_parse_task_id=video_task_id,
        chunk_parse_task_ids=(chunk_task_id,),
        start_sequence_no=0,
        chunk_count=1,
        phase="INDEXING",
        worker_id="worker-1",
        lease_version=1,
        lease_expire_time=now_utc() + timedelta(minutes=5),
        retry_count=0,
        max_retry_count=2,
    )


class _Repository:
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.saved = []
        self.completed = []

    def load_interval_indexing_snapshot(self, lease, *, now=None):
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
        chunk = next(
            item
            for item in self.snapshot.chunks
            if item.chunk_parse_task_id == chunk_parse_task_id
        )
        envelope = {
            "content_version": content_version,
            "representations": representations,
        }
        if receipt is not None:
            envelope["receipt"] = receipt
        chunk.content_detail[INDEX_EMBEDDINGS_DETAIL_KEY] = envelope

    def complete_interval_indexing(self, lease, indexed_versions, *, now=None):
        self.completed.append(dict(indexed_versions))
        return "complete"


class _Store:
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
                workspace_id=item.workspace_id,
                series_id=item.series_id,
                video_id=item.video_id,
                parse_task_id=item.parse_task_id,
                video_chunk_id=item.video_chunk_id,
                content_version=item.content_version,
                index_generation=item.index_generation,
                source_revision=item.source_revision,
                retrieval_contract_id=item.retrieval_contract_id,
                semantic_encoder_id=item.semantic_encoder_id,
                vision_encoder_id=item.vision_encoder_id,
                semantic_input_sha256=item.semantic_input_sha256,
                vision_input_sha256=item.vision_input_sha256,
                semantic_vector=tuple(item.semantic_vector) if item.semantic_vector else None,
                vision_vector=tuple(item.vision_vector),
                semantic_vector_present=item.semantic_vector is not None,
                vision_vector_present=True,
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
            item.source_revision,
            item.retrieval_contract_id,
            item.semantic_encoder_id,
            item.vision_encoder_id,
            item.semantic_input_sha256,
            item.vision_input_sha256,
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
        workspace_id=ids["workspace"],
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
    snapshot = IntervalIndexingSnapshot(
        interval_parse_task_id=ids["interval"],
        video_parse_task_id=ids["video_task"],
        chunks=(chunk,),
        indexed_versions={},
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
    lease = _lease(ids["video_task"], ids["interval"], ids["chunk_task"])

    model_calls = []
    with capture_parse_usage(model_calls.append):
        assert service.index_interval(lease) == "complete"
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
    assert snapshot.chunks[0].content_detail[
        INDEX_EMBEDDINGS_DETAIL_KEY
    ]["receipt"] == receipt

    # Losing the DB cache still reuses the exactly proven ES representations.
    snapshot.chunks[0].content_detail.clear()
    with capture_parse_usage(model_calls.append):
        assert service.index_interval(lease) == "complete"
    assert embedder.text_calls == embedder.video_calls == 1
    assert len(model_calls) == 2
    assert len(store.bulk_calls) == 1


def test_indexing_writes_the_configured_generation(tmp_path):
    ids, media, snapshot = _fixture(tmp_path)
    repository = _Repository(snapshot)
    store = _Store()
    service = VideoParseIndexCoordinator(
        repository, store, _Embedder(),
        media_resolver=lambda _uri: media,
        media_service=_media_service(tmp_path),
        contract=_contract(index_generation=7),
    )

    service.index_interval(
        _lease(ids["video_task"], ids["interval"], ids["chunk_task"])
    )

    document = store.bulk_calls[0][0][0]
    assert document.index_generation == 7


def test_generation_rebuild_reuses_compatible_stored_vectors(tmp_path):
    ids, media, snapshot = _fixture(tmp_path)
    repository = _Repository(snapshot)
    store = _Store()
    embedder = _Embedder()
    lease = _lease(ids["video_task"], ids["interval"], ids["chunk_task"])

    VideoParseIndexCoordinator(
        repository, store, embedder,
        media_resolver=lambda _uri: media,
        media_service=_media_service(tmp_path),
        contract=_contract(index_generation=1),
    ).index_interval(lease)
    snapshot.chunks[0].content_detail.clear()

    VideoParseIndexCoordinator(
        repository, store, embedder,
        media_resolver=lambda _uri: media,
        media_service=_media_service(tmp_path),
        contract=_contract(index_generation=2),
    ).index_interval(lease)

    assert embedder.text_calls == embedder.video_calls == 1
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

    assert service.index_interval(
        _lease(ids["video_task"], ids["interval"], ids["chunk_task"])
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
        service.index_interval(
            _lease(ids["video_task"], ids["interval"], ids["chunk_task"])
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
    lease = _lease(ids["video_task"], ids["interval"], ids["chunk_task"])

    with pytest.raises(RuntimeError, match="bulk unavailable"):
        service.index_interval(lease)
    assert repository.completed == []
    assert repository.saved[-1][3] is not None

    assert service.index_interval(lease) == "complete"
    assert embedder.text_calls == embedder.video_calls == 1
    assert store.attempts == 2


def test_legacy_receipts_reuse_proven_text_and_reencode_unproven_video(tmp_path):
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
    lease = _lease(ids["video_task"], ids["interval"], ids["chunk_task"])
    service.index_interval(lease)

    cached = snapshot.chunks[0].content_detail[INDEX_EMBEDDINGS_DETAIL_KEY]
    cached["representations"]["text"].pop("encoder_id")
    for field in (
        "encoder_id",
        "input_contract_id",
        "visual_input_kind",
        "max_height",
        "preprocess_revision",
    ):
        cached["representations"]["video"].pop(field)
    cached.pop("receipt")
    store.ready.clear()
    store.states.clear()
    store.bulk_calls.clear()

    service.index_interval(lease)

    assert embedder.text_calls == 1
    assert embedder.video_calls == 2
    refreshed = snapshot.chunks[0].content_detail[INDEX_EMBEDDINGS_DETAIL_KEY]
    assert refreshed["representations"]["text"]["encoder_id"]
    assert refreshed["representations"]["video"]["max_height"] == 720
    assert refreshed["receipt"] == store.bulk_calls[0][0][0].receipt.as_dict()


def test_partial_bulk_retry_writes_only_the_missing_document(tmp_path):
    ids, media, snapshot = _fixture(tmp_path)
    second_chunk_task_id = str(uuid4())
    second_chunk = replace(
        snapshot.chunks[0],
        chunk_parse_task_id=second_chunk_task_id,
        video_chunk_id=str(uuid4()),
        sequence_no=1,
        content_detail={},
    )
    snapshot = replace(
        snapshot,
        chunks=(snapshot.chunks[0], second_chunk),
    )
    repository = _Repository(snapshot)

    class PartiallyFailedStore(_Store):
        def __init__(self):
            super().__init__()
            self.first_attempt = True

        def bulk_upsert(self, documents, *, refresh=False):
            if self.first_attempt:
                self.first_attempt = False
                _Store.bulk_upsert(self, documents[:1], refresh=refresh)
                self.bulk_calls[-1] = (tuple(documents), refresh)
                raise RuntimeError("one bulk item failed")
            return _Store.bulk_upsert(self, documents, refresh=refresh)

    store = PartiallyFailedStore()
    embedder = _Embedder()
    service = VideoParseIndexCoordinator(
        repository, store, embedder,
        media_resolver=lambda _uri: media,
        media_service=_media_service(tmp_path),
        contract=_contract(),
    )
    lease = replace(
        _lease(ids["video_task"], ids["interval"], ids["chunk_task"]),
        chunk_parse_task_ids=(ids["chunk_task"], second_chunk_task_id),
        chunk_count=2,
    )

    with pytest.raises(RuntimeError, match="bulk item failed"):
        service.index_interval(lease)
    assert len(store.bulk_calls[0][0]) == 2

    assert service.index_interval(lease) == "complete"
    assert len(store.bulk_calls[1][0]) == 1
    assert store.bulk_calls[1][0][0].video_chunk_id == second_chunk.video_chunk_id
    assert embedder.text_calls == embedder.video_calls == 2
    assert repository.completed == [{
        ids["chunk_task"]: 2,
        second_chunk_task_id: 2,
    }]


def test_invalid_parse_content_stops_before_embedding_or_external_write(tmp_path):
    ids, media, snapshot = _fixture(tmp_path)
    snapshot.chunks[0].content["timeline"][0]["speaker"] = "未知人物"
    snapshot.chunks[0].content["timeline"][0]["participants"].append("未知人物")
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
        service.index_interval(
            _lease(ids["video_task"], ids["interval"], ids["chunk_task"])
        )
    assert embedder.text_calls == embedder.video_calls == 0
    assert repository.saved == repository.completed == []
    assert store.bulk_calls == []


def test_empty_timeline_needs_only_the_required_vision_representation(tmp_path):
    ids, media, snapshot = _fixture(tmp_path)
    snapshot.chunks[0].content.clear()
    snapshot.chunks[0].content.update({"persons": [], "timeline": []})
    repository = _Repository(snapshot)
    store = _Store()
    embedder = _Embedder()
    service = VideoParseIndexCoordinator(
        repository, store, embedder,
        media_resolver=lambda _uri: media,
        media_service=_media_service(tmp_path),
        contract=_contract(),
    )

    service.index_interval(
        _lease(ids["video_task"], ids["interval"], ids["chunk_task"])
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
    lease = _lease(ids["video_task"], ids["interval"], ids["chunk_task"])

    with pytest.raises(RuntimeError, match="provider unavailable"):
        service.index_interval(lease)
    assert embedder.text_calls == embedder.video_calls == 1
    assert repository.saved[-1][2].keys() == {"text"}
    assert store.bulk_calls == repository.completed == []

    assert service.index_interval(lease) == "complete"
    assert embedder.text_calls == 1
    assert embedder.video_calls == 2
    assert len(store.bulk_calls) == 1


def test_content_change_reencodes_only_changed_text_representation(tmp_path):
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
    lease = _lease(ids["video_task"], ids["interval"], ids["chunk_task"])
    service.index_interval(lease)

    chunk = snapshot.chunks[0]
    chunk.content["timeline"][0]["text_variants"][0]["text"] = "我很好"
    repository.snapshot = replace(
        snapshot, chunks=(replace(chunk, content_version=3),)
    )
    service.index_interval(lease)

    assert embedder.text_calls == 2
    assert embedder.video_calls == 1
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
        service.index_interval(
            _lease(ids["video_task"], ids["interval"], ids["chunk_task"])
        )
    assert embedder.text_calls == embedder.video_calls == 1
    assert store.bulk_calls == repository.completed == []
