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
from server.config.parse_profile import default_video_parse_v2_config
from server.database import DatabaseEnvironment, create_session_factory
from server.model.agent import AgentConversation, AgentMessage
from server.model.index_sync import IndexSyncIntent
from server.model.job import (
    SeriesParseJob,
    VideoChunkCheckpoint,
    VideoParseTask,
)
from server.model.upload import UploadSession
from server.model.video import Series, VideoChunkEmbedding, Video
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
from server.utils.time import now_unix_time_ms, now_utc, timedelta_to_ms


def _library(repository):
    engine, videos = repository
    factory = create_session_factory(engine)
    return factory, videos, LibraryService(LibraryRepository(factory))


def _add_prefix_task(
    session,
    *,
    video_id: str,
    compact_enabled: bool = True,
    lease_owner: str | None = None,
    lease_expire_time_ms: int | None = None,
    status: str = "COMPLETE",
    version: int = 1,
) -> str:
    task_id = str(uuid4())
    now = now_utc()
    session.add(VideoParseTask(
        id=task_id,
        video_id=video_id,
        pipeline_version="video-parse-v2",
        status=status,
        parse_config={"compact_enabled": compact_enabled},
        chunk_count=1,
        version=version,
        next_execute_time=now,
        attempt_count=1,
        max_attempts=3,
        lease_version=0,
        processing_duration_ms=0,
        processing_timing_complete=True,
        is_test=True,
        is_deleted=False,
        detail={
            "_worker_runtime_v2": {
                "accepted": True,
                "prefix": {
                    "next_chunk_sequence_no": 1,
                    "lease_owner": lease_owner,
                    "lease_version": 4,
                    "lease_expire_time_ms": lease_expire_time_ms,
                    "execution_task_id": "prefix-execution" if lease_owner else None,
                    "next_execute_time_ms": 0,
                    "retry_count": 2,
                    "video_prefix_compact": f"video-{video_id}",
                    "series_prefix_compact": f"series-{video_id}",
                    "last_error": {"message": "old failure"},
                },
                "unrelated": {"preserved": True},
            }
        },
        create_time=now,
        update_time=now,
    ))
    return task_id


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
        session.get(Video, report.video_id).original_source_uri = original.uri
    media = MediaService(library, store, probe_playback=lambda _path: True)

    initial = library.get_series(report.series_id, workspace_id="local-default")
    assert initial.video_count == 1
    assert initial.ready_video_count == 0
    assert initial.videos[0].index_status == VideoIndexStatus.NOT_READY
    assert initial.videos[0].latest_job_id is None

    with factory.begin() as session:
        for chunk in session.scalars(select(VideoChunkEmbedding)):
            chunk.text_vector = [0.1, 0.2]
            chunk.vision_vector = [0.2, 0.3]
    assert library.get_video(report.video_id, workspace_id="local-default").index_status == VideoIndexStatus.SYNCING
    with factory.begin() as session:
        for intent in session.scalars(select(IndexSyncIntent)):
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


def test_v2_library_status_uses_latest_video_parse_task(repository, prepared_import):
    engine, videos = repository
    report = videos.import_video_parse(prepared_import)
    factory = create_session_factory(engine)
    with factory.begin() as session:
        _add_prefix_task(session, video_id=report.video_id)
    library = LibraryService(
        LibraryRepository(factory, execution_mode="v2")
    )

    ready = library.get_series(report.series_id, workspace_id="local-default")
    assert ready.status == "READY"
    assert ready.ready_video_count == 1
    assert ready.videos[0].index_status == VideoIndexStatus.READY
    assert ready.videos[0].parse_status == "COMPLETE"

    with factory.begin() as session:
        _add_prefix_task(
            session,
            video_id=report.video_id,
            status="PENDING",
            version=2,
        )

    reparsing = library.get_series(
        report.series_id,
        workspace_id="local-default",
    )
    assert reparsing.status == "INDEXING"
    assert reparsing.ready_video_count == 0
    assert reparsing.videos[0].index_status == VideoIndexStatus.NOT_READY
    assert reparsing.videos[0].parse_status == "PENDING"


