from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from threading import Event
from uuid import uuid4

import pytest
from sqlalchemy import event, select

from engine.parsev1.runtime import VideoParseRuntimeConfig
from gateway.config import load_config
from server.config.media import ImageUploadConfig
from server.config.parse_profile import ParseConfig
from server.database import DatabaseEnvironment, create_session_factory
from server.model.dto.parseDTO import PreparedEmbedding
from server.model.index_sync import IndexSyncIntentRecord, IndexSyncOperation, IndexSyncStatus
from server.model.job import (
    ParseJobStage,
    ParseJobStatus,
    SeriesParseJobRecord,
    VideoChunkCheckpointRecord,
    VideoChunkCheckpointStage,
)
from server.model.parse_usage import ParseModelCallRecord, ParseUsageAttemptRecord
from server.model.upload import UploadSegmentRecord, UploadSessionRecord, UploadSessionStatus
from server.api.jobs import project_job
from server.api.uploads import project_upload_session
from server.model.dto.jobDTO import ParseJobDetail
from server.model.video import (
    ContextSnapshotRecord,
    SeriesRecord,
    VideoChunkEmbeddingRecord,
    VideoChunkInfoRecord,
    VideoChunkRecord,
    VideoRecord,
)
from server.mq.mysql_queue import MySqlParseJobQueue
from server.repository.upload import (
    UploadCompletionInProgressError,
    UploadConflictError,
    UploadRepository,
    UploadSessionNotFoundError,
    UploadStateError,
)
from server.repository.job import ParseJobRepository
from server.repository.index_sync import IndexSyncRepository
from server.service.upload import (
    UploadChecksumMismatchError,
    UploadService,
    UploadServiceConfig,
)
from server.service.qdrant_mutation_lock import (
    QdrantMutationLock,
    QdrantMutationLockBusyError,
)
from server.storage.artifact import LocalArtifactStore
from server.utils.time import timedelta_to_ms


def _config() -> UploadServiceConfig:
    return UploadServiceConfig(
        preferred_segment_size_bytes=4,
        maximum_upload_size_bytes=1024,
        session_ttl=timedelta(hours=1),
        completion_stale_after=timedelta(minutes=5),
        pipeline_version="pipeline-v1",
        max_job_attempts=3,
    )


def _upload_service(repository, tmp_path, *, clock=None, parse_defaults=None):
    engine, _video_repository = repository
    with engine.begin() as connection:
        connection.execute(
            SeriesRecord.__table__.insert().values(
                series_id="77285f16-3e5b-592e-ba15-8b224b619806",
                workspace_id="local-default",
                name="Test Upload Series",
                description=None,
                status="EMPTY",
                important_extra_data=None,
                character_bible=[],
            )
        )
    store = LocalArtifactStore(tmp_path / "artifacts", environment="test")
    upload_repository = UploadRepository(create_session_factory(engine))
    return (
        engine,
        store,
        upload_repository,
        UploadService(
            upload_repository,
            store,
            config=_config(),
            probe_duration=lambda _path: 12,
            clock=clock,
            parse_defaults=parse_defaults,
            qdrant_mutation_lock=QdrantMutationLock(
                engine,
                environment=DatabaseEnvironment.TEST,
                collection_name="video_chunks_test",
            ),
        ),
    )


def _create(service: UploadService):
    return service.create_session(
        workspace_id="local-default",
        series_id="77285f16-3e5b-592e-ba15-8b224b619806",
        episode_order=7,
        original_filename="episode-07.mp4",
        media_type="video/mp4",
        total_size_bytes=10,
        idempotency_key="upload-episode-07",
    )


def _put(
    service: UploadService,
    upload_session_id: str,
    number: int,
    data: bytes,
    *,
    total_size_bytes: int = 10,
):
    start = number * 4
    return service.upload_segment(
        upload_session_id,
        number,
        data,
        workspace_id="local-default",
        checksum_sha256=hashlib.sha256(data).hexdigest(),
        start_byte=start,
        end_byte_exclusive=start + len(data),
        total_size_bytes=total_size_bytes,
    )


def test_upload_session_is_idempotent_and_reports_progress(
    repository,
    tmp_path,
) -> None:
    engine, _store, _upload_repository, service = _upload_service(
        repository,
        tmp_path,
    )

    first = _create(service)
    second = _create(service)
    _put(service, first.upload_session_id, 1, b"efgh")
    _put(service, first.upload_session_id, 0, b"abcd")
    repeated = _put(service, first.upload_session_id, 0, b"abcd")
    current = service.get_session(
        first.upload_session_id,
        workspace_id="local-default",
    )

    assert first.upload_session_id == second.upload_session_id
    assert first.episode_order == first.upload_sequence == 1
    assert first.total_segments == 3
    assert repeated.segment_number == 0
    assert current.uploaded_segment_count == 2
    assert current.uploaded_size_bytes == 8
    assert service.get_detail(
        first.upload_session_id,
        workspace_id="local-default",
    ).missing_segment_numbers == (2,)
    with engine.connect() as connection:
        assert connection.execute(
            select(UploadSegmentRecord.segment_number)
        ).all() == [(0,), (1,)]


def test_new_upload_and_retry_keep_ready_sibling_searchable(
    repository, prepared_import, tmp_path,
) -> None:
    engine, video_repository = repository
    _engine, _store, _upload_repository, service = _upload_service(repository, tmp_path)
    series_id = "77285f16-3e5b-592e-ba15-8b224b619806"
    video_repository.import_video_parse(replace(
        prepared_import,
        series_id=series_id,
        series_name="Test Upload Series",
        episode_order=6,
        upload_sequence=1,
        chunks=tuple(replace(
            chunk,
            text_embedding=PreparedEmbedding([0.1], {"model": "test"}),
            vision_embedding=PreparedEmbedding([0.2], {"model": "test"}),
        ) for chunk in prepared_import.chunks),
    ))
    session_factory = create_session_factory(engine)
    index_repository = IndexSyncRepository(session_factory)
    for item in index_repository.list_all():
        assert index_repository.mark_synced(
            item.video_chunk_id, item.source_revision, item.operation,
            item.generation,
        )
    with session_factory() as session:
        assert session.get(SeriesRecord, series_id).status == "READY"

    upload = _create(service)
    for number, data in enumerate((b"abcd", b"efgh", b"ij")):
        _put(service, upload.upload_session_id, number, data)
    completed = service.complete(upload.upload_session_id, workspace_id="local-default")
    with session_factory() as session:
        assert session.get(SeriesRecord, series_id).status == "READY"

    with session_factory.begin() as session:
        job = session.get(SeriesParseJobRecord, completed.job_id)
        job.status = ParseJobStatus.FAILED.value
        job.attempt_count = job.max_attempts
        job.version = 7
        session.get(VideoRecord, completed.video_id).parse_status = ParseJobStatus.FAILED.value
    service.retry_parse(
        upload.upload_session_id,
        workspace_id="local-default",
        expected_job_version=7,
    )
    with session_factory() as session:
        assert session.get(SeriesRecord, series_id).status == "READY"


