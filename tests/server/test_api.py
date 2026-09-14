from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from server.api.app import _job_event_fingerprint, _job_view, create_app
from server.api.dependencies import ApplicationServices
from server.database import (
    DatabaseEnvironment,
    create_database_engine,
    create_schema,
    create_session_factory,
)
from server.model.job import ParseJobStage
from server.repository.job import ParseJobRepository
from server.repository.library import LibraryRepository
from server.repository.upload import UploadRepository
from server.schema import AgentReply, VideoChunkSearchResult
from server.service.job import ParseJobService
from server.service.library import LibraryService
from server.service.media import MediaService
from server.service.upload import UploadService, UploadServiceConfig
from server.storage.artifact import LocalArtifactStore


@dataclass
class _RetrievalStub:
    calls: list[tuple[str, list[str], int]]

    def search(self, query: str, *, series_ids, top_k: int):
        scope = list(series_ids)
        self.calls.append((query, scope, top_k))
        return VideoChunkSearchResult(
            query=query,
            series_ids=scope,
            agent_reply=AgentReply(
                message="这次没有找到符合当前检索范围的候选片段。",
                result_count=0,
            ),
            hits=[],
        )


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
        jobs=ParseJobService(job_repository),
        media=MediaService(library, artifact_store),
        retrieval=retrieval,  # type: ignore[arg-type]
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
    } <= paths
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
    assert retrieval.calls == [("她为什么哭", [series_id], 5)]


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

    lease = jobs_repository.claim_next(
        "api-test-worker",
        lease_duration=timedelta(minutes=5),
    )
    assert lease is not None
    job_service = ParseJobService(jobs_repository)
    before_heartbeat = _job_view(job_service.get(job_id))
    assert jobs_repository.heartbeat(
        lease,
        lease_duration=timedelta(minutes=5),
    )
    after_heartbeat = _job_view(job_service.get(job_id))
    assert _job_event_fingerprint(before_heartbeat) == _job_event_fingerprint(
        after_heartbeat
    )
    assert jobs_repository.update_progress(
        lease,
        stage=ParseJobStage.PARSING,
        progress=30,
    )
    after_progress = _job_view(job_service.get(job_id))
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
