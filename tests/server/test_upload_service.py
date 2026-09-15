from __future__ import annotations

import hashlib
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from server.database import create_session_factory
from server.model.job import ParseJobStatus, SeriesParseJobRecord
from server.model.upload import UploadSegmentRecord, UploadSessionStatus
from server.model.video import SeriesRecord, VideoRecord
from server.repository.upload import (
    UploadConflictError,
    UploadRepository,
    UploadSessionNotFoundError,
    UploadStateError,
)
from server.service.upload import (
    UploadChecksumMismatchError,
    UploadService,
    UploadServiceConfig,
)
from server.storage.artifact import LocalArtifactStore


def _config() -> UploadServiceConfig:
    return UploadServiceConfig(
        preferred_segment_size_bytes=4,
        maximum_upload_size_bytes=1024,
        session_ttl=timedelta(hours=1),
        completion_stale_after=timedelta(minutes=5),
        pipeline_version="pipeline-v1",
        max_job_attempts=3,
    )


def _upload_service(repository, tmp_path, *, clock=None):
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
            probe_duration=lambda _path: 12_345,
            clock=clock,
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
        total_size_bytes=10,
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
    assert video.content_hash == hashlib.sha256(b"abcdefghij").hexdigest()
    assert video.duration_ms == 12_345
    assert video.parse_status == ParseJobStatus.QUEUED.value
    assert store.resolve(video.original_source_uri).read_bytes() == b"abcdefghij"
    assert job.video_id == video.video_id
    assert job.status == ParseJobStatus.QUEUED.value
    assert job.attempt_count == 0


def test_expired_session_rejects_new_segments(repository, tmp_path) -> None:
    now = datetime(2026, 9, 14, 12, 0, 0)
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
        probe_duration=lambda _path: 12_345,
        clock=lambda: now + timedelta(hours=2),
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