def test_selected_parse_config_survives_upload_completion_and_job_claim(
    repository, tmp_path,
) -> None:
    defaults = ParseConfig.from_defaults(
        runtime=VideoParseRuntimeConfig(), gateway=load_config(),
        upload=_config(), images=ImageUploadConfig(),
        embedding_model="test-embedding", embedding_dimensions=1024,
        embedding_fps=1.0,
    )
    selected = defaults.model_copy(update={
        "window": defaults.window.model_copy(update={"analysis_max_height": 480}),
        "vision": defaults.vision.model_copy(update={"fps": 0.5}),
    })
    engine, _store, _upload_repository, service = _upload_service(
        repository, tmp_path, parse_defaults=defaults,
    )
    request = dict(
        workspace_id="local-default",
        series_id="77285f16-3e5b-592e-ba15-8b224b619806",
        original_filename="episode-07.mp4",
        media_type="video/mp4",
        total_size_bytes=10,
        idempotency_key="configured-upload",
    )
    upload = service.create_session(**request, parse_config=selected)
    assert upload.parse_config == selected.model_dump(mode="json")
    assert service.create_session(**request, parse_config=selected).upload_session_id == upload.upload_session_id
    with pytest.raises(UploadConflictError, match="idempotency"):
        service.create_session(**request, parse_config=defaults)

    default_request = {**request, "idempotency_key": "default-config-upload"}
    default_upload = service.create_session(**default_request)
    service._parse_defaults = selected
    replay = service.create_session(**default_request)
    assert replay.upload_session_id == default_upload.upload_session_id
    assert replay.parse_config == defaults.model_dump(mode="json")

    service._parse_defaults = None
    legacy_request = {**request, "idempotency_key": "legacy-config-upload"}
    legacy_upload = service.create_session(**legacy_request)
    assert legacy_upload.parse_config is None
    service._parse_defaults = defaults
    with pytest.raises(UploadConflictError, match="idempotency"):
        service.create_session(**legacy_request, parse_config=selected)

    _put(service, upload.upload_session_id, 0, b"abcd")
    _put(service, upload.upload_session_id, 1, b"efgh")
    _put(service, upload.upload_session_id, 2, b"ij")
    completed = service.complete(upload.upload_session_id, workspace_id="local-default")
    lease = MySqlParseJobQueue(create_session_factory(engine)).claim_next(
        "configured-worker", lease_duration=timedelta(minutes=5),
    )
    assert lease is not None and lease.job_id == completed.job_id
    assert ParseConfig.model_validate(lease.parse_config) == selected
    assert ParseJobRepository(create_session_factory(engine)).get(completed.job_id).parse_config == upload.parse_config


def test_service_checks_workspace_and_session_range_before_writing_bytes(
    repository,
    tmp_path,
) -> None:
    _engine, store, _upload_repository, service = _upload_service(
        repository,
        tmp_path,
    )
    upload = _create(service)

    with pytest.raises(UploadSessionNotFoundError, match="workspace"):
        service.get_detail(upload.upload_session_id, workspace_id="another-workspace")
    with pytest.raises(UploadSessionNotFoundError, match="workspace"):
        service.complete(upload.upload_session_id, workspace_id="another-workspace")
    for workspace_id, total_size, end_byte, expected_error in (
        ("another-workspace", 10, 4, UploadSessionNotFoundError),
        ("local-default", 11, 4, UploadConflictError),
        ("local-default", 10, 3, UploadConflictError),
    ):
        with pytest.raises(expected_error):
            service.upload_segment(
                upload.upload_session_id,
                0,
                b"abcd",
                workspace_id=workspace_id,
                checksum_sha256=hashlib.sha256(b"abcd").hexdigest(),
                start_byte=0,
                end_byte_exclusive=end_byte,
                total_size_bytes=total_size,
            )
    assert not (
        store.base_directory / "staging" / "uploads" / upload.upload_session_id
    ).exists()


def test_upload_rejects_checksum_mismatch_and_missing_completion(
    repository,
    tmp_path,
) -> None:
    _engine, _store, _upload_repository, service = _upload_service(
        repository,
        tmp_path,
    )
    upload = _create(service)

    with pytest.raises(UploadChecksumMismatchError, match="does not match"):
        service.upload_segment(
            upload.upload_session_id,
            0,
            b"abcd",
            workspace_id="local-default",
            checksum_sha256="0" * 64,
            start_byte=0,
            end_byte_exclusive=4,
            total_size_bytes=10,
        )
    with pytest.raises(UploadStateError, match="missing segments: 0, 1, 2"):
        service.complete(upload.upload_session_id, workspace_id="local-default")


def test_delete_removes_segments_and_unblocks_later_parse_without_reusing_order(repository, tmp_path):
    engine, store, upload_repository, service = _upload_service(repository, tmp_path)
    upload = _create(service)
    _put(service, upload.upload_session_id, 0, b"abcd")
    staging = store.base_directory / "staging" / "uploads" / upload.upload_session_id
    with pytest.raises(UploadSessionNotFoundError, match="workspace"):
        service.delete_session(upload.upload_session_id, workspace_id="other")
    assert staging.exists()
    assert upload_repository.get_session(upload.upload_session_id).status == "UPLOADING"

    later = service.create_session(
        workspace_id="local-default", series_id=upload.series_id,
        original_filename="later.mp4", media_type="video/mp4", total_size_bytes=4,
        idempotency_key="later",
    )
    _put(service, later.upload_session_id, 0, b"efgh", total_size_bytes=4)
    completed = service.complete(later.upload_session_id, workspace_id="local-default")
    session_factory = create_session_factory(engine)
    jobs = ParseJobRepository(session_factory)
    queue = MySqlParseJobQueue(session_factory)
    assert queue.claim_next(
        "test-worker", lease_duration=timedelta(minutes=5),
    ) is None

    deleted = service.delete_session(upload.upload_session_id, workspace_id="local-default")
    repeated = service.delete_session(upload.upload_session_id, workspace_id="local-default")
    assert deleted.status == repeated.status == "EXPIRED"
    assert repeated.version == deleted.version
    assert deleted.uploaded_size_bytes == deleted.uploaded_segment_count == 0
    assert upload_repository.list_segments(upload.upload_session_id) == []
    assert not staging.exists()
    assert service.list_sessions(workspace_id="local-default") == []
    lease = queue.claim_next(
        "test-worker", lease_duration=timedelta(minutes=5),
    )
    assert lease is not None and lease.job_id == completed.job_id
    with pytest.raises(UploadStateError, match="expired"):
        _put(service, upload.upload_session_id, 0, b"abcd")
    with pytest.raises(UploadStateError, match="expired"):
        service.complete(upload.upload_session_id, workspace_id="local-default")
    assert _create(service).status == "EXPIRED"
    next_upload = service.create_session(
        workspace_id="local-default", series_id=upload.series_id,
        original_filename="next.mp4", media_type="video/mp4", total_size_bytes=4,
        idempotency_key="next",
    )
    assert next_upload.episode_order == next_upload.upload_sequence == 3


