from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from engine.parsev2.schemas import VideoParseResult
from server.api.app import build_app
from server.api.errors import register_exception_handlers
from server.config.auth import AuthConfig
from server.config.sse import SSEConfig
from server.error.auth import AuthError
from server.model.dto.authDTO import AuthIdentity
from server.model.dto.mediaDTO import ParseExportRecord, parse_export_event_types
from server.repository.video_parse import VideoParseConflictError, VideoParseTaskNotFoundError
from server.router.media import router
from server.service.media import MediaService


class _Forbidden:
    def __getattr__(self, name):
        raise AssertionError(f"export accessed a media/model dependency: {name}")


def _content():
    events = []
    for index, event_type in enumerate(parse_export_event_types()):
        event = {
            "event_type": event_type, "start_seconds": 61_000 + index * 1000,
            "end_seconds": 64_000 + index * 1000,
            "description": f"已有的 {event_type} 观察描述，不是逐字台词。",
            "participants": ["爱音", "灯"], "visible_persons": ["灯"],
        }
        if event_type == "DIALOGUE":
            event.update(speaker=None, text_variants=[
                {"language": "ja", "role": "ORIGINAL", "text": "一緒に行く？\n待ってる。"},
                {"language": "zh-Hans", "role": "TRANSLATION", "text": "一起去吗？\n我等你。"},
                {"language": "en", "role": "TRANSLATION", "text": 'Shall we go? "I will wait."'},
            ])
        if event_type == "ON_SCREEN_TEXT":
            event.update(language="ja", text="音楽室")
        events.append(event)
    return VideoParseResult.model_validate({"persons": ["爱音", "灯"], "timeline": events})


def _snapshot(content=None):
    return SimpleNamespace(
        video_id=str(uuid4()), parse_task_id=str(uuid4()), chunk_id=str(uuid4()),
        content_version=3, chunk_sequence_no=1, content=content or _content(),
    )


@pytest.fixture
def export_api():
    snapshot = _snapshot()
    repo = SimpleNamespace(load_export_snapshot=Mock(return_value=(snapshot,)))
    media = MediaService(_Forbidden(), _Forbidden(), video_parse_repository=repo)
    app = FastAPI()
    app.state.services = SimpleNamespace(media=media)
    app.include_router(router)
    register_exception_handlers(app)
    with TestClient(app) as client:
        yield client, repo, snapshot


def _request(snapshot, **extra):
    return {"tenantId": "workspace", "videoIds": [snapshot.video_id], **extra}


@pytest.mark.parametrize("event_types", [None, ["DIALOGUE"], ["ACTION", "DIALOGUE"], ["DIALOGUE", "DIALOGUE"]])
def test_export_preserves_complete_events_contract_and_original_positions(export_api, event_types):
    client, repo, snapshot = export_api
    before = deepcopy(snapshot.content.model_dump())
    body = _request(snapshot, **({"eventTypes": event_types} if event_types is not None else {}))
    response = client.post("/api/videos/parse/export", json=body)
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("application/x-ndjson")
    assert response.headers["content-disposition"] == 'attachment; filename="parse-export.jsonl"'
    assert response.headers["cache-control"] == "private, no-store"
    lines = response.content.splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    ParseExportRecord.model_validate(record)
    wanted = set(event_types or parse_export_event_types())
    indices = [i for i, event in enumerate(before["timeline"]) if event["event_type"] in wanted]
    assert record["timeline"] == [before["timeline"][i] for i in indices]
    assert record["persons"] == before["persons"]
    assert record["schema_version"] == "anivag.parse-export.v1"
    assert record["source"] == {
        "video_id": snapshot.video_id, "parse_task_id": snapshot.parse_task_id,
        "chunk_id": snapshot.chunk_id, "content_version": 3, "timeline_indices": indices,
    }
    assert record["time"] == {"unit": "ms", "origin": "source_video"}
    assert record["timeline"][0]["start_seconds"] >= 61_000
    assert json.loads(response.headers["x-export-summary"]) == {
        "record_count": 1, "event_count": len(indices), "skipped": {"no_matching_events": 0},
    }
    assert "messages" not in record and "text" not in record
    assert snapshot.content.model_dump() == before
    repo.load_export_snapshot.assert_called_once_with(
        tenant_id="workspace", video_ids=[snapshot.video_id], video_parse_task_ids=(),
    )


