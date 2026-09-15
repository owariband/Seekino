from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import unquote, urlparse
from uuid import uuid4

import pytest
from sqlalchemy import select

from server.api import library as library_api
from server.model.vo.commonVO import unix_time_ms
from server.api.library import project_series_detail
from server.database import DatabaseEnvironment, create_session_factory
from server.model.agent import AgentConversationRecord, AgentMessageRecord
from server.model.index_sync import IndexSyncIntentRecord
from server.model.job import SeriesParseJobRecord
from server.model.video import VideoChunkEmbeddingRecord, VideoRecord
from server.repository.library import LibraryRepository, SeriesNotFoundError
from server.model.video import VideoIndexStatus
from server.repository.upload import UploadRepository
from server.service.library import LibraryService
from server.service.media import MediaService
from server.storage.artifact import LocalArtifactStore
from server.utils.time import utc_now_naive


def _library(repository):
    engine, videos = repository
    factory = create_session_factory(engine)
    return factory, videos, LibraryService(LibraryRepository(factory))


def test_offline_video_views_use_actual_index_and_media_facts(repository, prepared_import, tmp_path):
    factory, videos, library = _library(repository)
    report = videos.import_video_parse(prepared_import)
    store = LocalArtifactStore(tmp_path / "artifacts", environment=DatabaseEnvironment.TEST)
    staged = store.put_upload_segment(
        str(uuid4()), 0, Path(unquote(urlparse(prepared_import.original_source_uri).path)).read_bytes(),
    )
    original = store.publish_original(
        staged,
        series_id=report.series_id, video_id=report.video_id,
        original_filename="episode.mp4",
    )
    with factory.begin() as session:
        session.get(VideoRecord, report.video_id).original_source_uri = original.uri
    media = MediaService(library, store, probe_playback=lambda _path: True)

    initial = library.get_series(report.series_id, workspace_id="local-default")
    assert initial.video_count == 1
    assert initial.ready_video_count == 0
    assert initial.videos[0].index_status == VideoIndexStatus.NOT_READY
    assert initial.videos[0].latest_job_id is None

    with factory.begin() as session:
        for chunk in session.scalars(select(VideoChunkEmbeddingRecord)):
            chunk.text_vector = [0.1, 0.2]
            chunk.vision_vector = [0.2, 0.3]
    assert library.get_video(report.video_id, workspace_id="local-default").index_status == VideoIndexStatus.SYNCING
    with factory.begin() as session:
        for intent in session.scalars(select(IndexSyncIntentRecord)):
            intent.operation = "UPSERT"
            intent.status = "SYNCED"
    ready = library.get_series(report.series_id, workspace_id="local-default")
    assert ready.ready_video_count == 1
    detail = project_series_detail(ready, media).model_dump(by_alias=True)
    assert detail["createdAt"] == unix_time_ms(ready.created_at)
    assert detail["videos"][0]["indexStatus"] == "READY"
    assert detail["videos"][0]["parseStatus"] == "SUCCEEDED"
    assert detail["videos"][0]["playbackStatus"] == "READY"
    assert detail["videos"][0]["latestJobId"] is None
    assert detail["videos"][0]["thumbnail"] is None
    assert detail["cover"] is None
    for internal_field in ("originalSourceURI", "originalSourceUri", "contentHash", "inferenceProxyURI", "keyframeURIs"):
        assert internal_field not in detail["videos"][0]

    store.resolve(original.uri).unlink()
    missing = project_series_detail(ready, media).videos[0]
    assert missing.playback_status == "MISSING"
    assert missing.playback_url is None
    assert missing.last_error.code == "MEDIA_MISSING"


def test_latest_job_failure_and_index_error_are_actual_and_workspace_scoped(repository, prepared_import):
    factory, videos, library = _library(repository)
    report = videos.import_video_parse(prepared_import)
    now = utc_now_naive()
    job_id = str(uuid4())
    with factory.begin() as session:
        session.add(SeriesParseJobRecord(
            job_id=job_id, series_id=report.series_id, video_id=report.video_id,
            pipeline_version="failure-pipeline", status="FAILED", stage="PARSING",
            progress=42, attempt_count=3, max_attempts=3, available_at=now,
            completed_at=now, last_error="private provider endpoint failure",
        ))
        for intent in session.scalars(select(IndexSyncIntentRecord)):
            intent.status = "RETRY_WAIT"
            intent.last_error = "private index endpoint failure"
    state = library.get_series(report.series_id, workspace_id="local-default")
    assert state.failed_video_count == 1
    assert state.ready_video_count == 0
    video = state.videos[0]
    assert video.latest_job_id == job_id
    assert video.parse_status == "FAILED"
    assert video.parse_progress == 42
    assert video.index_status == VideoIndexStatus.FAILED
    with pytest.raises(SeriesNotFoundError):
        library.get_video(report.video_id, workspace_id="other-workspace")