@pytest.mark.parametrize("delete_before_write", [True, False])
def test_delete_fences_in_flight_segment_and_cleans_late_bytes(repository, tmp_path, monkeypatch, delete_before_write):
    _engine, store, upload_repository, service = _upload_service(repository, tmp_path)
    upload = _create(service)
    put = store.put_upload_segment

    def put_during_delete(*args):
        if delete_before_write:
            service.delete_session(upload.upload_session_id, workspace_id="local-default")
        stored = put(*args)
        if not delete_before_write:
            service.delete_session(upload.upload_session_id, workspace_id="local-default")
        return stored

    monkeypatch.setattr(store, "put_upload_segment", put_during_delete)
    with pytest.raises(UploadStateError, match="expired"):
        _put(service, upload.upload_session_id, 0, b"abcd")
    assert upload_repository.list_segments(upload.upload_session_id) == []
    assert not (store.base_directory / "staging" / "uploads" / upload.upload_session_id).exists()


def test_delete_rejects_completing_and_running_parse(repository, tmp_path):
    engine, store, upload_repository, service = _upload_service(repository, tmp_path)
    upload = _create(service)
    for number, data in enumerate((b"abcd", b"efgh", b"ij")):
        _put(service, upload.upload_session_id, number, data)
    claim = upload_repository.claim_completion(upload.upload_session_id)
    with pytest.raises(UploadCompletionInProgressError):
        service.delete_session(upload.upload_session_id, workspace_id="local-default")
    assert upload_repository.get_session(upload.upload_session_id).status == "COMPLETING"
    assert len(upload_repository.list_segments(upload.upload_session_id)) == 3
    upload_repository.release_completion(upload.upload_session_id, claim.completion_token, error="retry")
    completed = service.complete(upload.upload_session_id, workspace_id="local-default")
    lease = MySqlParseJobQueue(create_session_factory(engine)).claim_next(
        "test-worker",
        lease_duration=timedelta(minutes=5),
    )
    assert lease is not None and lease.job_id == completed.job_id
    with pytest.raises(UploadStateError, match="being parsed"):
        service.delete_session(upload.upload_session_id, workspace_id="local-default")
    with create_session_factory(engine)() as session:
        video = session.get(VideoRecord, completed.video_id)
        assert video is not None
        assert store.resolve(video.original_source_uri).exists()
        assert session.get(UploadSessionRecord, upload.upload_session_id).status == "COMPLETED"


def test_manual_parse_retry_preserves_progress_checkpoints_usage_and_attempt_history(repository, tmp_path):
    retry_at = 1_789_560_000_000
    engine, store, _upload_repository, service = _upload_service(
        repository,
        tmp_path,
        clock=lambda: retry_at,
    )
    upload = _create(service)
    for number, data in enumerate((b"abcd", b"efgh", b"ij")):
        _put(service, upload.upload_session_id, number, data)
    completed = service.complete(upload.upload_session_id, workspace_id="local-default")
    checkpoint_id = str(uuid4())
    started_at = retry_at - timedelta_to_ms(timedelta(minutes=10))
    completed_at = retry_at - timedelta_to_ms(timedelta(minutes=1))
    with create_session_factory(engine).begin() as session:
        job = session.get(SeriesParseJobRecord, completed.job_id)
        job.status = ParseJobStatus.FAILED.value
        job.stage = ParseJobStage.PARSING.value
        job.progress = 18
        job.attempt_count = 3
        job.max_attempts = 3
        job.version = 7
        job.lease_owner = "stale-worker"
        job.lease_expires_at = retry_at - timedelta_to_ms(timedelta(seconds=1))
        job.heartbeat_at = retry_at - timedelta_to_ms(timedelta(seconds=2))
        job.started_at = started_at
        job.completed_at = completed_at
        job.last_error = "provider timed out"
        video = session.get(VideoRecord, completed.video_id)
        video.parse_status = ParseJobStatus.FAILED.value
        video.parse_progress = 18
        session.get(SeriesRecord, upload.series_id).status = "FAILED"
        session.add(VideoChunkCheckpointRecord(
            job_id=completed.job_id,
            sequence_no=0,
            video_chunk_id=checkpoint_id,
            stage=VideoChunkCheckpointStage.PARSED.value,
            parse_artifact_uri="artifact://test/jobs/parse.json",
        ))
        session.add(ParseUsageAttemptRecord(
            job_id=completed.job_id,
            attempt_count=3,
            lease_version=3,
            started_at=started_at,
            recording_complete=True,
        ))
    work_file = store.work_directory(completed.job_id) / "resume.json"
    work_file.write_text("keep", encoding="utf-8")

    job_id = service.retry_parse(
        upload.upload_session_id,
        workspace_id="local-default",
        expected_job_version=7,
    )

    assert job_id == completed.job_id
    session_factory = create_session_factory(engine)
    jobs = ParseJobRepository(session_factory)
    queue = MySqlParseJobQueue(session_factory)
    retried = jobs.get(completed.job_id)
    assert retried.status == ParseJobStatus.QUEUED.value
    assert (retried.attempt_count, retried.max_attempts, retried.version) == (3, 4, 8)
    assert (retried.stage, retried.progress) == (ParseJobStage.PARSING.value, 18)
    assert retried.available_at == retry_at
    assert retried.started_at == started_at
    assert retried.completed_at is None and retried.last_error is None
    assert retried.lease_owner is None
    assert retried.lease_expires_at is None and retried.heartbeat_at is None
    assert retried.is_primary_upload_job
    public = project_job(ParseJobDetail(retried, tuple(jobs.list_checkpoints(completed.job_id))))
    assert public.can_retry is False
    assert public.checkpoints[0].video_chunk_id == checkpoint_id
    assert work_file.read_text(encoding="utf-8") == "keep"
    with create_session_factory(engine)() as session:
        assert session.get(ParseUsageAttemptRecord, (completed.job_id, 3, 3)) is not None
        video = session.get(VideoRecord, completed.video_id)
        assert video.parse_status == ParseJobStatus.QUEUED.value
        assert video.parse_progress == 18
        assert session.get(SeriesRecord, upload.series_id).status == "INDEXING"

    with pytest.raises(UploadConflictError, match="version changed"):
        service.retry_parse(
            upload.upload_session_id,
            workspace_id="local-default",
            expected_job_version=7,
        )
    claimed = queue.claim_next(
        "retry-worker",
        lease_duration=timedelta(minutes=5),
        now=retry_at,
    )
    assert claimed is not None
    assert claimed.job_id == completed.job_id
    assert (claimed.attempt_count, claimed.max_attempts) == (4, 4)