@pytest.mark.parametrize("parse_status", [
    "PENDING", "PREPARING", "PARSING", "INDEXING", "COMPLETE",
    "PARSING_FAILED", "INDEXING_FAILED",
])
def test_v2_library_detail_projects_parse_task_status(
    repository, prepared_import, tmp_path, parse_status,
):
    engine, videos = repository
    report = videos.import_video_parse(prepared_import)
    factory = create_session_factory(engine)
    with factory.begin() as session:
        _add_prefix_task(session, video_id=report.video_id, status=parse_status)
    library = LibraryService(LibraryRepository(factory, execution_mode="v2"))
    media = MediaService(
        library,
        LocalArtifactStore(tmp_path / "artifacts", environment=DatabaseEnvironment.TEST),
    )
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        services=SimpleNamespace(library=library, media=media),
    )))

    detail = library_api.get_series(
        request, series_id=report.series_id, workspace_id="local-default",
    ).model_dump(mode="json", by_alias=True)

    assert detail["videos"][0]["parseStatus"] == parse_status
    if parse_status == "COMPLETE":
        assert detail["videos"][0]["parseProgress"] == 100
    assert detail["readyVideoCount"] == int(parse_status == "COMPLETE")


def test_latest_job_failure_and_index_error_are_actual_and_workspace_scoped(repository, prepared_import):
    factory, videos, library = _library(repository)
    report = videos.import_video_parse(prepared_import)
    now = now_unix_time_ms()
    job_id = str(uuid4())
    with factory.begin() as session:
        session.add(SeriesParseJob(
            job_id=job_id, series_id=report.series_id, video_id=report.video_id,
            pipeline_version="failure-pipeline", status="FAILED", stage="PARSING",
            progress=42, attempt_count=3, max_attempts=3, available_at=now,
            completed_at=now, last_error="private provider endpoint failure",
        ))
        for intent in session.scalars(select(IndexSyncIntent)):
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
        for chunk in session.scalars(select(VideoChunkEmbedding)):
            chunk.text_vector = [0.1, 0.2]
            chunk.vision_vector = [0.2, 0.3]
        for intent in session.scalars(select(IndexSyncIntent)):
            intent.operation = "UPSERT"
            intent.status = "SYNCED"
        session.add(SeriesParseJob(
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
        session.add(SeriesParseJob(
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
            VideoChunkCheckpoint(
                job_id=job_id,
                sequence_no=0,
                video_chunk_id=str(uuid4()),
                stage="PARSED",
                chunk_start_seconds=0,
                chunk_end_seconds=15,
            ),
            VideoChunkCheckpoint(
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
        checkpoint = session.get(VideoChunkCheckpoint, (job_id, 1))
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
    now = now_utc()
    active = []
    for episode, expires in (
        (2, now + timedelta(days=1)),
        (3, now + timedelta(days=1)),
        (4, now - timedelta(seconds=1)),
    ):
        state = uploads.create_session(
            workspace_id="local-default", series_id=series.series_id,
            episode_order=episode,
            original_filename=f"episode-{episode}.mp4", media_type="video/mp4",
            total_size_bytes=10, preferred_segment_size_bytes=4, total_segments=3,
            idempotency_key=f"upload-{episode}", expires_at=expires,
            now=now - timedelta(days=1),
            video_parse_config=default_video_parse_v2_config().model_dump(
                mode="json"
            ),
        )
        if episode != 4:
            active.append(state.upload_session_id)
    detail = library.get_series(series.series_id, workspace_id="local-default")
    assert detail.video_count == 0
    assert detail.pending_upload_count == 2
    assert [item.upload_session_id for item in detail.pending_uploads] == active
    assert [item.upload_sequence for item in detail.pending_uploads] == [2, 3]
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
            session.add(AgentConversation(
                conversation_id=conversation_id, workspace_id=workspace,
                title="Scope history", idempotency_key=conversation_id,
            ))
        session.flush()
        for sequence, conversation, scope, content, created_at in (
            (0, local_conversation, [first.series_id], "first question", now - timedelta_to_ms(timedelta(minutes=2))),
            (1, local_conversation, [second.series_id], "second question", now - timedelta_to_ms(timedelta(minutes=1))),
            (0, foreign_conversation, [first.series_id, second.series_id], "foreign secret", now),
        ):
            session.add(AgentMessage(
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
    now = now_utc()
    with factory.begin() as session:
        session.add_all([
            UploadSession(
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
                expires_at=now + timedelta(days=1),
                completed_at=now,
                video_id=report.video_id,
            ),
            UploadSession(
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
    assert edited.upload_sequence == replay.upload_sequence == 9
    with factory() as session:
        video = session.get(Video, report.video_id)
        upload = session.get(UploadSession, upload_session_id)
        inactive_upload = session.get(
            UploadSession,
            inactive_upload_session_id,
        )
        assert video.episode_order == 9
        assert video.upload_sequence == 9
        assert upload.episode_order == 9
        assert upload.upload_sequence == 9
        assert upload.active_slot_key == f"{report.series_id}:9"
        assert upload.version == 2
        assert inactive_upload.episode_order == 9
        assert inactive_upload.upload_sequence == 9
        assert inactive_upload.active_slot_key is None
        assert inactive_upload.version == 2


@pytest.mark.parametrize(
    ("target_index", "episode_order", "invalidated_indexes"),
    [
        (0, 4, {0, 2}),
        (2, 2, {2}),
    ],
)
def test_episode_edit_atomically_invalidates_prefix_from_earliest_changed_order(
    repository,
    target_index,
    episode_order,
    invalidated_indexes,
) -> None:
    engine, _videos = repository
    factory = create_session_factory(engine)
    series_id = str(uuid4())
    video_ids = [str(uuid4()) for _ in range(3)]
    task_ids: list[str] = []
    with factory.begin() as session:
        session.add(Series(
            series_id=series_id,
            workspace_id="local-default",
            name=f"Episode move {target_index}-{episode_order}",
            description=None,
            status="INDEXING",
            important_extra_data=None,
            character_bible=[],
        ))
        for index, (video_id, order) in enumerate(zip(video_ids, (1, 3, 5))):
            session.add(Video(
                video_id=video_id,
                series_id=series_id,
                episode_order=order,
                upload_sequence=order,
                original_filename=f"episode-{order}.mp4",
                original_source_uri=f"artifact://test/{video_id}.mp4",
                inference_proxy_uri=None,
                content_hash=(str(index + 1) * 64)[:64],
                total_bytes=1,
                duration_seconds=1,
                upload_status="COMPLETED",
                parse_status="SUCCEEDED",
                upload_progress=100,
                parse_progress=100,
            ))
        session.flush()
        for index, video_id in enumerate(video_ids):
            task_ids.append(_add_prefix_task(
                session,
                video_id=video_id,
                compact_enabled=index != 1,
            ))

    library = LibraryRepository(factory)
    library.update_video_episode_order(
        video_ids[target_index],
        workspace_id="local-default",
        episode_order=episode_order,
    )
    with factory() as session:
        first_details = {
            task_id: session.get(VideoParseTask, task_id).detail
            for task_id in task_ids
        }
        assert session.get(
            Video, video_ids[target_index]
        ).episode_order == episode_order
        for index, task_id in enumerate(task_ids):
            runtime = first_details[task_id]["_worker_runtime_v2"]
            prefix = runtime["prefix"]
            if index in invalidated_indexes:
                assert prefix["next_chunk_sequence_no"] == 0
                assert prefix["video_prefix_compact"] == ""
                assert prefix["series_prefix_compact"] == ""
                assert prefix["retry_count"] == 0
                assert prefix["last_error"] is None
                assert prefix["lease_version"] == 5
            else:
                assert prefix["next_chunk_sequence_no"] == 1
                assert prefix["video_prefix_compact"] == f"video-{video_ids[index]}"
                assert prefix["series_prefix_compact"] == f"series-{video_ids[index]}"
                assert prefix["retry_count"] == 2
                assert prefix["lease_version"] == 4
            assert runtime["unrelated"] == {"preserved": True}

    library.update_video_episode_order(
        video_ids[target_index],
        workspace_id="local-default",
        episode_order=episode_order,
    )
    with factory() as session:
        assert {
            task_id: session.get(VideoParseTask, task_id).detail
            for task_id in task_ids
        } == first_details


def test_episode_edit_rolls_back_when_affected_prefix_lease_is_active(
    repository,
) -> None:
    engine, _videos = repository
    factory = create_session_factory(engine)
    series_id = str(uuid4())
    target_video_id = str(uuid4())
    downstream_video_id = str(uuid4())
    upload_session_id = str(uuid4())
    now = now_utc()
    with factory.begin() as session:
        session.add(Series(
            series_id=series_id,
            workspace_id="local-default",
            name="Active Prefix episode move",
            description=None,
            status="INDEXING",
            important_extra_data=None,
            character_bible=[],
        ))
        for video_id, order, content_hash in (
            (target_video_id, 1, "a" * 64),
            (downstream_video_id, 3, "b" * 64),
        ):
            session.add(Video(
                video_id=video_id,
                series_id=series_id,
                episode_order=order,
                upload_sequence=order,
                original_filename=f"episode-{order}.mp4",
                original_source_uri=f"artifact://test/{video_id}.mp4",
                inference_proxy_uri=None,
                content_hash=content_hash,
                total_bytes=1,
                duration_seconds=1,
                upload_status="COMPLETED",
                parse_status="SUCCEEDED",
                upload_progress=100,
                parse_progress=100,
            ))
        session.add(UploadSession(
            upload_session_id=upload_session_id,
            workspace_id="local-default",
            series_id=series_id,
            episode_order=1,
            upload_sequence=1,
            original_filename="episode-1.mp4",
            media_type="video/mp4",
            total_size_bytes=1,
            preferred_segment_size_bytes=1,
            total_segments=1,
            status="COMPLETED",
            idempotency_key="active-prefix-episode-move",
            active_slot_key=f"{series_id}:1",
            expires_at=now + timedelta(days=1),
            completed_at=now,
            video_id=target_video_id,
        ))
        session.flush()
        target_task_id = _add_prefix_task(session, video_id=target_video_id)
        downstream_task_id = _add_prefix_task(
            session,
            video_id=downstream_video_id,
            lease_owner="prefix-worker",
            lease_expire_time_ms=int((now + timedelta(minutes=5)).timestamp() * 1000),
        )

    with factory() as session:
        original_details = {
            task_id: session.get(VideoParseTask, task_id).detail
            for task_id in (target_task_id, downstream_task_id)
        }

    with pytest.raises(VideoEpisodeOrderConflictError, match="active lease"):
        LibraryRepository(factory).update_video_episode_order(
            target_video_id,
            workspace_id="local-default",
            episode_order=2,
        )

    with factory() as session:
        video = session.get(Video, target_video_id)
        upload = session.get(UploadSession, upload_session_id)
        assert video.episode_order == video.upload_sequence == 1
        assert upload.episode_order == upload.upload_sequence == 1
        assert upload.active_slot_key == f"{series_id}:1"
        assert upload.version == 1
        assert {
            task_id: session.get(VideoParseTask, task_id).detail
            for task_id in (target_task_id, downstream_task_id)
        } == original_details


def test_episode_edit_conflicts_roll_back_video_and_linked_upload(
    repository,
    prepared_import,
) -> None:
    factory, videos, library = _library(repository)
    report = videos.import_video_parse(prepared_import)
    now = now_utc()
    linked_upload_id = str(uuid4())
    reserved_upload_id = str(uuid4())
    occupied_video_id = str(uuid4())
    with factory.begin() as session:
        session.add_all([
            UploadSession(
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
                expires_at=now + timedelta(days=1),
                completed_at=now,
                video_id=report.video_id,
            ),
            UploadSession(
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
                expires_at=now + timedelta(days=1),
            ),
            Video(
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
        video = session.get(Video, report.video_id)
        upload = session.get(UploadSession, linked_upload_id)
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
