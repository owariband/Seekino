import base64
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from server.model.dto.mediaDTO import PlaybackMedia
from server.service.media import MediaMissingError
from server.service.mcp_retrieval import McpRetrievalService
from server.service.video_parse_errors import VideoParseConflictError
from tests.server.test_video_event_retrieval import _fixture


def _service(events=None):
    events_service, persistence, store, embedder, inputs = _fixture(events=events)
    evidence = persistence.evidence
    persistence.evidence = evidence.model_copy(update={
        "parse_provenance": {
            "parseTaskId": next(iter(persistence.scope.parse_tasks_by_video_id.values())),
            "contentVersion": 1, "indexGeneration": persistence.scope.index_generation,
        },
        "all_time_events": [event.model_dump(mode="json") for event in persistence.result.timeline],
    })
    states = [SimpleNamespace(
        series_id=inputs["series_ids"][0], name="Test Series", display_name=None,
        status="READY", video_count=1, ready_video_count=1,
    )]
    library = Mock()
    library.list_series.side_effect = lambda **kwargs: states[kwargs.get("offset", 0):
        kwargs.get("offset", 0) + kwargs.get("limit", 20)]
    media = Mock()
    media.playback_view.return_value = PlaybackMedia("READY", "/api/videos/content?variant=playback")
    service = McpRetrievalService(
        library=library, media=media, event_retrieval=events_service,
        parse_persistence=persistence, local_tenant_id=evidence.tenant_id,
    )
    return service, persistence, library, media, store, embedder, inputs


def _alter(ref, **updates):
    value = json.loads(base64.urlsafe_b64decode(ref))
    value.update(updates)
    return base64.urlsafe_b64encode(json.dumps(value).encode()).decode()


def test_retrieve_uses_formal_events_fixed_tenant_and_millisecond_evidence():
    service, persistence, library, media, _, _, inputs = _service()
    result = service.retrieve("蓝色杯子")
    assert result["top_k_unit"] == "distinct_chunks"
    assert result["matched_chunk_count"] == result["returned_event_count"] == 1
    item = result["items"][0]
    assert (item["start_ms"], item["end_ms"]) == (1, 4)
    assert item["event"]["start_ms"] == 1
    assert "seconds" not in json.dumps(result)
    library.list_series.assert_called_once_with(tenant_id=inputs["tenant_id"], status="READY", limit=51)
    read = service.read_evidence(item["evidence_ref"])
    assert read["matched_event"]["description"] == "角色拿起蓝色杯子"
    assert read["content_version"] == 1
    assert read["index_generation"] == persistence.scope.index_generation
    assert "variant=original" in read["media"]["original_url"]
    assert "artifact://" not in json.dumps(read)
    assert "original_source_uri" not in json.dumps(read)
    assert "seconds" not in json.dumps(read)
    assert media.get_original.call_args.kwargs["tenant_id"] == inputs["tenant_id"]


def test_event_unavailability_is_error_and_does_not_claim_no_match():
    service, _, _, _, store, embedder, _ = _service()
    store.ready = False
    with pytest.raises(ValueError, match="EVENT_INDEX_NOT_READY"):
        service.retrieve("杯子")
    assert embedder.calls == []
    service._events = None
    with pytest.raises(ValueError, match="EVENT_INDEX_NOT_READY"):
        service.retrieve("杯子")


def test_stale_reference_rehydrates_before_media_access():
    service, persistence, _, media, _, _, _ = _service()
    ref = service.retrieve("杯子")["items"][0]["evidence_ref"]
    persistence.hydrate_retrieval_candidates = Mock(side_effect=VideoParseConflictError("version changed"))
    with pytest.raises(ValueError, match="EVIDENCE_STALE"):
        service.read_evidence(ref)
    media.get_original.assert_not_called()
    requested = persistence.hydrate_retrieval_candidates.call_args.args[0][0]
    assert requested[1].content_version == 1
    assert requested[1].index_generation == persistence.scope.index_generation