def test_manual_parse_retry_requires_the_uploads_failed_primary_job(repository, tmp_path):
    engine, _store, _upload_repository, service = _upload_service(repository, tmp_path)
    upload = _create(service)
    for number, data in enumerate((b"abcd", b"efgh", b"ij")):
        _put(service, upload.upload_session_id, number, data)
    completed = service.complete(upload.upload_session_id, workspace_id="local-default")

    with pytest.raises(UploadSessionNotFoundError, match="workspace"):
        service.retry_parse(
            upload.upload_session_id,
            workspace_id="other-workspace",
            expected_job_version=1,
        )
    with pytest.raises(UploadStateError, match="failed ParseJob"):
        service.retry_parse(
            upload.upload_session_id,
            workspace_id="local-default",
            expected_job_version=1,
        )

    with create_session_factory(engine).begin() as session:
        primary = session.get(SeriesParseJobRecord, completed.job_id)
        primary.status = ParseJobStatus.FAILED.value
        primary.attempt_count = primary.max_attempts
        primary.last_error = "failed"
        secondary_id = str(uuid4())
        session.add(SeriesParseJobRecord(
            job_id=secondary_id,
            series_id=upload.series_id,
            video_id=completed.video_id,
            pipeline_version="secondary-pipeline",
            status=ParseJobStatus.FAILED.value,
            stage=ParseJobStage.PARSING.value,
            progress=1,
            attempt_count=1,
            max_attempts=1,
            version=1,
            available_at=1_789_560_000_000,
            lease_version=0,
            last_error="secondary failed",
        ))

    jobs = ParseJobRepository(create_session_factory(engine))
    primary = jobs.get(completed.job_id)
    secondary = jobs.get(secondary_id)
    assert project_job(ParseJobDetail(primary, ())).can_retry is True
    assert primary.upload_session_id == upload.upload_session_id
    assert project_job(ParseJobDetail(secondary, ())).can_retry is False
    assert secondary.upload_session_id == upload.upload_session_id


def test_full_reparse_clears_checkpoints_but_preserves_successful_snapshot(
    repository,
    tmp_path,
):
    reparsed_at = 1_789_646_400_000
    current_time = [reparsed_at]
    engine, store, _upload_repository, service = _upload_service(
        repository,
        tmp_path,
        clock=lambda: current_time[0],
    )
    upload = _create(service)
    for number, data in enumerate((b"abcd", b"efgh", b"ij")):
        _put(service, upload.upload_session_id, number, data)
    completed = service.complete(
        upload.upload_session_id,
        workspace_id="local-default",
    )
    video_chunk_id = str(uuid4())
    with create_session_factory(engine).begin() as session:
        job = session.get(SeriesParseJobRecord, completed.job_id)
        job.status = ParseJobStatus.SUCCEEDED.value
        job.stage = ParseJobStage.COMPLETED.value
        job.progress = 100
        job.attempt_count = 3
        job.max_attempts = 3
        job.version = 7
        job.started_at = reparsed_at - timedelta_to_ms(timedelta(hours=1))
        job.completed_at = reparsed_at - timedelta_to_ms(timedelta(minutes=30))
        video = session.get(VideoRecord, completed.video_id)
        video.parse_status = ParseJobStatus.SUCCEEDED.value
        video.parse_progress = 100
        session.get(SeriesRecord, upload.series_id).status = "READY"
        session.add(VideoChunkRecord(
            video_chunk_id=video_chunk_id,
            video_id=completed.video_id,
            sequence_no=0,
            start_seconds=0,
            end_seconds=12,
            analysis_window_start_seconds=0,
            analysis_window_end_seconds=12,
            chunk_media_ref="artifact://test/old-chunk.mp4",
            analysis_window_media_ref="artifact://test/old-window.mp4",
            transcript=[],
            baseline_visual_caption=None,
            keyframe_uris=[],
        ))
        session.add(VideoChunkEmbeddingRecord(
            video_chunk_id=video_chunk_id,
            text_input="old searchable facts",
            text_vector=[0.1],
            vision_vector=[0.2],
        ))
        session.add(IndexSyncIntentRecord(
            video_chunk_id=video_chunk_id,
            operation=IndexSyncOperation.UPSERT.value,
            source_revision="old-revision",
            generation=1,
            status=IndexSyncStatus.SYNCED.value,
            attempt_count=0,
            available_at=reparsed_at,
            synced_at=reparsed_at,
        ))
        session.add(VideoChunkCheckpointRecord(
            job_id=completed.job_id,
            sequence_no=0,
            video_chunk_id=video_chunk_id,
            stage=VideoChunkCheckpointStage.INDEXED.value,
            chunk_start_seconds=0,
            chunk_end_seconds=12,
        ))
        session.add(ParseUsageAttemptRecord(
            job_id=completed.job_id,
            attempt_count=3,
            lease_version=3,
            started_at=reparsed_at - timedelta_to_ms(timedelta(hours=1)),
            recording_complete=True,
        ))
    old_attempt_artifact = (
        store.work_directory(completed.job_id) / "old-attempt.json"
    )
    old_attempt_artifact.write_text("keep", encoding="utf-8")

    assert service.reparse(
        upload.upload_session_id,
        workspace_id="local-default",
        expected_job_version=7,
    ) == completed.job_id

    session_factory = create_session_factory(engine)
    jobs = ParseJobRepository(session_factory)
    queue = MySqlParseJobQueue(session_factory)
    reset = jobs.get(completed.job_id)
    assert (reset.status, reset.stage, reset.progress, reset.version) == (
        ParseJobStatus.QUEUED.value,
        ParseJobStage.QUEUED.value,
        0,
        8,
    )
    assert (reset.attempt_count, reset.max_attempts) == (3, 6)
    assert reset.started_at is None and reset.completed_at is None
    assert jobs.list_checkpoints(completed.job_id) == []
    public = project_job(ParseJobDetail(reset, ()))
    assert public.can_retry is False
    assert public.can_reparse is False
    with session_factory() as session:
        assert session.get(VideoChunkRecord, video_chunk_id) is not None
        assert session.get(VideoChunkEmbeddingRecord, video_chunk_id) is not None
        intent = session.get(IndexSyncIntentRecord, video_chunk_id)
        assert intent.status == IndexSyncStatus.SYNCED.value
        assert session.get(
            ParseUsageAttemptRecord,
            (completed.job_id, 3, 3),
        ) is not None
        video = session.get(VideoRecord, completed.video_id)
        assert (video.parse_status, video.parse_progress) == (
            ParseJobStatus.SUCCEEDED.value,
            100,
        )
        assert session.get(SeriesRecord, upload.series_id).status == "READY"
    assert old_attempt_artifact.read_text(encoding="utf-8") == "keep"

    lease = queue.claim_next(
        "reparse-worker",
        lease_duration=timedelta(minutes=5),
        now=reparsed_at,
    )
    assert lease is not None
    assert lease.preserve_existing_result is True
    assert (lease.attempt_count, lease.max_attempts) == (4, 6)
    assert queue.retry(
        lease,
        error="replacement parse failed",
        retry_at=reparsed_at + timedelta_to_ms(timedelta(minutes=1)),
        duration_ms=0,
        now=reparsed_at + timedelta_to_ms(timedelta(seconds=10)),
    )
    with session_factory() as session:
        video = session.get(VideoRecord, completed.video_id)
        assert video.parse_status == ParseJobStatus.SUCCEEDED.value
        assert session.get(SeriesRecord, upload.series_id).status == "READY"

    second_lease = queue.claim_next(
        "reparse-worker",
        lease_duration=timedelta(minutes=5),
        now=reparsed_at + timedelta_to_ms(timedelta(minutes=1)),
    )
    assert second_lease is not None
    assert second_lease.preserve_existing_result is True
    assert queue.reap_expired(
        retry_delay=timedelta(minutes=1),
        now=reparsed_at + timedelta_to_ms(timedelta(minutes=7)),
    ) == 1
    with session_factory() as session:
        video = session.get(VideoRecord, completed.video_id)
        assert video.parse_status == ParseJobStatus.SUCCEEDED.value
        assert session.get(SeriesRecord, upload.series_id).status == "READY"

    final_lease = queue.claim_next(
        "reparse-worker",
        lease_duration=timedelta(minutes=5),
        now=reparsed_at + timedelta_to_ms(timedelta(minutes=8)),
    )
    assert final_lease is not None
    assert final_lease.attempt_count == 6
    assert queue.retry(
        final_lease,
        error="replacement parse exhausted",
        retry_at=reparsed_at + timedelta_to_ms(timedelta(minutes=9)),
        duration_ms=0,
        now=reparsed_at + timedelta_to_ms(timedelta(minutes=8, seconds=10)),
    )
    failed_reparse = jobs.get(completed.job_id)
    assert failed_reparse.status == ParseJobStatus.FAILED.value
    assert failed_reparse.processing_timing_complete is False
    current_time[0] = reparsed_at + timedelta_to_ms(timedelta(minutes=9))
    service.retry_parse(
        upload.upload_session_id,
        workspace_id="local-default",
        expected_job_version=failed_reparse.version,
    )
    with session_factory() as session:
        video = session.get(VideoRecord, completed.video_id)
        assert video.parse_status == ParseJobStatus.SUCCEEDED.value
        assert session.get(SeriesRecord, upload.series_id).status == "READY"

    with pytest.raises(UploadConflictError, match="version changed"):
        service.reparse(
            upload.upload_session_id,
            workspace_id="local-default",
            expected_job_version=7,
        )


