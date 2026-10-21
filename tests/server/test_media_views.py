from __future__ import annotations

from io import BytesIO
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from server.api.errors import register_exception_handlers
from server.api.search import evidence_view, search_result_view
from server.config.media import ImageUploadConfig
from server.repository.library import SeriesNotFoundError
from server.service.video_parse_persistence import VideoParseConflictError
from server.router.media import router as media_router
from server.router.search import router as search_router
from server.schema import (
    AgentReply,
    SelectionReason,
    VideoChunkCandidate,
    VideoChunkKeyframeReference,
    VideoChunkSearchHit,
    VideoChunkSearchResult,
)
from server.service.media import MediaService
from server.storage.artifact import LocalArtifactStore


def media_fixture(tmp_path, *, playable=True):
    video_id = str(uuid4())
    store = LocalArtifactStore(tmp_path / "managed", environment="test")
    source = store.base_directory / "source.mp4"
    source.write_bytes(b"managed-original-video")
    frame = store.base_directory / "frame.png"
    frame.write_bytes(b"\x89PNG\r\n\x1a\nregistered-image")
    state = SimpleNamespace(video_id=video_id, tenant_id="workspace", original_source_uri=store.uri_for_path(source), keyframe_uris=(store.uri_for_path(frame),))
    def get_video(identifier, *, tenant_id):
        if identifier != video_id or tenant_id != "workspace":
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
    playback = service.playback_view(state.video_id, tenant_id="workspace", video=state)
    assert playback.status == "READY"
    response = client.get(playback.url)
    assert response.status_code == 200
    assert response.content == source.read_bytes()
    probe.return_value = False
    assert service.playback_view(state.video_id, tenant_id="workspace").status == "FAILED"
    assert client.get(playback.url).status_code == 409
    original_url = playback.url.replace("variant=playback", "variant=original")
    assert client.get(original_url).content == source.read_bytes()
    source.unlink()
    assert service.playback_view(state.video_id, tenant_id="workspace").status == "MISSING"
    response = client.get(playback.url)
    assert response.status_code == 410
    assert response.json()["code"] == "MEDIA_MISSING"


def test_image_http_reads_registered_bytes_and_is_workspace_scoped(tmp_path):
    service, client, _source, frame, state, _probe = media_fixture(tmp_path)
    image = service.thumbnail_view(state.video_id, tenant_id="workspace", video=state)
    assert image is not None
    response = client.get(image.url)
    assert response.status_code == 200
    assert response.content == frame.read_bytes()
    assert response.headers["content-type"] == "image/png"
    assert client.get(image.url.replace("tenantId=workspace", "tenantId=other")).status_code == 404
    assert client.get("/api/media/images/content", params={"tenantId": "workspace", "imageId": "artifact://private/frame.png"}).status_code == 404
    frame.unlink()
    assert service.thumbnail_view(state.video_id, tenant_id="workspace", video=state) is None
    assert client.get(image.url).status_code == 410


def test_media_requires_workspace_and_rejects_unmanaged_sources(tmp_path):
    service, client, source, _frame, state, _probe = media_fixture(tmp_path)
    assert client.get("/api/videos/content", params={"videoId": state.video_id}).status_code == 422
    state.original_source_uri = source.as_uri()
    assert service.playback_view(state.video_id, tenant_id="workspace", video=state).status == "FAILED"


def test_series_image_upload_reads_actual_bytes_and_enforces_series_scope(tmp_path):
    service, client, _source, _frame, _state, _probe = media_fixture(tmp_path)
    series_id = str(uuid4())

    def get_series(identifier, *, tenant_id):
        if identifier != series_id or tenant_id != "workspace":
            raise SeriesNotFoundError("Series 资源不存在")
        return SimpleNamespace(series_id=series_id)

    service._library.get_series.side_effect = get_series
    output = BytesIO()
    Image.new("RGB", (16, 12), "green").save(output, format="PNG")
    data = output.getvalue()
    image = service.upload_series_image(series_id, data, tenant_id="workspace")

    assert image.image_id.startswith("simg_")
    response = client.get(image.url)
    assert response.status_code == 200
    assert response.content == data
    assert response.headers["content-type"] == "image/png"
    assert client.get(image.url.replace("tenantId=workspace", "tenantId=other")).status_code == 404
    with pytest.raises(SeriesNotFoundError):
        service.upload_series_image(series_id, data, tenant_id="other")
    with pytest.raises(SeriesNotFoundError):
        service.upload_series_image(str(uuid4()), data, tenant_id="workspace")
    with pytest.raises(ValueError, match="corrupt"):
        service.upload_series_image(series_id, b"not an image", tenant_id="workspace")
    service.image_upload_config = ImageUploadConfig(maximum_upload_size_bytes=len(data) - 1)
    with pytest.raises(ValueError, match="maximum upload size"):
        service.upload_series_image(series_id, data, tenant_id="workspace")
    assert client.get("/api/media/images/content", params={
        "tenantId": "workspace", "imageId": "simg_invalid",
    }).status_code == 404
    service.get_image(image.image_id, tenant_id="workspace").path.unlink()
    assert client.get(image.url).status_code == 410


