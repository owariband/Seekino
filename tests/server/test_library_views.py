from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import unquote, urlparse
from uuid import uuid4

import pytest
from sqlalchemy import select

from server.api import library as library_api
from server.api.library import project_series_detail
from server.database import DatabaseEnvironment, create_session_factory
from server.model.agent import AgentConversationRecord, AgentMessageRecord
from server.model.index_sync import IndexSyncIntentRecord
from server.model.job import SeriesParseJobRecord, VideoChunkCheckpointRecord
from server.model.upload import UploadSessionRecord
from server.model.video import VideoChunkEmbeddingRecord, VideoRecord
from server.repository.library import (
    LibraryRepository,
    SeriesNotFoundError,
    VideoEpisodeOrderConflictError,
)
from server.model.video import VideoIndexStatus
from server.repository.upload import UploadRepository
from server.service.library import LibraryService
from server.service.media import MediaService
from server.storage.artifact import LocalArtifactStore
from server.utils.time import now_unix_time_ms, timedelta_to_ms


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
    assert detail["createdAt"] == ready.created_at
    assert detail["videos"][0]["indexStatus"] == "READY"
    assert detail["videos"][0]["parseStatus"] == "SUCCEEDED"
    assert detail["videos"][0]["parseProgress"] == 100
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
    now = now_unix_time_ms()
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
    assert video.parse_progress is None
    assert video.index_status == VideoIndexStatus.FAILED
    with pytest.raises(SeriesNotFoundError):
        library.get_video(report.video_id, workspace_id="other-workspace")


def test_ready_video_count_keeps_an_indexed_video_during_failed_reparse(
    repository, prepared_import,
):
    factory, videos, library = _library(repository)
    report = videos.import_video_parse(prepared_import)
    now = now_unix_time_ms()
    with factory.begin() as session:
        for chunk in session.scalars(select(VideoChunkEmbeddingRecord)):
            chunk.text_vector = [0.1, 0.2]
            chunk.vision_vector = [0.2, 0.3]
        for intent in session.scalars(select(IndexSyncIntentRecord)):
            intent.operation = "UPSERT"
            intent.status = "SYNCED"
        session.add(SeriesParseJobRecord(
            job_id=str(uuid4()),
            series_id=report.series_id,
            video_id=report.video_id,
            pipeline_version="failed-reparse",
            status="FAILED",
            stage="PARSING",
            progress=42,
            attempt_count=3,
            max_attempts=3,
            available_at=now,
            completed_at=now,
            last_error="reparse failed before replacing the indexed result",
        ))

    state = library.get_series(report.series_id, workspace_id="local-default")
    assert state.ready_video_count == 1
    assert state.videos[0].parse_status == "FAILED"
    assert state.videos[0].index_status == VideoIndexStatus.READY


def test_library_progress_uses_checkpoint_duration_instead_of_stored_job_percent(
    repository,
    prepared_import,
) -> None:
    factory, videos, library = _library(repository)
    report = videos.import_video_parse(prepared_import)
    now = now_unix_time_ms()
    job_id = str(uuid4())
    with factory.begin() as session:
        session.add(SeriesParseJobRecord(
            job_id=job_id,
            series_id=report.series_id,
            video_id=report.video_id,
            pipeline_version="checkpoint-progress",
            status="RUNNING",
            stage="PARSING",
            progress=34,
            attempt_count=1,
            max_attempts=3,
            available_at=now,
        ))
        session.add_all([
            VideoChunkCheckpointRecord(
                job_id=job_id,
                sequence_no=0,
                video_chunk_id=str(uuid4()),
                stage="PARSED",
                chunk_start_seconds=0,
                chunk_end_seconds=15,
            ),
            VideoChunkCheckpointRecord(
                job_id=job_id,
                sequence_no=1,
                video_chunk_id=str(uuid4()),
                stage="SPLIT_READY",
                chunk_start_seconds=15,
                chunk_end_seconds=20,
            ),
        ])

    video = library.get_video(report.video_id, workspace_id="local-default")

    assert video.latest_job_id == job_id
    assert video.parse_progress == 74

    with factory.begin() as session:
        checkpoint = session.get(VideoChunkCheckpointRecord, (job_id, 1))
        checkpoint.chunk_start_seconds = 16

    assert (
        library.get_video(
            report.video_id,
            workspace_id="local-default",
        ).parse_progress
        is None
    )