def test_delete_completed_upload_removes_full_task_aggregate(repository, tmp_path):
    engine, store, upload_repository, service = _upload_service(repository, tmp_path)
    upload = _create(service)
    for number, data in enumerate((b"abcd", b"efgh", b"ij")):
        _put(service, upload.upload_session_id, number, data)
    completed = service.complete(upload.upload_session_id, workspace_id="local-default")
    now = 1_789_560_000_000
    video_chunk_id = str(uuid4())
    call_id = str(uuid4())
    snapshot_id = str(uuid4())
    second_job_id = str(uuid4())
    with create_session_factory(engine).begin() as session:
        job = session.get(SeriesParseJobRecord, completed.job_id)
        job.status = ParseJobStatus.FAILED.value
        job.last_error = "fixture failure"
        session.add(SeriesParseJobRecord(
            job_id=second_job_id,
            series_id=upload.series_id,
            video_id=completed.video_id,
            pipeline_version="pipeline-v2",
            status=ParseJobStatus.FAILED.value,
            stage=ParseJobStage.PARSING.value,
            progress=40,
            attempt_count=1,
            max_attempts=3,
            version=1,
            available_at=now,
            lease_version=0,
            last_error="second pipeline failure",
        ))
        session.add(VideoChunkCheckpointRecord(
            job_id=completed.job_id,
            sequence_no=0,
            video_chunk_id=video_chunk_id,
            stage=VideoChunkCheckpointStage.INDEXED.value,
        ))
        session.add(VideoChunkRecord(
            video_chunk_id=video_chunk_id,
            video_id=completed.video_id,
            sequence_no=0,
            start_seconds=0,
            end_seconds=1,
            analysis_window_start_seconds=0,
            analysis_window_end_seconds=1,
            chunk_media_ref="artifact://test/chunk.mp4",
            analysis_window_media_ref="artifact://test/window.mp4",
            transcript=[],
            baseline_visual_caption=None,
            keyframe_uris=[],
        ))
        session.add(VideoChunkInfoRecord(
            video_chunk_id=video_chunk_id,
            all_time_events=[],
            all_time_event_text="",
            video_compact="compact",
            context_before_compact="",
            context_after_compact="compact",
            window_summary="summary",
            pipeline_version="pipeline-v1",
            is_fallback=False,
            parsed_at=now,
        ))
        session.add(VideoChunkEmbeddingRecord(
            video_chunk_id=video_chunk_id,
            text_input="summary",
            text_vector=[0.1],
            vision_vector=[0.2],
        ))
        session.add(ContextSnapshotRecord(
            snapshot_id=snapshot_id,
            engine_snapshot_id="engine-snapshot",
            series_id=upload.series_id,
            video_id=completed.video_id,
            after_chunk_sequence_no=0,
            compact_text="compact",
            structured_state={},
            created_from_video_chunk_id=video_chunk_id,
            created_at=now,
        ))
        session.add(IndexSyncIntentRecord(
            video_chunk_id=video_chunk_id,
            operation=IndexSyncOperation.UPSERT.value,
            source_revision="revision",
            generation=1,
            status=IndexSyncStatus.SYNCED.value,
            attempt_count=1,
            available_at=now,
            synced_at=now,
        ))
        session.add(ParseUsageAttemptRecord(
            job_id=completed.job_id,
            attempt_count=1,
            lease_version=1,
            started_at=now,
            recording_complete=True,
        ))
        session.add(ParseModelCallRecord(
            call_id=call_id,
            job_id=completed.job_id,
            attempt_count=1,
            lease_version=1,
            workspace_id="local-default",
            series_id=upload.series_id,
            video_id=completed.video_id,
            sequence_no=0,
            video_chunk_id=video_chunk_id,
            provider="test",
            model="test-model",
            category="parse",
            operation="parse",
            started_at=now,
            status="failed",
        ))

    job_directory = store.work_directory(completed.job_id)
    (job_directory / "checkpoint.json").write_text("{}", encoding="utf-8")
    second_job_directory = store.work_directory(second_job_id)
    (second_job_directory / "checkpoint.json").write_text("{}", encoding="utf-8")
    video_directory = (
        store.base_directory / "media" / "series" / upload.series_id
        / "videos" / completed.video_id
    )
    assert video_directory.exists()
    assert ParseJobRepository(create_session_factory(engine)).get(
        second_job_id
    ).upload_session_id == upload.upload_session_id

    class VectorStore:
        def __init__(self):
            self.deleted: list[str] = []

        def delete(self, video_chunk_id: str) -> None:
            self.deleted.append(video_chunk_id)

    vectors = VectorStore()
    service._video_chunk_store = vectors
    deleted = service.delete_session(
        upload.upload_session_id,
        workspace_id="local-default",
    )
    repeated = service.delete_session(
        upload.upload_session_id,
        workspace_id="local-default",
    )

    assert deleted.status == repeated.status == UploadSessionStatus.EXPIRED.value
    assert repeated.version == deleted.version
    assert deleted.video_id is None and deleted.job_id is None
    assert vectors.deleted == [video_chunk_id]
    assert not job_directory.exists()
    assert not second_job_directory.exists()
    assert not video_directory.exists()
    with create_session_factory(engine)() as session:
        tombstone = session.get(UploadSessionRecord, upload.upload_session_id)
        assert tombstone is not None
        assert tombstone.video_id is None and tombstone.job_id is None
        for model, key in (
            (VideoRecord, completed.video_id),
            (SeriesParseJobRecord, completed.job_id),
            (SeriesParseJobRecord, second_job_id),
            (VideoChunkRecord, video_chunk_id),
            (VideoChunkCheckpointRecord, (completed.job_id, 0)),
            (VideoChunkInfoRecord, video_chunk_id),
            (VideoChunkEmbeddingRecord, video_chunk_id),
            (ContextSnapshotRecord, snapshot_id),
            (IndexSyncIntentRecord, video_chunk_id),
            (ParseUsageAttemptRecord, (completed.job_id, 1, 1)),
            (ParseModelCallRecord, call_id),
        ):
            assert session.get(model, key) is None
        assert session.get(SeriesRecord, upload.series_id).status == "EMPTY"


