from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from server.api.app import create_app
from server.agent.runtime import AgentRuntime
from server.api.jobs import _job_event_fingerprint, _job_view
from server.bootstrap import ApplicationServices
from server.database import (
    DatabaseEnvironment,
    create_database_engine,
    create_schema,
    create_session_factory,
)
from server.model.job import ParseJobStage
from server.repository.agent import AgentRepository
from server.repository.job import ParseJobRepository
from server.repository.library import LibraryRepository
from server.repository.upload import UploadRepository
from server.schema import AgentReply, VideoChunkSearchResult
from server.service.agent_chat import AgentChatService
from server.service.job import ParseJobService
from server.service.library import LibraryService
from server.service.media import MediaService
from server.service.upload import UploadService, UploadServiceConfig
from server.storage.artifact import LocalArtifactStore


@dataclass
class _RetrievalStub:
    calls: list[tuple[str, list[str], int, str]]

    def search(self, query: str, *, workspace_id: str, series_ids, top_k: int):
        scope = list(series_ids)
        self.calls.append((query, scope, top_k, workspace_id))
        return VideoChunkSearchResult(
            query=query,
            series_ids=scope,
            agent_reply=AgentReply(
                message="这次没有找到符合当前检索范围的候选片段。",
                result_count=0,
            ),
            hits=[],
        )


@dataclass
class _AgentModelStub:
    model_name: str
    provider_name: str = "test"

    def run_query(self, _prompt, *, tools, result_type, max_model_calls, **_kwargs):
        tools[0].invoke({"query": "她为什么哭", "top_k": 5})
        return result_type.model_validate({
            "plan_context": "召回哭泣的片段并检查画面和对白。",
            "message": "没有找到可以核验的候选画面。",
            "selected_video_chunk_ids": [],
        })

    def stream_text(self, _inputs):
        yield "没有找到"
        yield "可以核验的候选画面。"

    def generate(self, _inputs, *, schema_name: str, result_type):
        assert schema_name == "anivag_answer_evaluation"
        return result_type.model_validate({
            "action": "complete", "advice": "回答与证据一致，可以交付。",
        })


def _client(tmp_path: Path):
    engine = create_database_engine(
        f"sqlite+pysqlite:///{tmp_path / 'api.sqlite'}",
        environment=DatabaseEnvironment.TEST,
    )
    create_schema(engine)
    session_factory = create_session_factory(engine)
    artifact_store = LocalArtifactStore(
        tmp_path / "artifacts",
        environment=DatabaseEnvironment.TEST,
    )
    library = LibraryService(LibraryRepository(session_factory))
    job_repository = ParseJobRepository(session_factory)
    retrieval = _RetrievalStub([])
    agent = AgentChatService(
        AgentRepository(session_factory),
        library,
        AgentRuntime(
            _AgentModelStub("query-model-v1"),
            _AgentModelStub("evaluator-model-v1"),
            _AgentModelStub("chat-model-v1"),
            retrieval.search,
        ),
    )
    services = ApplicationServices(
        library=library,
        uploads=UploadService(
            UploadRepository(session_factory),
            artifact_store,
            config=UploadServiceConfig(
                preferred_segment_size_bytes=4,
                maximum_upload_size_bytes=1024,
                session_ttl=timedelta(hours=1),
                completion_stale_after=timedelta(minutes=5),
                pipeline_version="pipeline-v1",
                max_job_attempts=3,
            ),
            probe_duration=lambda _path: 12_345,
        ),
        jobs=ParseJobService(job_repository, library),
        media=MediaService(library, artifact_store),
        retrieval=retrieval,  # type: ignore[arg-type]
        agent=agent,
        index_sync=object(),  # type: ignore[arg-type]
    )
    return TestClient(create_app(services)), retrieval, job_repository