def test_pending_upload_counts_expiry_and_full_detail(repository):
    factory, _videos, library = _library(repository)
    series = library.create_series(workspace_id="local-default", name="Pending Series")
    uploads = UploadRepository(factory)
    now = utc_now_naive()
    active = []
    for episode, expires in ((2, now + timedelta(days=1)), (3, now + timedelta(days=1)), (4, now - timedelta(seconds=1))):
        state = uploads.create_session(
            workspace_id="local-default", series_id=series.series_id,
            episode_order=episode, upload_sequence=episode,
            original_filename=f"episode-{episode}.mp4", media_type="video/mp4",
            total_size_bytes=10, preferred_segment_size_bytes=4, total_segments=3,
            idempotency_key=f"upload-{episode}", expires_at=expires,
            now=now - timedelta(days=1),
        )
        if episode != 4:
            active.append(state.upload_session_id)
    detail = library.get_series(series.series_id, workspace_id="local-default")
    assert detail.video_count == 0
    assert detail.pending_upload_count == 2
    assert [item.upload_session_id for item in detail.pending_uploads] == active
    assert [item.upload_sequence for item in detail.pending_uploads] == [1, 2]
    assert all(item.missing_segment_numbers == (0, 1, 2) for item in detail.pending_uploads)
    assert library.list_series(workspace_id="local-default")[0].pending_upload_count == 2


def test_last_search_uses_each_message_scope_and_workspace(repository):
    factory, _videos, library = _library(repository)
    first = library.create_series(workspace_id="local-default", name="First")
    second = library.create_series(workspace_id="local-default", name="Second")
    now = utc_now_naive()
    local_conversation, foreign_conversation = str(uuid4()), str(uuid4())
    with factory.begin() as session:
        for conversation_id, workspace in ((local_conversation, "local-default"), (foreign_conversation, "other")):
            session.add(AgentConversationRecord(
                conversation_id=conversation_id, workspace_id=workspace,
                title="Scope history", idempotency_key=conversation_id,
            ))
        session.flush()
        for sequence, conversation, scope, content, created_at in (
            (0, local_conversation, [first.series_id], "first question", now - timedelta(minutes=2)),
            (1, local_conversation, [second.series_id], "second question", now - timedelta(minutes=1)),
            (0, foreign_conversation, [first.series_id, second.series_id], "foreign secret", now),
        ):
            session.add(AgentMessageRecord(
                message_id=str(uuid4()), conversation_id=conversation, sequence_no=sequence,
                role="USER", status="COMPLETED", content=content, series_ids=scope,
                requested_series_ids=scope, created_at=created_at,
            ))
    states = {state.series_id: state for state in library.list_series(workspace_id="local-default")}
    assert states[first.series_id].last_search.query == "first question"
    assert states[second.series_id].last_search.query == "second question"
    assert states[first.series_id].last_search.conversation_id == local_conversation


def test_series_page_counts_filters_and_cursor_bind_actual_query(repository, tmp_path):
    factory, _videos, library = _library(repository)
    for name in ("Match A", "Match B", "Other"):
        library.create_series(workspace_id="local-default", name=name)
    library.create_series(workspace_id="other", name="Match Foreign")
    media = MediaService(library, LocalArtifactStore(tmp_path / "artifacts", environment=DatabaseEnvironment.TEST))
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(services=SimpleNamespace(library=library, media=media))))
    first = library_api.list_series(request, "local-default", None, 1, "EMPTY", "Match")
    assert first.total == 2
    assert len(first.items) == 1
    assert first.has_more
    second = library_api.list_series(request, "local-default", first.next_cursor, 1, "EMPTY", "Match")
    assert second.total == 2
    assert not second.has_more
    assert {first.items[0].name, second.items[0].name} == {"Match A", "Match B"}
    assert "videos" not in first.items[0].model_dump()
    with pytest.raises(ValueError, match="cursor"):
        library_api.list_series(request, "local-default", first.next_cursor, 1, "EMPTY", "Other")
    with pytest.raises(ValueError, match="cursor"):
        library_api.list_series(request, "other", first.next_cursor, 1, "EMPTY", "Match")


def test_background_edit_preserves_video_order_and_character_metadata(repository, prepared_import):
    _factory, videos, library = _library(repository)
    report = videos.import_video_parse(prepared_import)
    before = library.get_series(report.series_id, workspace_id="local-default")
    edited = library.update_series(
        report.series_id, workspace_id="local-default",
        changes={"description": "背景资料更新", "important_extra_data": {"content": "# 新背景"}},
    )
    assert edited.description == "背景资料更新"
    assert edited.important_extra_data == {"format": "markdown", "content": "# 新背景"}
    assert edited.name == before.name
    assert edited.character_bible == before.character_bible
    assert [(video.video_id, video.episode_order, video.upload_sequence) for video in edited.videos] == [
        (video.video_id, video.episode_order, video.upload_sequence) for video in before.videos
    ]


def test_create_preserves_legacy_freeform_background_and_validates_managed_refs(repository):
    _factory, _videos, library = _library(repository)
    legacy = {"seriesBible": "旧Markdown", "arbitrary": {"custom": [1, "freeform"]}}
    series = library.create_series(workspace_id="local-default", name="Legacy", important_extra_data=legacy)
    assert series.important_extra_data == legacy
    with pytest.raises(ValueError, match="storage is unavailable"):
        library.create_series(
            workspace_id="local-default", name="Invalid image",
            important_extra_data={"arbitrary": {"image": "asset://missing"}},
        )
    assert library.count_series(workspace_id="local-default") == 1