def domain_result(repository, prepared_import):
    _engine, repo = repository
    report = repo.import_video_parse(prepared_import)
    evidence = repo.hydrate_video_chunk_states(report.video_chunk_ids)[0].evidence
    hit = VideoChunkSearchHit(candidate=VideoChunkCandidate(video_chunk_id=evidence.video_chunk_id, fused_rank=7, fused_score=0.1), selection_reason=SelectionReason(summary="真实证据", matched_evidence=[], limitations=[]), evidence=evidence)
    return VideoChunkSearchResult(query="action", series_ids=[evidence.series_id], searched_video_ids=[evidence.video_id, str(uuid4())], agent_reply=AgentReply(message="结果", result_count=1), hits=[hit])


def test_search_view_preserves_actual_scope_rank_and_media_diagnostics(repository, prepared_import, tmp_path):
    raw = domain_result(repository, prepared_import)
    service, _client, _source, _frame, _state, _probe = media_fixture(tmp_path)
    view = search_result_view(raw, service, tenant_id=prepared_import.tenant_id)
    payload = view.model_dump(mode="json", by_alias=True)
    assert payload["searchedVideoIds"] == raw.searched_video_ids
    assert payload["hits"][0]["candidate"]["rank"] == 1
    assert payload["hits"][0]["candidate"]["fusedRank"] == 7
    evidence = payload["hits"][0]["evidence"]
    assert evidence["tenantId"] == prepared_import.tenant_id
    assert evidence["seriesId"] == prepared_import.series_id
    assert isinstance(evidence["parsedAt"], int)
    assert evidence["playbackStatus"] == "FAILED"
    assert evidence["playbackUrl"] is None
    assert evidence["thumbnail"] is None and evidence["keyframes"] == []
    assert "originalSourceUri" not in evidence and "keyframeUris" not in evidence
    assert "retrievalText" not in evidence and "contentHash" not in evidence
    assert payload["partial"] is False
    assert payload["warnings"]
    assert not any("已登记关键帧" in warning for warning in payload["warnings"])
    old = raw.model_copy(update={"searched_video_ids": None})
    old_view = search_result_view(old, service, tenant_id=prepared_import.tenant_id)
    assert old_view.searched_video_ids == []
    assert old_view.partial is True
    assert "原始记录缺少检索视频范围" in old_view.warnings
    with pytest.raises(SeriesNotFoundError):
        search_result_view(raw, service, tenant_id="foreign")


def test_public_search_and_chunk_projection_hide_original_filename(repository, prepared_import, tmp_path):
    raw = domain_result(repository, prepared_import)
    service, _client, _source, _frame, _state, _probe = media_fixture(tmp_path)
    original = raw.hits[0].evidence.original_filename
    assert original
    member = search_result_view(raw, service, tenant_id=prepared_import.tenant_id)
    public = search_result_view(raw, service, tenant_id=prepared_import.tenant_id, public_view=True)
    chunk, _ = evidence_view(raw.hits[0].evidence, service, tenant_id=prepared_import.tenant_id, public_view=True)
    assert member.hits[0].evidence.original_filename == original
    assert public.hits[0].evidence.original_filename is None
    assert chunk.original_filename is None
    assert public.hits[0].evidence.video_chunk_id == member.hits[0].evidence.video_chunk_id


@pytest.mark.parametrize("partial", [False, True])
def test_search_view_supports_video_preview_without_registered_frames(
    repository, prepared_import, tmp_path, partial,
):
    raw = domain_result(repository, prepared_import)
    service, _client, _source, _frame, state, _probe = media_fixture(tmp_path)
    hit = raw.hits[0]
    evidence = hit.evidence.model_copy(update={
        "original_source_uri": state.original_source_uri,
        "keyframe_uris": [],
    })
    raw = raw.model_copy(update={
        "hits": [hit.model_copy(update={"evidence": evidence})],
        "partial": partial,
        "warnings": ["部分视频尚不可检索"] if partial else [],
    })

    view = search_result_view(raw, service, tenant_id=prepared_import.tenant_id)

    assert view.hits[0].evidence.playback_status == "READY"
    assert view.hits[0].evidence.playback_url
    assert view.hits[0].evidence.thumbnail is None
    assert view.hits[0].evidence.keyframes == []
    assert view.partial is partial
    assert view.warnings == raw.warnings
    assert view.hits[0].selection_reason.limitations == []