def test_pending_upload_counts_expiry_and_full_detail(repository):
    factory, _videos, library = _library(repository)
    series = library.create_series(workspace_id="local-default", name="Pending Series")
    uploads = UploadRepository(factory)
    now = now_unix_time_ms()
    active = []
    for episode, expires in ((2, now + timedelta_to_ms(timedelta(days=1))), (3, now + timedelta_to_ms(timedelta(days=1))), (4, now - timedelta_to_ms(timedelta(seconds=1)))):
        state = uploads.create_session(
            workspace_id="local-default", series_id=series.series_id,
            episode_order=episode, upload_sequence=episode,
            original_filename=f"episode-{episode}.mp4", media_type="video/mp4",
            total_size_bytes=10, preferred_segment_size_bytes=4, total_segments=3,
            idempotency_key=f"upload-{episode}", expires_at=expires,
            now=now - timedelta_to_ms(timedelta(days=1)),
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
    now = now_unix_time_ms()
    local_conversation, foreign_conversation = str(uuid4()), str(uuid4())
    with factory.begin() as session:
        for conversation_id, workspace in ((local_conversation, "local-default"), (foreign_conversation, "other")):
            session.add(AgentConversationRecord(
                conversation_id=conversation_id, workspace_id=workspace,
                title="Scope history", idempotency_key=conversation_id,
            ))
        session.flush()
        for sequence, conversation, scope, content, created_at in (
            (0, local_conversation, [first.series_id], "first question", now - timedelta_to_ms(timedelta(minutes=2))),
            (1, local_conversation, [second.series_id], "second question", now - timedelta_to_ms(timedelta(minutes=1))),
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


def test_episode_edit_updates_linked_upload_without_changing_execution_order(
    repository,
    prepared_import,
) -> None:
    factory, videos, library = _library(repository)
    report = videos.import_video_parse(prepared_import)
    upload_session_id = str(uuid4())
    inactive_upload_session_id = str(uuid4())
    now = now_unix_time_ms()
    with factory.begin() as session:
        session.add_all([
            UploadSessionRecord(
                upload_session_id=upload_session_id,
                workspace_id=prepared_import.workspace_id,
                series_id=report.series_id,
                episode_order=prepared_import.episode_order,
                upload_sequence=prepared_import.upload_sequence,
                original_filename=prepared_import.original_filename,
                media_type="video/mp4",
                total_size_bytes=prepared_import.total_bytes,
                preferred_segment_size_bytes=prepared_import.total_bytes,
                total_segments=1,
                status="COMPLETED",
                idempotency_key="episode-edit-linked-upload",
                active_slot_key=f"{report.series_id}:{prepared_import.episode_order}",
                expires_at=now + timedelta_to_ms(timedelta(days=1)),
                completed_at=now,
                video_id=report.video_id,
            ),
            UploadSessionRecord(
                upload_session_id=inactive_upload_session_id,
                workspace_id=prepared_import.workspace_id,
                series_id=report.series_id,
                episode_order=prepared_import.episode_order,
                upload_sequence=prepared_import.upload_sequence,
                original_filename=prepared_import.original_filename,
                media_type="video/mp4",
                total_size_bytes=prepared_import.total_bytes,
                preferred_segment_size_bytes=prepared_import.total_bytes,
                total_segments=1,
                status="EXPIRED",
                idempotency_key="episode-edit-inactive-upload",
                active_slot_key=None,
                expires_at=now,
                completed_at=now,
                video_id=report.video_id,
            ),
        ])

    edited = library.update_video_episode_order(
        report.video_id,
        workspace_id=prepared_import.workspace_id,
        episode_order=9,
    )
    replay = library.update_video_episode_order(
        report.video_id,
        workspace_id=prepared_import.workspace_id,
        episode_order=9,
    )

    assert edited.episode_order == replay.episode_order == 9
    assert edited.upload_sequence == replay.upload_sequence == prepared_import.upload_sequence
    with factory() as session:
        video = session.get(VideoRecord, report.video_id)
        upload = session.get(UploadSessionRecord, upload_session_id)
        inactive_upload = session.get(
            UploadSessionRecord,
            inactive_upload_session_id,
        )
        assert video.episode_order == 9
        assert video.upload_sequence == prepared_import.upload_sequence
        assert upload.episode_order == 9
        assert upload.upload_sequence == prepared_import.upload_sequence
        assert upload.active_slot_key == f"{report.series_id}:9"
        assert upload.version == 2
        assert inactive_upload.episode_order == 9
        assert inactive_upload.upload_sequence == prepared_import.upload_sequence
        assert inactive_upload.active_slot_key is None
        assert inactive_upload.version == 2


def test_episode_edit_conflicts_roll_back_video_and_linked_upload(
    repository,
    prepared_import,
) -> None:
    factory, videos, library = _library(repository)
    report = videos.import_video_parse(prepared_import)
    now = now_unix_time_ms()
    linked_upload_id = str(uuid4())
    reserved_upload_id = str(uuid4())
    occupied_video_id = str(uuid4())
    with factory.begin() as session:
        session.add_all([
            UploadSessionRecord(
                upload_session_id=linked_upload_id,
                workspace_id=prepared_import.workspace_id,
                series_id=report.series_id,
                episode_order=prepared_import.episode_order,
                upload_sequence=prepared_import.upload_sequence,
                original_filename=prepared_import.original_filename,
                media_type="video/mp4",
                total_size_bytes=prepared_import.total_bytes,
                preferred_segment_size_bytes=prepared_import.total_bytes,
                total_segments=1,
                status="COMPLETED",
                idempotency_key="episode-edit-conflict-linked",
                active_slot_key=f"{report.series_id}:{prepared_import.episode_order}",
                expires_at=now + timedelta_to_ms(timedelta(days=1)),
                completed_at=now,
                video_id=report.video_id,
            ),
            UploadSessionRecord(
                upload_session_id=reserved_upload_id,
                workspace_id=prepared_import.workspace_id,
                series_id=report.series_id,
                episode_order=10,
                upload_sequence=10,
                original_filename="reserved.mp4",
                media_type="video/mp4",
                total_size_bytes=1,
                preferred_segment_size_bytes=1,
                total_segments=1,
                status="UPLOADING",
                idempotency_key="episode-edit-reserved-slot",
                active_slot_key=f"{report.series_id}:10",
                expires_at=now + timedelta_to_ms(timedelta(days=1)),
            ),
            VideoRecord(
                video_id=occupied_video_id,
                series_id=report.series_id,
                episode_order=11,
                upload_sequence=11,
                original_filename="occupied.mp4",
                original_source_uri="artifact://test/occupied.mp4",
                inference_proxy_uri=None,
                content_hash="f" * 64,
                total_bytes=1,
                duration_seconds=1,
                upload_status="COMPLETED",
                parse_status="SUCCEEDED",
                upload_progress=100,
                parse_progress=100,
            ),
        ])

    for occupied_episode in (10, 11):
        with pytest.raises(
            VideoEpisodeOrderConflictError,
            match="already occupied",
        ):
            library.update_video_episode_order(
                report.video_id,
                workspace_id=prepared_import.workspace_id,
                episode_order=occupied_episode,
            )

    with factory() as session:
        video = session.get(VideoRecord, report.video_id)
        upload = session.get(UploadSessionRecord, linked_upload_id)
        assert video.episode_order == prepared_import.episode_order
        assert video.upload_sequence == prepared_import.upload_sequence
        assert upload.episode_order == prepared_import.episode_order
        assert upload.upload_sequence == prepared_import.upload_sequence
        assert upload.active_slot_key == (
            f"{report.series_id}:{prepared_import.episode_order}"
        )
        assert upload.version == 1


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


def test_series_transcript_language_hint_is_normalized_updated_and_cleared(repository):
    _factory, _videos, library = _library(repository)
    created = library.create_series(
        workspace_id="local-default",
        name="Japanese audio",
        transcript_language_hint="  ja-jp  ",
    )
    assert created.transcript_language_hint == "ja-JP"

    updated = library.update_series(
        created.series_id,
        workspace_id="local-default",
        changes={"transcript_language_hint": "zh-hans"},
    )
    assert updated.transcript_language_hint == "zh-Hans"
    assert library.update_series(
        created.series_id,
        workspace_id="local-default",
        changes={"transcript_language_hint": None},
    ).transcript_language_hint is None

    with pytest.raises(ValueError, match="BCP-47"):
        library.update_series(
            created.series_id,
            workspace_id="local-default",
            changes={"transcript_language_hint": "Japanese language"},
        )