def test_completed_upload_cleanup_failure_retains_retry_identity(repository, tmp_path, monkeypatch):
    engine, store, _upload_repository, service = _upload_service(repository, tmp_path)
    upload = _create(service)
    for number, data in enumerate((b"abcd", b"efgh", b"ij")):
        _put(service, upload.upload_session_id, number, data)
    completed = service.complete(upload.upload_session_id, workspace_id="local-default")
    cleanup_video = store.delete_video_artifacts

    def fail_video_cleanup(_series_id, _video_id):
        raise PermissionError("locked")

    monkeypatch.setattr(store, "delete_video_artifacts", fail_video_cleanup)
    with pytest.raises(PermissionError, match="locked"):
        service.delete_session(upload.upload_session_id, workspace_id="local-default")
    with create_session_factory(engine)() as session:
        tombstone = session.get(UploadSessionRecord, upload.upload_session_id)
        assert tombstone.status == UploadSessionStatus.EXPIRED.value
        assert tombstone.video_id == completed.video_id
        assert tombstone.job_id == completed.job_id
        assert session.get(VideoRecord, completed.video_id) is not None
        assert session.get(SeriesParseJobRecord, completed.job_id) is not None

    monkeypatch.setattr(store, "delete_video_artifacts", cleanup_video)
    assert service.reconcile_deletions() == (1, 0)
    with create_session_factory(engine)() as session:
        assert session.get(VideoRecord, completed.video_id) is None
        assert session.get(SeriesParseJobRecord, completed.job_id) is None


def test_reconcile_resumes_active_upload_cleanup_after_begin_crash(repository, tmp_path):
    engine, store, upload_repository, service = _upload_service(repository, tmp_path)
    upload = _create(service)
    _put(service, upload.upload_session_id, 0, b"abcd")
    staging = store.base_directory / "staging" / "uploads" / upload.upload_session_id
    assert staging.exists()

    deletion = upload_repository.begin_delete_session(
        upload.upload_session_id,
        workspace_id="local-default",
        now=1_789_560_000_000,
    )
    assert deletion.session.status == UploadSessionStatus.EXPIRED.value
    with create_session_factory(engine)() as session:
        tombstone = session.get(UploadSessionRecord, upload.upload_session_id)
        assert tombstone.last_error_code == "DELETION_PENDING"

    assert service.reconcile_deletions() == (1, 0)
    assert not staging.exists()
    with create_session_factory(engine)() as session:
        tombstone = session.get(UploadSessionRecord, upload.upload_session_id)
        assert tombstone.last_error_code is None
        assert tombstone.status == UploadSessionStatus.EXPIRED.value


def test_delete_rejects_an_inconsistent_shared_video_reference(repository, tmp_path):
    engine, _store, _upload_repository, service = _upload_service(repository, tmp_path)
    upload = _create(service)
    for number, data in enumerate((b"abcd", b"efgh", b"ij")):
        _put(service, upload.upload_session_id, number, data)
    completed = service.complete(upload.upload_session_id, workspace_id="local-default")
    shared_upload_id = str(uuid4())
    with create_session_factory(engine).begin() as session:
        original = session.get(UploadSessionRecord, upload.upload_session_id)
        session.add(UploadSessionRecord(
            upload_session_id=shared_upload_id,
            workspace_id=original.workspace_id,
            series_id=original.series_id,
            episode_order=original.episode_order + 1,
            upload_sequence=original.upload_sequence + 1,
            original_filename=original.original_filename,
            media_type=original.media_type,
            total_size_bytes=original.total_size_bytes,
            preferred_segment_size_bytes=original.preferred_segment_size_bytes,
            total_segments=original.total_segments,
            content_sha256=original.content_sha256,
            version=1,
            status=UploadSessionStatus.COMPLETED.value,
            idempotency_key="inconsistent-shared-video",
            request_fingerprint="shared",
            expires_at=original.expires_at,
            completed_at=original.completed_at,
            video_id=completed.video_id,
            job_id=completed.job_id,
        ))

    with pytest.raises(UploadConflictError, match="another upload session"):
        service.delete_session(upload.upload_session_id, workspace_id="local-default")
    with create_session_factory(engine)() as session:
        assert session.get(VideoRecord, completed.video_id) is not None
        assert session.get(SeriesParseJobRecord, completed.job_id) is not None
        assert session.get(UploadSessionRecord, upload.upload_session_id).status == "COMPLETED"


def test_failed_completion_removes_unregistered_published_video(repository, tmp_path):
    _engine, store, upload_repository, _service = _upload_service(repository, tmp_path)
    service = UploadService(
        upload_repository,
        store,
        config=_config(),
        probe_duration=lambda _path: (_ for _ in ()).throw(RuntimeError("probe failed")),
    )
    upload = _create(service)
    for number, data in enumerate((b"abcd", b"efgh", b"ij")):
        _put(service, upload.upload_session_id, number, data)

    with pytest.raises(RuntimeError, match="probe failed"):
        service.complete(upload.upload_session_id, workspace_id="local-default")

    videos_directory = (
        store.base_directory / "media" / "series" / upload.series_id / "videos"
    )
    assert not videos_directory.exists() or list(videos_directory.iterdir()) == []
    assert service.get_session(
        upload.upload_session_id,
        workspace_id="local-default",
    ).status == UploadSessionStatus.UPLOADING.value


def test_delete_cleanup_failure_can_be_retried(repository, tmp_path, monkeypatch):
    _engine, store, _upload_repository, service = _upload_service(repository, tmp_path)
    upload = _create(service)
    _put(service, upload.upload_session_id, 0, b"abcd")
    cleanup = store.delete_upload_staging

    def fail_cleanup(_session_id):
        raise PermissionError("locked")

    monkeypatch.setattr(store, "delete_upload_staging", fail_cleanup)
    with pytest.raises(PermissionError):
        service.delete_session(upload.upload_session_id, workspace_id="local-default")
    monkeypatch.setattr(store, "delete_upload_staging", cleanup)
    assert service.delete_session(upload.upload_session_id, workspace_id="local-default").status == "EXPIRED"
    assert not (store.base_directory / "staging" / "uploads" / upload.upload_session_id).exists()


def test_deleting_segments_from_an_expired_session_advances_its_visible_version(repository, tmp_path):
    now = 1_789_560_000_000
    _engine, _store, _repo, service = _upload_service(repository, tmp_path, clock=lambda: now)
    upload = _create(service)
    _put(service, upload.upload_session_id, 0, b"abcd")
    now += timedelta_to_ms(timedelta(hours=2))
    expired = service.get_session(upload.upload_session_id, workspace_id="local-default")
    assert expired.status == "EXPIRED" and expired.uploaded_size_bytes == 4
    deleted = service.delete_session(upload.upload_session_id, workspace_id="local-default")
    assert deleted.version > expired.version
    assert deleted.uploaded_size_bytes == 0
    assert service.delete_session(upload.upload_session_id, workspace_id="local-default").version == deleted.version


