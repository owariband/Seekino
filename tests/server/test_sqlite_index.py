from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select, text

from engine.rag.indexing.events import EventIndexingContract
from engine.rag.retrieval.contracts import EventFilters, EventIndexNotReadyError, RetrievalPolicy
from server.desktop.database import open_desktop_database
from server.model.parse_content import ChunkParseContent
from server.model.task import ChunkParseTask
from server.service.video_event_retrieval import VideoEventRetrievalService
from server.service.video_parse_persistence import INDEX_EMBEDDINGS_DETAIL_KEY, VideoParseLeaseLostError, VideoParsePersistenceService
from server.service.video_parse_index_coordinator import VideoParseIndexCoordinator
from server.storage.index_contract import IndexSchemaError
from server.storage.sqlite_index import SQLiteSearchIndex
from tests.server.test_video_parse_index_coordinator import _Embedder, _contract
from tests.server.test_video_parse_repository import _event_index_fixture
from tests.server.test_video_event_retrieval import FakeQueryEmbedder


@pytest.fixture
def library(tmp_path):
    pytest.importorskip("sqlite_vec", reason="install the desktop dependency group for native index tests")
    db = open_desktop_database(tmp_path / "library.sqlite3")
    try:
        yield db
    finally:
        db.close()


def test_local_publication_retries_without_encoding_and_searches_current_source(library, monkeypatch):
    # Prepare one real, refined Chunk and an active INDEX lease at a fixed clock.
    _, queue, _, task_id, lease, at, parent = _event_index_fixture((library.engine, None))
    factory = library.session_factory
    persistence = VideoParsePersistenceService(factory)
    monkeypatch.setattr("server.service.video_parse_persistence.now_utc", lambda: at)
    with factory.begin() as session:
        content = session.scalar(select(ChunkParseContent))
        content.content = {"persons": [], "timeline": [
            {"event_type": "ACTION", "start_seconds": 1000, "end_seconds": 4000,
             "description": "爱音拿起蓝色杯子 Blue cup", "participants": [], "visible_persons": []},
            {"event_type": "ACTION", "start_seconds": 4000, "end_seconds": 6000,
             "description": "另一只蓝色杯子 Cupboard", "participants": [], "visible_persons": []},
        ]}
    store = SQLiteSearchIndex(factory, semantic_dimensions=2, vision_dimensions=2, vision_enabled=False)
    chunk_contract = replace(_contract(), vision_encoder=None, visual_input_contract=None)
    contract = EventIndexingContract(semantic_encoder=chunk_contract.semantic_encoder, vision_encoder=None, capture=None)
    target = persistence.bind_event_index_target(lease, {
        "index_name": store.index_name, "contract_id": contract.contract_id, "contract": contract.as_dict(),
    })
    snapshot = persistence.load_chunk_indexing_snapshot(lease, event_target=target)
    chunk = snapshot.chunk
    embedder = _Embedder()
    coordinator = VideoParseIndexCoordinator(
        persistence, store, embedder, contract=chunk_contract, media_resolver=None, media_service=None,
        event_store=store, event_target=target, local_publisher=store,
    )
    publish = store.publish

    def fail_before_commit(*args):
        publish(*args)
        raise RuntimeError("simulated interrupted publication")

    monkeypatch.setattr(store, "publish", fail_before_commit)
    with pytest.raises(RuntimeError, match="interrupted publication"):
        coordinator.index_chunk(lease)
    key = (task_id, chunk.video_chunk_id)
    options = dict(sources=[(*key, chunk.content_version)], limit=1, filters=EventFilters())
    assert store.verify_absent([key])
    assert not store.search_lexical("爱音", **options)
    with factory() as session:
        assert session.get(ChunkParseTask, lease.chunk_parse_task_id).status == "RUNNING"
        checkpoint = session.scalar(select(ChunkParseContent)).detail[INDEX_EMBEDDINGS_DETAIL_KEY]
        assert "events" not in checkpoint["receipt"]
    calls = embedder.text_calls
    assert calls >= 3

    monkeypatch.setattr(store, "publish", publish)
    assert queue.release_execution(parent, now=at)
    coordinator.index_chunk(lease)
    assert embedder.text_calls == calls
    # Both a two-character name and a four-character phrase match inside a
    # Chinese sentence; an English word does not pick the substring in cupboard.
    for query in ("爱音", '"爱音"！', "cup"):
        hits = store.search_lexical(query, **{**options, "limit": 10})
        assert [item.timeline_index for item in hits] == [0]
        assert hits[0].matched_fields[0].raw_text.endswith("Blue cup")
    assert len(store.search_lexical("蓝色杯子", **{**options, "limit": 10})) == 2
    assert store.search_vector([1., 0.], channel="semantic", **options)[0].score == pytest.approx(2)
    for sources in ([], [(str(uuid4()), key[1], chunk.content_version)], [(*key, chunk.content_version + 1)]):
        assert not store.search_vector([1., 0.], channel="semantic", **{**options, "sources": sources})
        assert not store.search_lexical("爱音", **{**options, "sources": sources})
    scoped = {**options, "filters": EventFilters(speaker="other")}
    assert not store.search_vector([1., 0.], channel="semantic", **scoped)
    assert not store.search_lexical("爱音", **scoped)
    with pytest.raises(IndexSchemaError, match="stored Event encoder"):
        store.bind_contract(contract.model_copy(update={"description_language": "ja"}), create=True)
    with pytest.raises(VideoParseLeaseLostError):
        coordinator.index_chunk(lease)

    service = VideoEventRetrievalService(persistence, store, FakeQueryEmbedder([1., 0.]), contract=contract,
        policy=RetrievalPolicy(lexical_weight=1., semantic_weight=0., vision_weight=0.,
                               channel_top_k=10, candidate_budget=20, top_k=1, rrf_k=60))
    results = service.search("爱音", tenant_id=chunk.tenant_id, series_ids=[chunk.series_id])
    assert [(hit.start_ms, hit.end_ms) for hit in results.matches] == [(1000, 4000)]
    assert results.chunk_result.hits[0].evidence.video_id == chunk.video_id

    store.bulk_delete([key], refresh="wait_for")
    assert store.verify_absent([key])
    with pytest.raises(EventIndexNotReadyError, match="rebuilt"):
        service.search("爱音", tenant_id=chunk.tenant_id, series_ids=[chunk.series_id])
    assert not store.search_lexical("爱音", **options)
    assert not store.search_vector([1., 0.], channel="semantic", **options)
    with factory() as session:
        assert session.scalar(text("SELECT count(*) FROM local_event_fields")) == 0
        assert session.scalar(text("SELECT count(*) FROM local_event_vectors")) == 0

    # Rebuild from the saved embeddings after deleting the derived projections.
    assert persistence.request_index_rebuild(task_id, event_target=target, now=at)
    assert queue.claim_video("rebuild-parent", lease_duration=timedelta(minutes=5), now=at)

    retry = queue.claim_chunk_index("rebuild", lease_duration=timedelta(minutes=5), now=at)
    assert retry is not None
    coordinator.index_chunk(retry)
    assert embedder.text_calls == calls
    assert store.search_lexical("爱音", **options)[0].timeline_index == 0


