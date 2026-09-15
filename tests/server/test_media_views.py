from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.api.errors import register_exception_handlers
from server.api.search import search_result_view
from server.repository.library import SeriesNotFoundError
from server.router.media import router as media_router
from server.router.search import router as search_router
from server.schema import AgentReply, SelectionReason, VideoChunkCandidate, VideoChunkSearchHit, VideoChunkSearchResult
from server.service.media import MediaService
from server.storage.artifact import LocalArtifactStore


def media_fixture(tmp_path, *, playable=True):
    video_id = str(uuid4())
    store = LocalArtifactStore(tmp_path / "managed", environment="test")
    source = store.base_directory / "source.mp4"
    source.write_bytes(b"managed-original-video")
    frame = store.base_directory / "frame.png"
    frame.write_bytes(b"\x89PNG\r\n\x1a\nregistered-image")
    state = SimpleNamespace(video_id=video_id, workspace_id="workspace", original_source_uri=store.uri_for_path(source), keyframe_uris=(store.uri_for_path(frame),))
    def get_video(identifier, *, workspace_id):
        if identifier != video_id or workspace_id != "workspace":
            raise SeriesNotFoundError("视频资源不存在")
        return state
    library = Mock(get_video=Mock(side_effect=get_video))
    probe = Mock(return_value=playable)
    service = MediaService(library, store, probe_playback=probe)
    app = FastAPI()
    app.state.services = SimpleNamespace(media=service)
    app.include_router(media_router)
    register_exception_handlers(app)
    return service, TestClient(app), source, frame, state, probe


def test_playback_requires_real_managed_file_and_confirmed_codec(tmp_path):
    service, client, source, _frame, state, probe = media_fixture(tmp_path)
    playback = service.playback_view(state.video_id, workspace_id="workspace", video=state)
    assert playback.status == "READY"
    response = client.get(playback.url)
    assert response.status_code == 200
    assert response.content == source.read_bytes()
    probe.return_value = False
    assert service.playback_view(state.video_id, workspace_id="workspace").status == "FAILED"
    assert client.get(playback.url).status_code == 409
    original_url = playback.url.replace("variant=playback", "variant=original")
    assert client.get(original_url).content == source.read_bytes()
    source.unlink()
    assert service.playback_view(state.video_id, workspace_id="workspace").status == "MISSING"
    response = client.get(playback.url)
    assert response.status_code == 410
    assert response.json()["code"] == "MEDIA_MISSING"


def test_image_http_reads_registered_bytes_and_is_workspace_scoped(tmp_path):
    service, client, _source, frame, state, _probe = media_fixture(tmp_path)
    image = service.thumbnail_view(state.video_id, workspace_id="workspace", video=state)
    assert image is not None
    response = client.get(image.url)
    assert response.status_code == 200
    assert response.content == frame.read_bytes()
    assert response.headers["content-type"] == "image/png"
    assert client.get(image.url.replace("workspaceId=workspace", "workspaceId=other")).status_code == 404
    assert client.get("/api/media/images/content", params={"workspaceId": "workspace", "imageId": "artifact://private/frame.png"}).status_code == 404
    frame.unlink()
    assert service.thumbnail_view(state.video_id, workspace_id="workspace", video=state) is None
    assert client.get(image.url).status_code == 410


def test_media_requires_workspace_and_rejects_unmanaged_sources(tmp_path):
    service, client, source, _frame, state, _probe = media_fixture(tmp_path)
    assert client.get("/api/videos/content", params={"videoId": state.video_id}).status_code == 422
    state.original_source_uri = source.as_uri()
    assert service.playback_view(state.video_id, workspace_id="workspace", video=state).status == "FAILED"


def domain_result(repository, prepared_import):
    _engine, repo = repository
    report = repo.import_video_parse(prepared_import)
    evidence = repo.hydrate_video_chunk_states(report.video_chunk_ids)[0].evidence
    hit = VideoChunkSearchHit(candidate=VideoChunkCandidate(video_chunk_id=evidence.video_chunk_id, fused_rank=7, fused_score=0.1), selection_reason=SelectionReason(summary="真实证据", matched_evidence=[], limitations=[]), evidence=evidence)
    return VideoChunkSearchResult(query="action", series_ids=[evidence.series_id], searched_video_ids=[evidence.video_id, str(uuid4())], agent_reply=AgentReply(message="结果", result_count=1), hits=[hit])


def test_search_view_preserves_actual_scope_rank_and_reports_unknown_frames(repository, prepared_import, tmp_path):
    raw = domain_result(repository, prepared_import)
    service, _client, _source, _frame, _state, _probe = media_fixture(tmp_path)
    view = search_result_view(raw, service, workspace_id=prepared_import.workspace_id)
    payload = view.model_dump(mode="json", by_alias=True)
    assert payload["searchedVideoIds"] == raw.searched_video_ids
    assert payload["hits"][0]["candidate"]["rank"] == 1
    assert payload["hits"][0]["candidate"]["fusedRank"] == 7
    evidence = payload["hits"][0]["evidence"]
    assert evidence["workspaceId"] == prepared_import.workspace_id
    assert evidence["seriesId"] == prepared_import.series_id
    assert isinstance(evidence["parsedAt"], int)
    assert evidence["playbackStatus"] == "FAILED"
    assert evidence["playbackUrl"] is None
    assert evidence["thumbnail"] is None and evidence["keyframes"] == []
    assert "originalSourceUri" not in evidence and "keyframeUris" not in evidence
    assert "retrievalText" not in evidence and "contentHash" not in evidence
    assert payload["partial"] and payload["warnings"]
    old = raw.model_copy(update={"searched_video_ids": None})
    old_view = search_result_view(old, service, workspace_id=prepared_import.workspace_id)
    assert old_view.searched_video_ids == []
    assert "原始记录缺少检索视频范围" in old_view.warnings
    with pytest.raises(SeriesNotFoundError):
        search_result_view(raw, service, workspace_id="foreign")


def test_nonempty_search_route_returns_public_projection(repository, prepared_import, tmp_path):
    raw = domain_result(repository, prepared_import)
    service, _client, _source, _frame, _state, _probe = media_fixture(tmp_path)
    app = FastAPI()
    app.state.services = SimpleNamespace(media=service, retrieval=Mock(search=Mock(return_value=raw)))
    app.include_router(search_router)
    client = TestClient(app)
    response = client.post("/api/search", json={"workspaceId": prepared_import.workspace_id, "seriesIds": raw.series_ids, "query": "action"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["searchedVideoIds"] == raw.searched_video_ids
    assert payload["hits"][0]["candidate"]["rank"] == 1
    assert "originalSourceUri" not in payload["hits"][0]["evidence"]