def test_reference_cannot_switch_tenant_or_forge_event_identity():
    service, persistence, _, media, _, _, _ = _service()
    ref = service.retrieve("杯子")["items"][0]["evidence_ref"]
    payload = json.loads(base64.urlsafe_b64decode(ref))
    payload["identity"]["tenant_id"] = "other-tenant"
    call_count = len(persistence.hydrated)
    with pytest.raises(ValueError, match="EVIDENCE_SCOPE_MISMATCH"):
        service.read_evidence(_alter(ref, identity=payload["identity"]))
    assert len(persistence.hydrated) == call_count
    with pytest.raises(ValueError, match="INVALID_EVIDENCE_REF"):
        service.read_evidence(_alter(ref, event_id="a" * 64))
    media.get_original.assert_not_called()


@pytest.mark.parametrize("ref", ["", "bad!", "x" * 4097, "e30=", 1, None])
def test_invalid_reference_is_rejected(ref):
    service, _, _, _, _, _, _ = _service()
    with pytest.raises(ValueError, match="INVALID_EVIDENCE_REF"):
        service.read_evidence(ref)


def test_context_pagination_keeps_matched_event_and_version():
    events = [{"event_type": "ACTION", "start_seconds": 1, "end_seconds": 4,
               "description": f"动作 {index}", "participants": [], "visible_persons": []}
              for index in range(11)]
    service, _, _, _, _, _, _ = _service(events)
    result = service.retrieve("动作", top_k=1)
    assert result["matched_chunk_count"] == 1
    assert result["matched_event_count"] == 11
    read = service.read_evidence(result["items"][-1]["evidence_ref"])
    assert read["matched_event"]["description"] == "动作 10"
    assert len(read["context_events"]) == 8
    next_read = service.read_evidence(read["next_evidence_ref"])
    assert [event["timeline_index"] for event in next_read["context_events"]] == [8, 9, 10]
    assert next_read["next_evidence_ref"] is None
    assert next_read["matched_event"] == read["matched_event"]


def test_truncation_is_explicit_and_result_does_not_expose_unbounded_source():
    events = [{"event_type": "ACTION", "start_seconds": 1, "end_seconds": 4,
               "description": "字" * 40_000, "participants": [], "visible_persons": []}]
    service, _, _, _, _, _, _ = _service(events)
    result = service.retrieve("动作")
    assert result["truncated"] and result["warnings"]
    assert result["items"][0]["event"]["event_type"] == "ACTION"
    read = service.read_evidence(result["items"][0]["evidence_ref"])
    assert read["truncated"] and read["warnings"]
    assert len(json.dumps(read, ensure_ascii=False).encode()) < 48_000


def test_missing_media_preserves_readable_source_and_reports_unavailability():
    service, _, _, media, _, _, _ = _service()
    ref = service.retrieve("杯子")["items"][0]["evidence_ref"]
    media.get_original.side_effect = MediaMissingError("not present")
    media.playback_view.return_value = PlaybackMedia("MISSING", None)
    result = service.read_evidence(ref)
    assert result["matched_event"]["description"]
    assert result["media"] == {"original_url": None, "playback_url": None, "playback_status": "MISSING"}
    assert result["warnings"]


def test_series_pagination_and_bounded_default_scope():
    service, _, library, _, _, _, _ = _service()
    state = library.list_series()[0]
    library.list_series.side_effect = None
    library.list_series.return_value = [state] * 21
    result = service.list_series()
    assert len(result["items"]) == result["next_offset"] == 20
    library.list_series.return_value = [state] * 51
    with pytest.raises(ValueError, match="SERIES_SELECTION_REQUIRED"):
        service.retrieve("杯子")
    library.list_series.return_value = []
    with pytest.raises(ValueError, match="NO_SEARCHABLE_SERIES"):
        service.retrieve("杯子")


@pytest.mark.parametrize("kwargs", [
    {"top_k": True}, {"top_k": 21}, {"series_ids": []}, {"series_ids": "abc"},
    {"speaker": ""}, {"event_types": "ACTION"}, {"query": " "},
])
def test_invalid_search_arguments_fail_before_retrieval(kwargs):
    service, _, _, _, store, embedder, _ = _service()
    with pytest.raises(ValueError, match="INVALID_ARGUMENT"):
        service.retrieve(**{"query": "杯子", **kwargs})
    assert store.calls == [] and embedder.calls == []


