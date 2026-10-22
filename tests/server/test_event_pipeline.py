from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select

from agent.chat.video_search.output import merge_retrieval_calls
from agent.chat.video_search.state import RetrievalCall, LexicalRetrievalChannelWeights
from engine.rag.indexing.events import EventIndexingContract
from engine.rag.retrieval.contracts import RawEventHit, RetrievalPolicy
from engine.rag.retrieval.events import EVENT_RETRIEVAL_STRATEGY_VERSION
from engine.rag.vision.event_frames import EventFrameContract
from gateway.embedding import EmbeddingResult
from server.model.job import ChunkParseContent, ChunkParseTask, VideoParseTask
from server.service import video_parse_persistence as persistence_module
from server.service.video_parse_persistence import INDEX_EMBEDDINGS_DETAIL_KEY
from server.service.video_event_retrieval import VideoEventRetrievalService
from server.service.video_parse_index_coordinator import VideoParseIndexCoordinator
from server.service.video_parse_media import VideoParseMediaService
from tests.server.test_video_event_index_coordinator import EventStore
from tests.server.test_video_parse_index_coordinator import _Embedder, _Store, _contract
from tests.server.test_video_parse_repository import _event_index_fixture


class PublishedEventStore(EventStore):
    """Only external search I/O is replaced; hits come from actually indexed documents."""

    semantic_dimensions = vision_dimensions = 2

    def __init__(self):
        super().__init__()
        self.search_sources = []

    def search_lexical(self, query, *, sources, filters, limit):
        self.search_sources.append(tuple(sources))
        hits = []
        for document in self.documents.values():
            source = (document.parse_task_id, document.video_chunk_id, document.version)
            if (source not in sources
                    or filters.event_types and document.event_type not in filters.event_types
                    or filters.speaker is not None and document.speaker != filters.speaker
                    or filters.video_chunk_ids and document.video_chunk_id not in filters.video_chunk_ids):
                continue
            matched = tuple(field for field in document.lexical_fields if query in field.raw_text)
            if matched:
                hits.append(RawEventHit(
                    channel="lexical", event_id=document.event_id,
                    parse_task_id=document.parse_task_id, video_chunk_id=document.video_chunk_id,
                    version=document.version, timeline_index=document.timeline_index,
                    event_type=document.event_type, speaker=document.speaker,
                    lexical_fields=document.lexical_fields, matched_fields=matched,
                    rank=len(hits) + 1, score=1.0,
                ))
        return tuple(hits[:limit])


class ImageEmbedder(_Embedder):
    def __init__(self):
        super().__init__()
        self.image_calls = 0

    def embed_image(self, path):
        assert Path(path).read_bytes().startswith(b"frame:")
        self.image_calls += 1
        return EmbeddingResult(model="embed-v1", modality="image", vector=[0., 1.])

    def embed_query(self, query):
        pytest.fail("lexical-only retrieval must not encode the query")


def test_real_sql_index_event_receipt_hydration_and_agent_evidence_pipeline(repository, tmp_path, monkeypatch):
    factory, _queue, repo, task_id, index_lease, at, video_lease = _event_index_fixture(repository)
    monkeypatch.setattr(persistence_module, "now_utc", lambda: at)
    chunk_contract = _contract()
    event_contract = EventIndexingContract(
        semantic_encoder=chunk_contract.semantic_encoder,
        vision_encoder=chunk_contract.vision_encoder,
        capture=EventFrameContract(max_height=720),
    )
    events, chunks, embedder = PublishedEventStore(), _Store(), ImageEmbedder()
    target = repo.bind_event_index_target(index_lease, {
        "index_name": events.index_name, "contract_id": event_contract.contract_id,
        "contract": event_contract.as_dict(),
    }, now=at)
    snapshot = repo.load_chunk_indexing_snapshot(index_lease, event_target=target, now=at)
    original = snapshot.chunk.content["timeline"][0]
    source_video = tmp_path / "source.mp4"
    source_video.write_bytes(b"source-video")
    temporary_frames = []

    def run(command):
        path = Path(command[-1])
        if "-frames:v" in command:
            path.write_bytes(("frame:" + command[command.index("-ss") + 1]).encode())
            temporary_frames.append(path)
        else:
            path.write_bytes(b"prepared-video")

    coordinator = VideoParseIndexCoordinator(
        repo, chunks, embedder, media_resolver=lambda _uri: source_video,
        media_service=VideoParseMediaService(tmp_path / "media", command_runner=run),
        contract=chunk_contract, event_store=events, event_target=target,
    )
    coordinator.index_chunk(index_lease)
    repo.aggregate_task(video_lease, now=at)

    with factory() as session:
        content = session.scalar(select(ChunkParseContent))
        child = session.get(ChunkParseTask, index_lease.chunk_parse_task_id)
        parent = session.get(VideoParseTask, task_id)
        receipt = content.detail[INDEX_EMBEDDINGS_DETAIL_KEY]["receipt"]
        assert child.status == parent.status == "COMPLETE"
        assert child.indexed_content_version == content.version == snapshot.chunk.content_version
        assert receipt["events"]["event_ids"] == list(events.documents)
        assert receipt["events"]["event_count"] == 1
        assert receipt["events"]["contract_id"] == target["contract_id"]
        assert content.content["timeline"][0] == original
    assert embedder.text_calls == 2 and embedder.video_calls == 1 and embedder.image_calls == 3
    assert all(not path.exists() for path in temporary_frames)

    service = VideoEventRetrievalService(repo, events, embedder, contract=event_contract,
        policy=RetrievalPolicy(lexical_weight=1., semantic_weight=0., vision_weight=0.,
                               channel_top_k=8, candidate_budget=8, top_k=1, rrf_k=60))
    found = service.search(original["description"], tenant_id=snapshot.chunk.tenant_id,
        series_ids=[snapshot.chunk.series_id], event_types=[original["event_type"]], top_k=1)
    expected_source = (task_id, snapshot.chunk.video_chunk_id, snapshot.chunk.content_version)
    assert events.search_sources == [(expected_source,)]
    assert len(found.matches) == len(found.chunk_result.hits) == 1
    match, hit = found.matches[0], found.chunk_result.hits[0]
    assert (match.parse_task_id, match.video_chunk_id, match.version) == expected_source
    assert match.timeline_index == 0 and match.event_type == original["event_type"]
    assert (match.start_ms, match.end_ms) == (original["start_seconds"], original["end_seconds"])
    assert match.matched_fields[0].raw_text == original["description"]
    assert found.trace["strategy_version"] == EVENT_RETRIEVAL_STRATEGY_VERSION
    assert found.trace["sources"] == [list(expected_source)]
    assert found.trace["raw_candidates"][0]["event_id"] == match.event_id
    assert found.trace["selected_event_ids"] == [match.event_id] and not found.trace["rejected"]
    assert hit.evidence.all_time_events[0] == original
    weights = LexicalRetrievalChannelWeights(lexical=1, semantic=0, visual=0)
    calls = [RetrievalCall(query=found.chunk_result.query, top_k=1,
                          requested_weights=weights, effective_weights=weights, result=found.chunk_result)]
    merged = merge_retrieval_calls({"tenant_id": snapshot.chunk.tenant_id,
                                    "series_ids": [snapshot.chunk.series_id]}, calls)
    assert merged.hits[0].evidence == hit.evidence