def test_chunk_keyframe_projection_uses_absolute_time_and_scoped_image_id(
    repository,
    prepared_import,
    tmp_path,
):
    _engine, video_repository = repository
    video_repository.import_video_parse(prepared_import)
    evidence = video_repository.hydrate_video_chunks(
        [prepared_import.chunks[1].video_chunk_id]
    )[0]
    service, client, _source, frame, _state, _probe = media_fixture(tmp_path)
    evidence = evidence.model_copy(update={
        "keyframe_uris": [
            VideoChunkKeyframeReference(
                uri=service._artifact_store.uri_for_path(frame),
                at_seconds=evidence.start_seconds,
            )
        ]
    })
    service._video_repository = Mock(
        hydrate_video_chunk_states=Mock(
            return_value=[SimpleNamespace(evidence=evidence)]
        )
    )

    view, limitations = evidence_view(
        evidence,
        service,
        tenant_id=prepared_import.tenant_id,
    )

    assert view.thumbnail == view.keyframes[0]
    assert view.keyframes[0].at_seconds == 10
    assert "缺少带原视频时间位置的已登记关键帧" not in limitations
    response = client.get(view.keyframes[0].url)
    assert response.status_code == 200
    assert response.content == frame.read_bytes()

    relative_time = evidence.model_copy(update={
        "keyframe_uris": [
            VideoChunkKeyframeReference(
                uri=service._artifact_store.uri_for_path(frame),
                at_seconds=0,
            )
        ]
    })
    assert service.keyframe_views(
        relative_time,
        tenant_id=prepared_import.tenant_id,
    ) == ()

    historical_snapshot = evidence.model_copy(update={"keyframe_uris": []})
    refreshed = service.keyframe_views(
        historical_snapshot,
        tenant_id=prepared_import.tenant_id,
    )
    assert len(refreshed) == 1
    assert refreshed[0].at_seconds == evidence.start_seconds


def test_v2_media_reader_takes_priority_and_refreshes_historical_keyframes(
    repository, prepared_import, tmp_path,
):
    evidence = domain_result(repository, prepared_import).hits[0].evidence
    fixture, _client, _source, frame, _state, _probe = media_fixture(tmp_path)
    evidence = evidence.model_copy(update={"keyframe_uris": [
        VideoChunkKeyframeReference(
            uri=fixture._artifact_store.uri_for_path(frame), at_seconds=evidence.start_seconds,
        ),
    ]})
    legacy = Mock()
    v2 = Mock(get_chunk_evidence=Mock(return_value=evidence))
    service = MediaService(
        fixture._library, fixture._artifact_store, legacy, parse_persistence=v2,
    )
    assert service.get_chunk_evidence(
        evidence.video_chunk_id, tenant_id=evidence.tenant_id,
    ) == evidence
    legacy.hydrate_video_chunk_states.assert_not_called()
    service._video_repository = None
    historical = evidence.model_copy(update={"keyframe_uris": []})
    frames = service.keyframe_views(historical, tenant_id=evidence.tenant_id)
    assert len(frames) == 1
    assert frames[0].at_seconds == evidence.start_seconds
    v2.get_chunk_evidence.side_effect = VideoParseConflictError("newer task is not COMPLETE")
    assert service.keyframe_views(historical, tenant_id=evidence.tenant_id) == ()


def test_nonempty_search_route_returns_public_projection(repository, prepared_import, tmp_path):
    raw = domain_result(repository, prepared_import)
    service, _client, _source, _frame, _state, _probe = media_fixture(tmp_path)
    app = FastAPI()
    app.state.services = SimpleNamespace(
        media=service, retrieval=Mock(search=Mock(return_value=raw)),
        agent=Mock(message_options=Mock(return_value=SimpleNamespace(
            top_k=SimpleNamespace(default_value=10, minimum=1, maximum=100),
        ))),
    )
    app.include_router(search_router)
    client = TestClient(app)
    response = client.post("/api/search", json={"tenantId": prepared_import.tenant_id, "seriesIds": raw.series_ids, "query": "action"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["searchedVideoIds"] == raw.searched_video_ids
    assert payload["hits"][0]["candidate"]["rank"] == 1
    assert "originalSourceUri" not in payload["hits"][0]["evidence"]