def test_sqlite_source_versions_and_real_library_media_roundtrip(repository, parsed_video, tmp_path):
    from sqlalchemy import select

    from engine.rag.contracts import (
        AgentReply, SelectionReason, VideoChunkCandidate, VideoChunkSearchHit, VideoChunkSearchResult,
    )
    from engine.rag.indexing.events import compute_event_id
    from engine.rag.retrieval.contracts import EventSearchResult, FusedEventHit
    from server.database import create_session_factory
    from server.model.job import ChunkParseContent, ChunkParseTask
    from server.model.video import Video
    from server.repository.library import LibraryRepository
    from server.service.library import LibraryService
    from server.service.media import MediaService
    from server.storage.artifact import LocalArtifactStore

    engine, persistence = repository
    factory = create_session_factory(engine)
    store = LocalArtifactStore(tmp_path / "managed-mcp", environment="test")
    source = store.base_directory / "source.mp4"
    source.write_bytes(b"isolated managed media fixture")
    with factory.begin() as session:
        session.get(Video, parsed_video.video_id).source_uri = store.uri_for_path(source)
    library = LibraryService(LibraryRepository(factory), store)
    # Only codec probing and ES retrieval are stubbed; SQL hydration and media
    # resolution use the real services and an isolated temporary artifact store.
    media = MediaService(library, store, parse_persistence=persistence, probe_playback=lambda _path: True)
    chunk_id = parsed_video.video_chunk_ids[0]
    evidence = persistence.get_chunk_evidence(chunk_id, tenant_id=parsed_video.tenant_id)
    assert evidence.parse_provenance == {
        "parseTaskId": parsed_video.video_parse_task_id, "contentVersion": 1, "indexGeneration": 1,
    }
    match = FusedEventHit(
        event_id=compute_event_id(parsed_video.video_parse_task_id, chunk_id, 1, 0),
        parse_task_id=parsed_video.video_parse_task_id, video_chunk_id=chunk_id,
        version=1, timeline_index=0, event_type="ACTION", fused_rank=1, fused_score=1.0,
        channel_ranks={"lexical": 1}, channel_scores={"lexical": 1.0}, start_ms=1000, end_ms=3000,
    )
    found = EventSearchResult(chunk_result=VideoChunkSearchResult(
        query="动作", series_ids=[parsed_video.series_id], searched_video_ids=[parsed_video.video_id],
        hits=[VideoChunkSearchHit(candidate=VideoChunkCandidate(video_chunk_id=chunk_id, fused_rank=1, fused_score=1.0),
                                  selection_reason=SelectionReason(summary="第 1 段动作", matched_evidence=[], limitations=[]), evidence=evidence)],
        agent_reply=AgentReply(message="找到片段", result_count=1),
    ), matches=[match], trace={})
    service = McpRetrievalService(
        library=library, media=media, event_retrieval=Mock(search=Mock(return_value=found)),
        parse_persistence=persistence, local_tenant_id=parsed_video.tenant_id,
    )
    assert service.list_series()["items"][0]["series_id"] == parsed_video.series_id
    ref = service.retrieve("动作", series_ids=[parsed_video.series_id])["items"][0]["evidence_ref"]
    read = service.read_evidence(ref)
    assert read["matched_event"]["start_ms"] == 1000
    assert read["chunk_end_ms"] == 10_000 and read["content_version"] == 1
    assert read["media"]["playback_status"] == "READY"
    assert "variant=original" in read["media"]["original_url"]
    assert str(tmp_path) not in json.dumps(read)
    assert len(json.dumps(read, ensure_ascii=False).encode()) < 48_000
    assert media.get_original(parsed_video.video_id, tenant_id=parsed_video.tenant_id).path.read_bytes() == source.read_bytes()

    with factory.begin() as session:
        child = session.scalar(select(ChunkParseTask).where(ChunkParseTask.chunk_id == chunk_id))
        content = session.scalar(select(ChunkParseContent).where(ChunkParseContent.chunk_task_id == child.id))
        content.version = child.indexed_content_version = 2
    assert persistence.get_chunk_evidence(chunk_id, tenant_id=parsed_video.tenant_id).parse_provenance["contentVersion"] == 2
    with pytest.raises(ValueError, match="EVIDENCE_STALE"):
        service.read_evidence(ref)