def test_null_speaker_and_language_variants_remain_one_utterance(export_api):
    client, _, snapshot = export_api
    response = client.post("/api/videos/parse/export", json=_request(snapshot, eventTypes=["DIALOGUE"]))
    events = response.json()["timeline"]
    assert len(events) == 1
    assert events[0]["speaker"] is None
    assert [v["language"] for v in events[0]["text_variants"]] == ["ja", "zh-Hans", "en"]
    assert [v["role"] for v in events[0]["text_variants"]] == ["ORIGINAL", "TRANSLATION", "TRANSLATION"]
    assert events[0]["text_variants"][1]["text"] == "一起去吗？\n我等你。"


def test_empty_chunks_are_reported_separately_and_all_empty_is_empty_file(export_api):
    client, repo, snapshot = export_api
    empty = _snapshot(VideoParseResult(persons=[], timeline=[]))
    repo.load_export_snapshot.return_value = (empty, snapshot)
    response = client.post("/api/videos/parse/export", json=_request(snapshot))
    assert len(response.content.splitlines()) == 1
    assert json.loads(response.headers["x-export-summary"])["skipped"] == {"no_matching_events": 1}
    repo.load_export_snapshot.return_value = (empty,)
    response = client.post("/api/videos/parse/export", json=_request(snapshot, eventTypes=["DIALOGUE"]))
    assert response.status_code == 200 and response.content == b""
    assert json.loads(response.headers["x-export-summary"]) == {
        "record_count": 0, "event_count": 0, "skipped": {"no_matching_events": 1},
    }


@pytest.mark.parametrize("body,status", [
    ({"videoIds": ["video"], "eventTypes": []}, 422),
    ({"videoIds": ["video"], "eventTypes": ["SHOT"]}, 400),
    ({"videoIds": ["video"], "eventTypes": ["dialogue"]}, 400),
    ({"videoIds": []}, 422),
    ({}, 422),
    ({"videoIds": ["video"], "videoParseTaskIds": ["task"]}, 422),
])
def test_invalid_request_never_reads_sources(export_api, body, status):
    client, repo, _ = export_api
    response = client.post("/api/videos/parse/export", json={"tenantId": "workspace", **body})
    assert response.status_code == status, response.text
    repo.load_export_snapshot.assert_not_called()


def test_explicit_task_selector_and_options_use_current_formal_types(export_api):
    client, repo, snapshot = export_api
    response = client.post("/api/videos/parse/export", json={
        "tenantId": "workspace", "videoParseTaskIds": [snapshot.parse_task_id],
    })
    assert response.status_code == 200
    repo.load_export_snapshot.assert_called_once_with(
        tenant_id="workspace", video_ids=(), video_parse_task_ids=[snapshot.parse_task_id],
    )
    options = client.get("/api/videos/parse/export/options", params={"tenantId": "workspace"})
    assert options.json() == {"eventTypes": list(parse_export_event_types())}


@pytest.mark.parametrize("error,status,code", [
    (VideoParseTaskNotFoundError("private source"), 404, "NOT_FOUND"),
    (VideoParseConflictError("formal content is incomplete"), 409, "PARSE_CONTENT_NOT_READY"),
])
def test_source_errors_are_explicit_and_do_not_return_partial_files(export_api, error, status, code):
    client, repo, snapshot = export_api
    repo.load_export_snapshot.side_effect = error
    response = client.post("/api/videos/parse/export", json=_request(snapshot))
    assert response.status_code == status and response.json()["code"] == code
    assert "content-disposition" not in response.headers
    if status == 404:
        assert "private source" not in response.text
    else:
        assert response.json()["message"] == str(error)