def test_complete_creates_video_and_durable_parse_job_once(
    repository,
    tmp_path,
) -> None:
    engine, store, _upload_repository, service = _upload_service(
        repository,
        tmp_path,
    )
    upload = _create(service)
    _put(service, upload.upload_session_id, 2, b"ij")
    _put(service, upload.upload_session_id, 0, b"abcd")
    _put(service, upload.upload_session_id, 1, b"efgh")

    first = service.complete(upload.upload_session_id, workspace_id="local-default")
    second = service.complete(upload.upload_session_id, workspace_id="local-default")

    assert first.video_id == second.video_id
    assert first.job_id == second.job_id
    assert first.session.status == UploadSessionStatus.COMPLETED.value
    with create_session_factory(engine)() as session:
        video = session.scalar(select(VideoRecord))
        job = session.scalar(select(SeriesParseJobRecord))
    assert video is not None
    assert job is not None
    assert video.original_filename == "episode-07.mp4"
    assert video.episode_order == video.upload_sequence == 1
    assert video.content_hash == hashlib.sha256(b"abcdefghij").hexdigest()
    assert video.duration_seconds == 12
    assert video.parse_status == ParseJobStatus.QUEUED.value
    assert store.resolve(video.original_source_uri).read_bytes() == b"abcdefghij"
    assert job.video_id == video.video_id
    assert job.status == ParseJobStatus.QUEUED.value
    assert job.attempt_count == 0


def test_expired_session_rejects_new_segments(repository, tmp_path) -> None:
    now = 1_789_387_200_000
    _engine, store, upload_repository, service = _upload_service(
        repository,
        tmp_path,
        clock=lambda: now,
    )
    upload = _create(service)
    expired_service = UploadService(
        upload_repository,
        store,
        config=_config(),
        probe_duration=lambda _path: 12,
        clock=lambda: now + timedelta_to_ms(timedelta(hours=2)),
    )

    with pytest.raises(UploadStateError, match="expired"):
        _put(expired_service, upload.upload_session_id, 0, b"abcd")
    assert (
        upload_repository.get_session(upload.upload_session_id).status
        == UploadSessionStatus.EXPIRED.value
    )
    renewed = expired_service.create_session(
        workspace_id="local-default",
        series_id="77285f16-3e5b-592e-ba15-8b224b619806",
        episode_order=7,
        original_filename="episode-07.mp4",
        media_type="video/mp4",
        total_size_bytes=10,
        idempotency_key="upload-episode-07-retry",
    )
    assert renewed.upload_session_id != upload.upload_session_id
    assert renewed.status == UploadSessionStatus.UPLOADING.value
    assert renewed.episode_order == renewed.upload_sequence == upload.episode_order + 1


@pytest.mark.parametrize("content_sha256", ["0" * 64, hashlib.sha256(b"abcd").hexdigest()])
def test_source_checksum_and_visible_completion_versions(repository, tmp_path, content_sha256):
    _engine, _store, upload_repository, service = _upload_service(repository, tmp_path)
    upload = service.create_session(
        workspace_id="local-default", series_id="77285f16-3e5b-592e-ba15-8b224b619806",
        episode_order=7, original_filename="episode.mp4", media_type="video/mp4",
        total_size_bytes=4, idempotency_key="checksum", content_sha256=content_sha256,
    )
    assert upload.upload_sequence == 1
    _put(service, upload.upload_session_id, 0, b"abcd", total_size_bytes=4)
    received = service.get_session(upload.upload_session_id, workspace_id="local-default")
    assert received.version > upload.version
    _put(service, upload.upload_session_id, 0, b"abcd", total_size_bytes=4)
    assert service.get_session(upload.upload_session_id, workspace_id="local-default").version == received.version
    if content_sha256 == "0" * 64:
        with pytest.raises(UploadChecksumMismatchError):
            service.complete(upload.upload_session_id, workspace_id="local-default")
        current = service.get_session(upload.upload_session_id, workspace_id="local-default")
        assert current.status == "UPLOADING" and current.missing_segment_numbers == ()
        assert current.job_id is None and current.completion_stage is None
        assert current.version > received.version
        public = project_upload_session(current)
        assert public.last_error.code == "CHECKSUM_MISMATCH"
        assert public.completion_progress is public.estimated_remaining_ms is None
    else:
        completed = service.complete(upload.upload_session_id, workspace_id="local-default")
        assert completed.session.job_id == completed.job_id
        assert completed.session.version > received.version
        assert completed.session.content_sha256 == content_sha256
        assert completed.session.completion_stage is None
        replay = service.complete(upload.upload_session_id, workspace_id="local-default")
        assert replay.session.version == completed.session.version
        assert replay.job_id == completed.job_id


def test_auto_upload_sequence_is_stable_on_replay_and_increases(repository, tmp_path):
    _engine, _store, _repo, service = _upload_service(repository, tmp_path)
    first = _create(service)
    second = service.create_session(
        workspace_id="local-default", series_id=first.series_id, episode_order=8,
        original_filename="episode-08.mp4", media_type="video/mp4", total_size_bytes=4,
        idempotency_key="second-upload",
    )
    assert second.upload_sequence == first.upload_sequence + 1
    assert second.episode_order == second.upload_sequence == 2
    assert _create(service).upload_sequence == first.upload_sequence


@pytest.mark.parametrize("first_optional,changed_optional", [
    ({"content_sha256": "0" * 64}, {}),
    ({}, {"content_sha256": hashlib.sha256(b"abcd").hexdigest()}),
])
def test_upload_key_keeps_optional_original_request_parameters(repository, tmp_path, first_optional, changed_optional):
    _engine, _store, _repo, service = _upload_service(repository, tmp_path)
    request = {
        "workspace_id": "local-default", "series_id": "77285f16-3e5b-592e-ba15-8b224b619806",
        "episode_order": 7, "original_filename": "episode.mp4", "media_type": "video/mp4",
        "total_size_bytes": 4, "idempotency_key": "optional-parameters",
    }
    first = service.create_session(**request, **first_optional)
    if not first_optional and "content_sha256" in changed_optional:
        _put(service, first.upload_session_id, 0, b"abcd", total_size_bytes=4)
        service.complete(first.upload_session_id, workspace_id="local-default")
    with pytest.raises(UploadConflictError):
        service.create_session(**request, **changed_optional)
    replay = service.create_session(**request, **first_optional)
    assert replay.upload_session_id == first.upload_session_id


@pytest.mark.parametrize("orders", [
    {"episode_order": 99, "upload_sequence": 3},
    {"episode_order": None, "upload_sequence": None},
    {"episode_order": -1, "upload_sequence": 0},
])
def test_client_order_is_ignored_for_allocation_and_replay(repository, tmp_path, orders):
    _engine, _store, _repo, service = _upload_service(repository, tmp_path)
    request = {
        "workspace_id": "local-default", "series_id": "77285f16-3e5b-592e-ba15-8b224b619806",
        "original_filename": "episode-99.mp4", "media_type": "video/mp4",
        "total_size_bytes": 4, "idempotency_key": "ignored-order",
    }
    first = service.create_session(**request, **orders)
    replay = service.create_session(**request, episode_order=1000, upload_sequence=1000)
    next_upload = service.create_session(**{**request, "idempotency_key": "next"})
    assert first.episode_order == first.upload_sequence == 1
    assert replay.upload_session_id == first.upload_session_id
    assert replay.episode_order == replay.upload_sequence == 1
    assert next_upload.episode_order == next_upload.upload_sequence == 2