def _create_series(client: TestClient) -> str:
    response = client.post(
        "/api/series/create",
        json={
            "workspaceId": "local-default",
            "name": "Test Series",
            "description": "API fixture",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["seriesId"]


def test_api_paths_name_create_list_get_and_upload_actions(tmp_path: Path) -> None:
    client, _retrieval, _jobs = _client(tmp_path)
    openapi_paths = client.app.openapi()["paths"]
    paths = set(openapi_paths)

    assert {
        "/healthz",
        "/api/series/create",
        "/api/series/list",
        "/api/series/get",
        "/api/uploads/create",
        "/api/uploads/get",
        "/api/uploads/segments/upload",
        "/api/uploads/complete",
        "/api/jobs/list",
        "/api/jobs/get",
        "/api/jobs/events",
        "/api/videos/content",
        "/api/search",
        "/api/agent/sessions/create",
        "/api/agent/sessions/list",
        "/api/agent/sessions/get",
        "/api/agent/conversations/create",
        "/api/agent/conversations/list",
        "/api/agent/conversations/get",
        "/api/agent/messages/list",
        "/api/agent/messages/send",
        "/api/agent/messages/events",
    } == paths
    assert {
        "/api/series",
        "/api/series/{series_id}",
        "/api/series/{series_id}/uploads",
        "/api/series/{series_id}/uploads/create",
        "/api/uploads/{upload_session_id}",
        "/api/uploads/{upload_session_id}/segments/{segment_number}",
        "/api/uploads/{upload_session_id}/segments/{segment_number}/upload",
        "/api/uploads/{upload_session_id}/complete",
        "/api/jobs",
        "/api/jobs/{job_id}",
        "/api/jobs/get/{job_id}",
        "/api/jobs/{job_id}/events",
        "/api/videos/{video_id}/content",
    }.isdisjoint(paths)
    assert all("{" not in path and "}" not in path for path in paths)
    assert all(
        route.endpoint.__module__.startswith("server.api.")
        for route in client.app.routes
        if isinstance(route, APIRoute)
    )

    query_identifiers = {
        ("/api/series/get", "get"): {"seriesId"},
        ("/api/uploads/create", "post"): {"seriesId"},
        ("/api/uploads/get", "get"): {"uploadSessionId"},
        ("/api/uploads/segments/upload", "put"): {
            "uploadSessionId",
            "segmentNumber",
        },
        ("/api/uploads/complete", "post"): {"uploadSessionId"},
        ("/api/jobs/get", "get"): {"jobId"},
        ("/api/jobs/events", "get"): {"jobId"},
        ("/api/videos/content", "get"): {"videoId"},
        ("/api/agent/sessions/get", "get"): {"sessionId"},
        ("/api/agent/conversations/create", "post"): {"sessionId"},
        ("/api/agent/conversations/list", "get"): {"sessionId"},
        ("/api/agent/conversations/get", "get"): {"conversationId"},
        ("/api/agent/messages/list", "get"): {"conversationId"},
        ("/api/agent/messages/send", "post"): {"conversationId"},
        ("/api/agent/messages/events", "get"): {"conversationId"},
    }
    for (path, method), identifiers in query_identifiers.items():
        parameters = openapi_paths[path][method]["parameters"]
        actual = {
            parameter["name"]
            for parameter in parameters
            if parameter["in"] == "query" and parameter["required"]
        }
        assert identifiers <= actual

    schemas = client.app.openapi()["components"]["schemas"]
    assert {
        "seriesName",
        "episodeOrder",
        "originalFilename",
    } <= set(schemas["ParseJobView"]["required"])
    assert "agentReply" in schemas["VideoChunkSearchResult"]["required"]
    assert "selectionReason" in schemas["VideoChunkSearchHit"]["required"]
    assert {
        "speaker",
        "text",
        "startMs",
        "endMs",
    } <= set(schemas["TranscriptCueView"]["required"])
    assert "modelName" in schemas["AgentMessageView"]["required"]


def test_agent_session_conversation_send_and_list_messages(tmp_path: Path) -> None:
    client, _retrieval, _jobs = _client(tmp_path)
    series_id = _create_series(client)

    session_response = client.post(
        "/api/agent/sessions/create",
        headers={"Idempotency-Key": "agent-session-1"},
        json={"workspaceId": "local-default", "name": "画面札记"},
    )
    assert session_response.status_code == 201, session_response.text
    session_id = session_response.json()["sessionId"]

    conversation_response = client.post(
        "/api/agent/conversations/create",
        params={"sessionId": session_id},
        headers={"Idempotency-Key": "conversation-1"},
        json={
            "workspaceId": "local-default",
            "title": "寻找哭泣场景",
            "seriesIds": [series_id],
        },
    )
    assert conversation_response.status_code == 201, conversation_response.text
    conversation_id = conversation_response.json()["conversationId"]

    sent = client.post(
        "/api/agent/messages/send",
        params={"conversationId": conversation_id},
        headers={"Idempotency-Key": "message-1"},
        json={
            "workspaceId": "local-default",
            "content": "她为什么哭？",
            "topK": 5,
        },
    )
    listed = client.get(
        "/api/agent/messages/list",
        params={"conversationId": conversation_id},
    )
    listed_sessions = client.get("/api/agent/sessions/list")
    listed_conversations = client.get(
        "/api/agent/conversations/list",
        params={"sessionId": session_id},
    )
    messages_after_user = client.get(
        "/api/agent/messages/list",
        params={"conversationId": conversation_id, "afterSequenceNo": 0},
    )

    assert sent.status_code == 200, sent.text
    assert listed.status_code == 200
    assert [item["role"] for item in listed.json()] == ["USER", "ASSISTANT"]
    assert listed.json()[0]["modelName"] is None
    assert listed.json()[1]["modelName"] == "chat-model-v1"
    assert listed.json()[1]["status"] == "COMPLETED"
    assert listed.json()[1]["searchResult"]["seriesIds"] == [series_id]
    assert listed_sessions.json()[0]["conversationCount"] == 1
    assert listed_conversations.json()[0]["messageCount"] == 2
    assert [item["role"] for item in messages_after_user.json()] == ["ASSISTANT"]


def test_series_and_search_contract(tmp_path: Path) -> None:
    client, retrieval, _jobs = _client(tmp_path)
    series_id = _create_series(client)

    listed = client.get("/api/series/list").json()
    detail = client.get(
        "/api/series/get",
        params={"seriesId": series_id},
    ).json()
    searched = client.post(
        "/api/search",
        json={
            "workspaceId": "local-default",
            "query": "她为什么哭",
            "seriesIds": [series_id],
            "topK": 5,
        },
    )

    assert listed[0]["seriesId"] == series_id
    assert detail["videoCount"] == 0
    assert searched.status_code == 200
    assert searched.json() == {
        "query": "她为什么哭",
        "seriesIds": [series_id],
        "agentReply": {
            "message": "这次没有找到符合当前检索范围的候选片段。",
            "resultCount": 0,
        },
        "hits": [],
    }
    assert retrieval.calls == [("她为什么哭", [series_id], 5, "local-default")]


def test_chunked_upload_complete_job_progress_and_video_range(tmp_path: Path) -> None:
    client, _retrieval, jobs_repository = _client(tmp_path)
    series_id = _create_series(client)
    create_payload = {
        "workspaceId": "local-default",
        "episodeOrder": 7,
        "originalFilename": "episode-07.mp4",
        "mediaType": "video/mp4",
        "totalSizeBytes": 10,
    }

    first = client.post(
        "/api/uploads/create",
        params={"seriesId": series_id},
        headers={"Idempotency-Key": "episode-07-upload"},
        json=create_payload,
    )
    repeated = client.post(
        "/api/uploads/create",
        params={"seriesId": series_id},
        headers={"Idempotency-Key": "episode-07-upload"},
        json=create_payload,
    )
    assert first.status_code == 201, first.text
    assert repeated.status_code == 201
    upload_id = first.json()["uploadSessionId"]
    assert repeated.json()["uploadSessionId"] == upload_id
    assert first.json()["missingSegmentNumbers"] == [0, 1, 2]

    parts = [(2, 8, b"ij"), (0, 0, b"abcd"), (1, 4, b"efgh")]
    for number, start, data in parts:
        end = start + len(data) - 1
        response = client.put(
            "/api/uploads/segments/upload",
            params={
                "uploadSessionId": upload_id,
                "segmentNumber": number,
            },
            headers={
                "Content-Range": f"bytes {start}-{end}/10",
                "X-Content-SHA256": hashlib.sha256(data).hexdigest(),
                "Content-Type": "application/octet-stream",
            },
            content=data,
        )
        assert response.status_code == 200, response.text

    current = client.get(
        "/api/uploads/get",
        params={"uploadSessionId": upload_id},
    )
    assert current.json()["uploadedSizeBytes"] == 10
    assert current.json()["missingSegmentNumbers"] == []

    completed = client.post(
        "/api/uploads/complete",
        params={"uploadSessionId": upload_id},
    )
    assert completed.status_code == 200, completed.text
    video_id = completed.json()["videoId"]
    job_id = completed.json()["jobId"]
    job = client.get("/api/jobs/get", params={"jobId": job_id})
    jobs = client.get("/api/jobs/list")
    assert job.status_code == 200
    assert job.json()["status"] == "QUEUED"
    assert job.json()["progress"] == 0
    assert job.json()["seriesName"] == "Test Series"
    assert job.json()["episodeOrder"] == 7
    assert job.json()["originalFilename"] == "episode-07.mp4"
    assert [item["jobId"] for item in jobs.json()] == [job_id]
    for path in ("/api/jobs/get", "/api/jobs/events"):
        outside_workspace = client.get(
            path,
            params={"jobId": job_id, "workspaceId": "another-workspace"},
        )
        assert outside_workspace.status_code == 404

    lease = jobs_repository.claim_next(
        "api-test-worker",
        lease_duration=timedelta(minutes=5),
    )
    assert lease is not None
    job_service = client.app.state.services.jobs
    before_heartbeat = _job_view(
        job_service.get(job_id, workspace_id="local-default")
    )
    assert jobs_repository.heartbeat(
        lease,
        lease_duration=timedelta(minutes=5),
    )
    after_heartbeat = _job_view(
        job_service.get(job_id, workspace_id="local-default")
    )
    assert _job_event_fingerprint(before_heartbeat) == _job_event_fingerprint(
        after_heartbeat
    )
    assert jobs_repository.update_progress(
        lease,
        stage=ParseJobStage.PARSING,
        progress=30,
    )
    after_progress = _job_view(
        job_service.get(job_id, workspace_id="local-default")
    )
    assert _job_event_fingerprint(after_progress) != _job_event_fingerprint(
        after_heartbeat
    )
    assert jobs_repository.mark_succeeded(lease)
    with client.stream(
        "GET",
        "/api/jobs/events",
        params={"jobId": job_id},
    ) as events:
        body = "\n".join(events.iter_lines())
    assert "event: job" in body
    assert '"status":"SUCCEEDED"' in body

    full_video = client.get(
        "/api/videos/content",
        params={"videoId": video_id},
    )
    ranged_video = client.get(
        "/api/videos/content",
        params={"videoId": video_id},
        headers={"Range": "bytes=2-5"},
    )
    assert full_video.content == b"abcdefghij"
    assert ranged_video.status_code == 206
    assert ranged_video.content == b"cdef"


def test_upload_rejects_wrong_content_range_and_workspace(tmp_path: Path) -> None:
    client, _retrieval, _jobs = _client(tmp_path)
    series_id = _create_series(client)
    created = client.post(
        "/api/uploads/create",
        params={"seriesId": series_id},
        headers={"Idempotency-Key": "bad-range"},
        json={
            "episodeOrder": 1,
            "originalFilename": "episode.mp4",
            "mediaType": "video/mp4",
            "totalSizeBytes": 4,
        },
    )
    upload_id = created.json()["uploadSessionId"]

    wrong_workspace = client.get(
        "/api/uploads/get",
        params={
            "uploadSessionId": upload_id,
            "workspaceId": "another-workspace",
        },
    )
    wrong_range = client.put(
        "/api/uploads/segments/upload",
        params={
            "uploadSessionId": upload_id,
            "segmentNumber": 0,
        },
        headers={
            "Content-Range": "bytes 0-2/4",
            "X-Content-SHA256": hashlib.sha256(b"abc").hexdigest(),
        },
        content=b"abc",
    )

    assert wrong_workspace.status_code == 404
    assert wrong_range.status_code == 409
    assert wrong_range.json()["code"] == "CONFLICT"

    mismatched_total = client.put(
        "/api/uploads/segments/upload",
        params={"uploadSessionId": upload_id, "segmentNumber": 0},
        headers={
            "Content-Range": "bytes 0-3/5",
            "X-Content-SHA256": hashlib.sha256(b"abcd").hexdigest(),
        },
        content=b"abcd",
    )
    assert mismatched_total.status_code == 409
    for path, method in (
        ("/api/uploads/segments/upload", "PUT"),
        ("/api/uploads/complete", "POST"),
    ):
        outside_workspace = client.request(
            method,
            path,
            params={
                "uploadSessionId": upload_id,
                "segmentNumber": 0,
                "workspaceId": "another-workspace",
            },
            headers={
                "Content-Range": "bytes 0-3/4",
                "X-Content-SHA256": hashlib.sha256(b"abcd").hexdigest(),
            },
            content=b"abcd" if method == "PUT" else None,
        )
        assert outside_workspace.status_code == 404


@pytest.mark.parametrize(
    ("segment_number", "content_range", "data", "expected_status"),
    [
        (0, "not-a-byte-range", b"abcd", 400),
        (0, "bytes 0-3/4", b"abc", 400),
        (-1, "bytes 0-3/4", b"abcd", 422),
    ],
)
def test_upload_protocol_errors_do_not_require_business_data(
    tmp_path: Path,
    segment_number: int,
    content_range: str,
    data: bytes,
    expected_status: int,
) -> None:
    client, _retrieval, _jobs = _client(tmp_path)
    response = client.put(
        "/api/uploads/segments/upload",
        params={
            "uploadSessionId": "00000000-0000-0000-0000-000000000000",
            "segmentNumber": segment_number,
        },
        headers={
            "Content-Range": content_range,
            "X-Content-SHA256": hashlib.sha256(data).hexdigest(),
        },
        content=data,
    )
    assert response.status_code == expected_status


def _create_agent_conversation(client, series_id):
    session = client.post(
        "/api/agent/sessions/create", headers={"Idempotency-Key": "session-stream"},
        json={"name": "流式会话"},
    ).json()
    response = client.post(
        "/api/agent/conversations/create", params={"sessionId": session["sessionId"]},
        headers={"Idempotency-Key": "conversation-stream"},
        json={"seriesIds": [series_id]},
    )
    assert response.status_code == 201, response.text
    return response.json()["conversationId"]


def _answer_payloads(text):
    import json
    return [json.loads(line[6:]) for line in text.splitlines() if line.startswith("data: ")]


def test_same_send_path_supports_live_events_and_idempotent_replay(tmp_path):
    client, retrieval, _ = _client(tmp_path)
    conversation_id = _create_agent_conversation(client, _create_series(client))
    kwargs = {
        "params": {"conversationId": conversation_id},
        "headers": {"Accept": "text/event-stream", "Idempotency-Key": "stream-message"},
        "json": {"content": "找哭泣的画面", "topK": 5},
    }
    response = client.post("/api/agent/messages/send", **kwargs)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = _answer_payloads(response.text)
    assert [event["type"] for event in events] == [
        "answer.started", "answer.evidence", "answer.delta", "answer.delta", "answer.completed",
    ]
    assert [event["sequence"] for event in events] == [1, 2, 3, 4, 5]
    assert all(event["conversationId"] == conversation_id for event in events)
    assert len({event["messageId"] for event in events}) == 1
    assert events[0]["assistantMessage"]["status"] == "PROCESSING"
    assert events[0]["assistantMessage"]["content"] is None
    assert events[0]["userMessage"]["modelName"] is None
    final = events[-1]["assistantMessage"]
    assert final["status"] == "COMPLETED"
    assert final["content"] == "".join(event["delta"] for event in events if event["type"] == "answer.delta")
    persisted = client.get("/api/agent/messages/list", params={"conversationId": conversation_id}).json()
    assert final == persisted[-1]
    replay = _answer_payloads(client.post("/api/agent/messages/send", **kwargs).text)
    assert [event["type"] for event in replay] == ["answer.started", "answer.completed"]
    assert replay[-1]["messageId"] == final["messageId"]
    assert len(retrieval.calls) == 1
    content = client.get("/openapi.json").json()["paths"]["/api/agent/messages/send"]["post"]["responses"]["200"]["content"]
    assert {"application/json", "text/event-stream"}.issubset(content)
    client.app.state.services.agent.close()


def test_rejected_loop_sends_failed_event_and_json_maps_harness_error(tmp_path):
    client, _, _ = _client(tmp_path)
    conversation_id = _create_agent_conversation(client, _create_series(client))
    runtime = client.app.state.services.agent._runtime

    class RejectModel:
        model_name = "reject-model"
        def generate(self, _inputs, *, schema_name, result_type):
            return result_type.model_validate({"action": "continue", "advice": "需要重新找摔杯子的片段。"})

    runtime._evaluator_model = RejectModel()
    def forbidden_chat(_inputs):
        raise AssertionError("rejected draft reached final Chat")
    runtime._chat_model.stream_text = forbidden_chat
    response = client.post(
        "/api/agent/messages/send", params={"conversationId": conversation_id},
        headers={"Accept": "text/event-stream", "Idempotency-Key": "rejected-stream"},
        json={"content": "找摔杯子的画面"},
    )
    events = _answer_payloads(response.text)
    assert [event["type"] for event in events] == ["answer.started", "answer.failed"]
    assert events[-1]["code"] == "AGENT_MODEL_ERROR"
    assert events[-1]["assistantMessage"]["status"] == "FAILED"
    assert "after 3 turns" in events[-1]["message"]
    json_response = client.post(
        "/api/agent/messages/send", params={"conversationId": conversation_id},
        headers={"Idempotency-Key": "rejected-json"}, json={"content": "重试"},
    )
    assert json_response.status_code == 502
    assert json_response.json()["code"] == "AGENT_MODEL_ERROR"
    outside = client.post(
        "/api/agent/messages/send", params={"conversationId": conversation_id},
        headers={"Accept": "text/event-stream", "Idempotency-Key": "outside"},
        json={"content": "隔离", "workspaceId": "other-workspace"},
    )
    assert outside.status_code == 404
    client.app.state.services.agent.close()


def test_asgi_delivers_first_delta_before_provider_finishes(tmp_path):
    import asyncio
    import json
    from threading import Event

    client, _, _ = _client(tmp_path)
    conversation_id = _create_agent_conversation(client, _create_series(client))
    service = client.app.state.services.agent
    release = Event()

    def blocked_provider(_inputs):
        yield "第一段"
        assert release.wait(5), "test did not release Provider"
        yield "第二段"
    service._runtime._chat_model.stream_text = blocked_provider
    body = json.dumps({"content": "找画面"}).encode()

    async def exercise():
        first_delta = asyncio.Event()
        request_done = False
        never_disconnected = asyncio.Event()
        output = []

        async def receive():
            nonlocal request_done
            if not request_done:
                request_done = True
                return {"type": "http.request", "body": body, "more_body": False}
            await never_disconnected.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            output.append(message)
            if message["type"] == "http.response.body" and b"event: answer.delta" in message.get("body", b""):
                first_delta.set()

        scope = {
            "type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1", "method": "POST", "scheme": "http",
            "path": "/api/agent/messages/send", "raw_path": b"/api/agent/messages/send",
            "root_path": "", "query_string": f"conversationId={conversation_id}".encode(),
            "headers": [(b"content-type", b"application/json"), (b"accept", b"text/event-stream"), (b"idempotency-key", b"live-asgi")],
            "client": ("127.0.0.1", 10000), "server": ("testserver", 80),
        }
        task = asyncio.create_task(client.app(scope, receive, send))
        try:
            await asyncio.wait_for(first_delta.wait(), timeout=3)
            assert not release.is_set() and not task.done()
            assert service.list_all_messages(conversation_id, workspace_id="local-default")[-1].status == "PROCESSING"
        finally:
            release.set()
        await asyncio.wait_for(task, timeout=3)
        events = _answer_payloads(b"".join(message.get("body", b"") for message in output if message["type"] == "http.response.body").decode())
        assert [event["delta"] for event in events if event["type"] == "answer.delta"] == ["第一段", "第二段"]
        assert events[-1]["assistantMessage"]["content"] == "第一段第二段"

    try:
        asyncio.run(exercise())
    finally:
        release.set()
        service.close()


@pytest.mark.parametrize("accept,expected", [
    ("text/event-stream;q=0", "application/json"),
    ("application/json;q=1,text/event-stream;q=0.5", "application/json"),
    ("application/json;q=0.5,text/event-stream;q=1", "text/event-stream"),
])
def test_send_respects_accept_quality(tmp_path, accept, expected):
    client, _, _ = _client(tmp_path)
    conversation_id = _create_agent_conversation(client, _create_series(client))
    response = client.post(
        "/api/agent/messages/send", params={"conversationId": conversation_id},
        headers={"Accept": accept, "Idempotency-Key": "accept-message"},
        json={"content": "找画面"},
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith(expected)
    client.app.state.services.agent.close()


@pytest.mark.parametrize("failure,expected_status,expected_code", [
    ("not-ready", 409, "CONFLICT"),
    ("missing-scope", 404, "NOT_FOUND"),
    ("index-inconsistent", 409, "CONFLICT"),
])
def test_retrieval_domain_errors_keep_http_mapping_through_harness(tmp_path, failure, expected_status, expected_code):
    from server.repository.video import SeriesNotReadyError, SeriesScopeNotFoundError
    from server.service.video_retrieval import RetrievalIndexConsistencyError
    client, _, _ = _client(tmp_path)
    conversation_id = _create_agent_conversation(client, _create_series(client))
    error_type = {
        "not-ready": SeriesNotReadyError,
        "missing-scope": SeriesScopeNotFoundError,
        "index-inconsistent": RetrievalIndexConsistencyError,
    }[failure]
    def failed_search(*_args, **_kwargs):
        raise error_type("retrieval source unavailable")
    service = client.app.state.services.agent
    service._runtime._retrieve = failed_search
    try:
        response = client.post(
            "/api/agent/messages/send", params={"conversationId": conversation_id},
            headers={"Idempotency-Key": "retrieval-error"}, json={"content": "找画面"},
        )
        assert response.status_code == expected_status
        assert response.json()["code"] == expected_code
        failed = service.list_all_messages(conversation_id, workspace_id="local-default")[-1]
        assert failed.status == "FAILED" and failed.last_error.startswith(error_type.__name__)
        response = client.post(
            "/api/agent/messages/send", params={"conversationId": conversation_id},
            headers={"Accept": "text/event-stream", "Idempotency-Key": "retrieval-stream-error"},
            json={"content": "再找画面"},
        )
        events = _answer_payloads(response.text)
        assert [event["type"] for event in events] == ["answer.started", "answer.failed"]
        assert events[-1]["code"] == "AGENT_EXECUTION_ERROR"
        assert events[-1]["assistantMessage"]["status"] == "FAILED"
    finally:
        service.close()