def _store(sessions):
    return SQLiteSearchIndex(sessions, semantic_dimensions=2, vision_dimensions=2, vision_enabled=True)


def _add_video(store, session, contract, *, task, chunk_id, tenant, series, vector, vision):
    from engine.rag import VideoChunkIndexDocument
    from engine.rag.indexing.events import EventIndexDocument, build_event_index_receipt
    from engine.rag.indexing.projection import ChunkIndexProjection

    chunk = VideoChunkIndexDocument(
        tenant_id=tenant, series_id=series, video_id=str(uuid4()), video_chunk_id=chunk_id,
        parse_task_id=task, content_version=1, index_generation=2, chunk_start_seconds=0, chunk_end_seconds=4000,
        projection=ChunkIndexProjection(lexical_fields=[], semantic_text="端着蓝色杯子"),
        semantic_vector=vector, vision_vector=vision,
    )
    event = EventIndexDocument.model_validate({
        "parse_task_id": task, "video_chunk_id": chunk_id, "version": 1, "timeline_index": 0, "event_type": "SCENE",
        "lexical_fields": [{"field": "event_description", "raw_text": "端着蓝色杯子"}],
        "semantic_vectors": [{"source": "description", "vector": vector}], "vision_vector": vision,
        "detail": {"capture": {"frames": [{"requested_at_ms": 1000, "image_sha256": "1" * 64}]}},
    })
    store.publish(session, chunk, [event], build_event_index_receipt(store.index_name, contract, [event]))
    return (task, chunk_id, 1)