def test_uncertain_non_dialogue_person_references_follow_the_formal_contract(export_api):
    client, _, snapshot = export_api
    action = next(event for event in snapshot.content.timeline if event.event_type == "ACTION")
    action.participants.append("未登记人物")
    response = client.post("/api/videos/parse/export", json=_request(snapshot))
    assert response.status_code == 200
    record = response.json()
    exported = next(event for event in record["timeline"] if event["event_type"] == "ACTION")
    assert exported["participants"] == action.participants
    assert record["persons"] == snapshot.content.persons
    assert client.post("/api/videos/parse/export", json=_request(snapshot, eventTypes=["DIALOGUE"])).status_code == 200


def test_cross_chunk_events_and_equal_start_order_are_not_rewritten(export_api):
    client, _, snapshot = export_api
    events = snapshot.content.timeline
    events[1].start_seconds = events[0].start_seconds
    events[0].end_seconds = 80_000
    response = client.post("/api/videos/parse/export", json=_request(snapshot))
    record = response.json()
    assert response.status_code == 200
    assert record["timeline"][0]["end_seconds"] == 80_000
    assert record["timeline"] == snapshot.content.model_dump()["timeline"]
    assert record["source"]["timeline_indices"] == list(range(len(events)))


class _Auth:
    config = SimpleNamespace(cookie_name="session")
    identity = AuthIdentity(user_id="owner", email="owner@example.test", is_guest=False,
                            tenant_id="workspace", public_tenant_id="public", role="user",
                            auth_version=1, auth_level="email", mode="cloud")

    def authenticate(self, token):
        if token != "test-token":
            raise AuthError("登录已失效", status_code=401, code="AUTH_REQUIRED")
        return self.identity

    def verify_csrf(self, token, csrf):
        if csrf != "test-csrf":
            raise AuthError("请求校验失败", status_code=403, code="CSRF_FAILED")


@pytest.mark.parametrize("tenant,guest,csrf,status", [
    ("other", False, "test-csrf", 404), ("public", False, "test-csrf", 404),
    ("workspace", True, "test-csrf", 403), ("workspace", False, "wrong", 403),
    ("workspace", False, "test-csrf", 200),
])
def test_export_checks_identity_scope_and_csrf_before_reading(export_api, tenant, guest, csrf, status):
    client, repo, snapshot = export_api
    auth = _Auth()
    auth.identity = replace(auth.identity, is_guest=guest, auth_level="guest" if guest else "email")
    client.app.state.services.auth = auth
    client.cookies.set("session", "test-token")
    response = client.post("/api/videos/parse/export", json=_request(snapshot, tenantId=tenant),
                           headers={"X-CSRF-Token": csrf})
    assert response.status_code == status, response.text
    if status != 200:
        repo.load_export_snapshot.assert_not_called()
    else:
        repo.load_export_snapshot.assert_called_once()


def test_remote_download_exposes_filename_and_summary(export_api):
    client, _, snapshot = export_api
    settings = SimpleNamespace(auth=AuthConfig(), sse=SSEConfig(), allowed_origins=("https://ui.example.test",))
    app = build_app(client.app.state.services, settings=settings)
    with TestClient(app) as remote:
        response = remote.post("/api/videos/parse/export", json=_request(snapshot),
                               headers={"Origin": "https://ui.example.test"})
    assert response.status_code == 200
    exposed = response.headers["access-control-expose-headers"]
    assert "Content-Disposition" in exposed and "X-Export-Summary" in exposed
    assert response.headers["access-control-allow-origin"] == "https://ui.example.test"


def test_export_without_parsev2_is_explicitly_unavailable():
    service = MediaService(_Forbidden(), _Forbidden())
    with pytest.raises(VideoParseConflictError, match="execution mode"):
        service.export_parse_results(tenant_id="workspace", video_ids=[str(uuid4())])
