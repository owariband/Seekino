from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import datetime, timedelta

import pytest
from sqlalchemy import event, select

from server.database import create_session_factory
from server.model.job import ParseJobStatus, SeriesParseJobRecord
from server.model.upload import UploadSegmentRecord, UploadSessionRecord, UploadSessionStatus
from server.api.uploads import project_upload_session
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
    assert video.episode_order == video.upload_sequence == 1
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
            content_hash="0" * 64, total_bytes=4, duration_ms=1000,
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
    now = datetime(2026, 9, 14, 12, 0, 0)
    engine, store, upload_repository, service = _upload_service(repository, tmp_path, clock=lambda: now)
    first = _create(service)
    replay_service = UploadService(
        upload_repository, store, config=_config(),
        clock=lambda: now + timedelta(hours=elapsed_hours),
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