def test_many_source_tuples_filter_before_top_k_and_reopened_connections_load_vec(library):
    import sqlite3
    from uuid import UUID

    from engine.rag.vision.event_frames import EventFrameContract
    from engine.rag.retrieval.contracts import EventIndexNotReadyError

    contract = EventIndexingContract(semantic_encoder=_contract().semantic_encoder, vision_encoder=_contract().vision_encoder,
                                    capture=EventFrameContract(max_height=720))
    sessions = library.session_factory
    store = _store(sessions)
    store.bind_contract(contract, create=True)
    owner, other_owner, series, other_series = (str(uuid4()) for _ in range(4))
    task, chunk, foreign_task, foreign_chunk = (str(uuid4()) for _ in range(4))
    with sessions.begin() as session:
        allowed = _add_video(store, session, contract, task=task, chunk_id=chunk,
                             tenant=owner, series=series, vector=(0., 1.), vision=(1., 0.))
        another = _add_video(store, session, contract, task=str(uuid4()), chunk_id=str(uuid4()),
                             tenant=owner, series=series, vector=(.5, .5), vision=(0., 1.))
        _add_video(store, session, contract, task=foreign_task, chunk_id=foreign_chunk,
                   tenant=other_owner, series=other_series, vector=(1., 0.), vision=(1., 0.))
        # A reparse can reuse the physical Chunk ID with a different task.
        _add_video(store, session, contract, task=str(uuid4()), chunk_id=chunk, tenant=owner,
                   series=other_series, vector=(1., 0.), vision=(1., 0.))

    # Simulate a host restart. Each new sqlite3 connection must load its own
    # sqlite-vec functions; they do not persist in the on-disk database.
    library.engine.dispose()
    reopened = _store(sessions)
    reopened.bind_contract(contract)
    with pytest.raises(IndexSchemaError):
        reopened.bind_contract(contract.model_copy(update={"description_language": "ja"}), create=True)
    reopened.require_coverage({allowed: 2, another: 2})
    with pytest.raises(EventIndexNotReadyError):
        reopened.require_coverage({allowed: 1})
    # Lower SQLite's *real connection* cap to make a placeholder-per-Chunk
    # implementation fail. The scope is transmitted as one JSON parameter.
    with sessions() as occupied:
        occupied.connection()
        with sessions() as spare:
            dbapi = spare.connection().connection.driver_connection
            previous = dbapi.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 12)
        try:
            many = [(str(UUID(int=i+1)), foreign_chunk, 1) for i in range(2048)]
            opts = dict(sources=[*many, allowed, another, (task, foreign_chunk, 1), (*allowed[:2], 2)],
                        filters=EventFilters(), limit=1)
            assert reopened.search_vector([1., 0.], channel="semantic", **opts)[0].parse_task_id == another[0]
            assert reopened.search_vector([1., 0.], channel="vision", **opts)[0].parse_task_id == task
            assert {hit.parse_task_id for hit in reopened.search_lexical("杯子", **{**opts, "limit": 20})} == {
                task, another[0],
            }
        finally:
            dbapi.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, previous)