def test_delete_lock_contention_does_not_start_task_cleanup(repository, tmp_path):
    engine, store, upload_repository, service = _upload_service(repository, tmp_path)
    upload = _create(service)
    _put(service, upload.upload_session_id, 0, b"abcd")
    staging = store.base_directory / "staging" / "uploads" / upload.upload_session_id
    holder = QdrantMutationLock(
        engine,
        environment=DatabaseEnvironment.TEST,
        collection_name="video_chunks_test",
    )
    service._qdrant_mutation_lock = QdrantMutationLock(
        engine,
        environment=DatabaseEnvironment.TEST,
        collection_name="video_chunks_test",
        acquire_timeout_seconds=0,
    )
    lock_acquired = Event()
    release_lock = Event()

    def hold_lock() -> None:
        with holder.hold():
            lock_acquired.set()
            assert release_lock.wait(2)

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(hold_lock)
        assert lock_acquired.wait(1)
        try:
            with pytest.raises(QdrantMutationLockBusyError, match="retry"):
                service.delete_session(
                    upload.upload_session_id,
                    workspace_id="local-default",
                )
        finally:
            release_lock.set()
        future.result(timeout=1)

    current = upload_repository.get_session(upload.upload_session_id)
    assert current.status == UploadSessionStatus.UPLOADING.value
    assert current.uploaded_segment_count == 1
    assert staging.exists()


@pytest.mark.parametrize("highest_field", [
    "upload_episode_order", "upload_sequence", "video_episode_order", "video_upload_sequence",
])
def test_append_order_uses_all_legacy_order_fields(repository, tmp_path, highest_field):
    engine, _store, _repo, service = _upload_service(repository, tmp_path)
    first = _create(service)
    with create_session_factory(engine).begin() as session:
        upload = session.get(UploadSessionRecord, first.upload_session_id)
        upload.episode_order = 9 if highest_field == "upload_episode_order" else 3
        upload.upload_sequence = 9 if highest_field == "upload_sequence" else 4
        upload.active_slot_key = f"{first.series_id}:{upload.episode_order}"
        session.add(VideoRecord(
            video_id="5772855a-8c93-440c-aae1-91c9cf9eebf5", series_id=first.series_id,
            episode_order=9 if highest_field == "video_episode_order" else 5,
            upload_sequence=9 if highest_field == "video_upload_sequence" else 6,
            original_filename="legacy.mp4", original_source_uri="artifact://test/legacy.mp4",
            content_hash="0" * 64, total_bytes=4, duration_seconds=1,
            upload_status="COMPLETED", parse_status="SUCCEEDED", upload_progress=100, parse_progress=100,
        ))
    next_upload = service.create_session(
        workspace_id="local-default", series_id=first.series_id,
        original_filename="second.mp4", media_type="video/mp4", total_size_bytes=4,
        idempotency_key="second",
    )
    assert next_upload.episode_order == next_upload.upload_sequence == 10
    with create_session_factory(engine)() as session:
        assert session.get(UploadSessionRecord, first.upload_session_id).episode_order == upload.episode_order


@pytest.mark.parametrize("legacy_sequence", [None, 5])
def test_legacy_fingerprint_keeps_replay_without_accepting_file_changes(repository, tmp_path, legacy_sequence):
    engine, _store, _repo, service = _upload_service(repository, tmp_path)
    request = {
        "workspace_id": "local-default", "series_id": "77285f16-3e5b-592e-ba15-8b224b619806",
        "original_filename": "legacy.mp4", "media_type": "video/mp4",
        "total_size_bytes": 4, "idempotency_key": "legacy", "content_sha256": None,
    }
    first = service.create_session(**request)
    legacy_fields = {**request, "episode_order": 7, "upload_sequence": legacy_sequence}
    with create_session_factory(engine).begin() as session:
        upload = session.get(UploadSessionRecord, first.upload_session_id)
        upload.episode_order = 7
        upload.upload_sequence = 5
        upload.active_slot_key = f"{first.series_id}:7"
        upload.request_fingerprint = hashlib.sha256(json.dumps(legacy_fields, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    replay = service.create_session(**request, episode_order=999, upload_sequence=999)
    assert replay.upload_session_id == first.upload_session_id
    assert replay.episode_order == 7 and replay.upload_sequence == 5
    with pytest.raises(UploadConflictError):
        service.create_session(**{**request, "total_size_bytes": 8})


def test_legacy_session_without_fingerprint_compares_file_fields(repository, tmp_path):
    engine, store, upload_repository, service = _upload_service(repository, tmp_path)
    first = _create(service)
    with create_session_factory(engine).begin() as session:
        upload = session.get(UploadSessionRecord, first.upload_session_id)
        upload.request_fingerprint = None
    changed_config_service = UploadService(
        upload_repository, store, config=replace(_config(), preferred_segment_size_bytes=2),
    )
    request = {
        "workspace_id": "local-default", "series_id": first.series_id,
        "original_filename": first.original_filename, "media_type": first.media_type,
        "total_size_bytes": first.total_size_bytes, "idempotency_key": "upload-episode-07",
    }
    replay = changed_config_service.create_session(**request, episode_order=999, upload_sequence=999)
    assert replay.upload_session_id == first.upload_session_id
    assert replay.preferred_segment_size_bytes == first.preferred_segment_size_bytes
    for changed in ({"original_filename": "changed.mp4"}, {"total_size_bytes": 8}, {"content_sha256": "0" * 64}):
        with pytest.raises(UploadConflictError):
            changed_config_service.create_session(**{**request, **changed})


@pytest.mark.parametrize("elapsed_hours", [0, 2])
def test_replay_and_expiration_do_not_lock_series(repository, tmp_path, elapsed_hours):
    now = 1_789_387_200_000
    engine, store, upload_repository, service = _upload_service(repository, tmp_path, clock=lambda: now)
    first = _create(service)
    replay_service = UploadService(
        upload_repository, store, config=_config(),
        clock=lambda: now + timedelta_to_ms(timedelta(hours=elapsed_hours)),
    )
    statements = []

    def record_sql(_connection, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement.lower())

    event.listen(engine, "before_cursor_execute", record_sql)
    try:
        replay = _create(replay_service)
    finally:
        event.remove(engine, "before_cursor_execute", record_sql)
    assert replay.upload_session_id == first.upload_session_id
    assert replay.episode_order == replay.upload_sequence == first.episode_order
    assert any("from upload_session" in statement for statement in statements)
    assert not any("from series" in statement for statement in statements)
    if elapsed_hours:
        assert replay.status == UploadSessionStatus.EXPIRED.value
        assert any("update upload_session" in statement for statement in statements)
    else:
        assert replay.status == UploadSessionStatus.UPLOADING.value
