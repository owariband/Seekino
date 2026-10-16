from __future__ import annotations

import hashlib
import json
from dataclasses import fields, replace
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from pydantic import ValidationError
from sqlalchemy import event, select

from agent.video_parse_v2.input import ParseConfig as VideoParseV2Config
from engine.common.config import VideoParseRuntimeConfig
from engine.rag import EncoderContract
from gateway.ark.embedding import DOUBAO_MULTIMODAL_EMBEDDING_REVISION
from gateway.config import load_config
from server.config.media import ImageUploadConfig
from server.config.mq import MQConsumerConfig
from server.config.parse_profile import ParseConfig, default_video_parse_v2_config, index_contract_fingerprint
from server.constants.parse import MAX_INDEX_GENERATION, PREPARE_FAILURES_KEY, RUNTIME_DETAIL_KEY
from server.model.dto.uploadDTO import UploadReparseRequest, UploadSessionCreateRequest, UploadSessionState
from server.database import create_session_factory
from server.model.dto.parseDTO import PreparedEmbedding
from server.model.index_sync import IndexSyncIntent, IndexSyncOperation, IndexSyncStatus
from server.model.job import (
    ChunkParseContent,
    ChunkParseTask,
    IntervalParseContent,
    IntervalParseTask,
    ParseJobStage,
    ParseJobStatus,
    SeriesParseJob,
    VideoParseTask,
    VideoChunkCheckpoint,
    VideoChunkCheckpointStage,
)
from server.model.parse_usage import ParseModelCall, ParseUsageAttempt
from server.model.upload import UploadSegment, UploadSession, UploadSessionStatus
from server.api.uploads import project_upload_session
from server.model.video import (
    ContextSnapshot,
    Series,
    VideoChunkEmbedding,
    VideoChunkInfo,
    VideoChunk,
    Video,
)
from server.mq.mysql_queue import MySqlParseJobQueue
from server.mq.video_parse_queue import MySqlVideoParseQueue
from server.repository.upload import (
    UploadCompletionInProgressError,
    UploadConflictError,
    UploadRepository,
    UploadSessionNotFoundError,
    UploadStateError,
)
from server.repository.job import ParseJobRepository
from server.repository.index_sync import IndexSyncRepository
from server.repository.video_parse import VideoParseRepository
from server.service.upload import (
    UploadChecksumMismatchError,
    UploadService,
    UploadServiceConfig,
)
from server.storage.artifact import LocalArtifactStore
from server.utils.time import (
    datetime_to_unix_time_ms,
    timedelta_to_ms,
    unix_time_ms_to_datetime,
)


def _with_historical_v1_job(engine, completed, profile):
    """Seed retained V1 history; new uploads never create legacy jobs."""
    job_id = str(uuid4())
    with create_session_factory(engine).begin() as session:
        upload = session.get(UploadSession, completed.session.upload_session_id)
        session.add(SeriesParseJob(
            id=job_id, creator_id=upload.creator_id,
            series_id=upload.series_id, video_id=completed.video_id,
            pipeline_version="pipeline-v1", parse_config=profile.model_dump(mode="json"),
            status=ParseJobStatus.QUEUED.value, stage=ParseJobStage.QUEUED.value,
            progress=0, attempt_count=0, max_attempts=3, version=1,
            next_execute_time=datetime_to_unix_time_ms(completed.session.updated_at),
            lease_version=0,
        ))
        session.flush()
        upload.job_id = job_id
    return replace(completed, job_id=job_id, session=replace(completed.session, job_id=job_id))


def _config() -> UploadServiceConfig:
    return UploadServiceConfig(
        preferred_segment_size_bytes=4,
        maximum_upload_size_bytes=1024,
        session_ttl=timedelta(hours=1),
        completion_stale_after=timedelta(minutes=5),
        pipeline_version="pipeline-v1",
        max_job_attempts=3,
    )


def _upload_service(
    repository,
    tmp_path,
    *,
    clock=None,
    parse_defaults=None,
    video_parse_index_store=None,
    probe_duration=None,
):
    engine, _video_repository = repository
    with engine.begin() as connection:
        connection.execute(
            Series.__table__.insert().values(
                id="77285f16-3e5b-592e-ba15-8b224b619806",
                tenant_id="local-default",
                name="Test Upload Series",
                description=None,
                status="EMPTY",
                important_extra_data=None,
                character_bible=[],
            )
        )
    store = LocalArtifactStore(tmp_path / "artifacts", environment="test")
    upload_repository = UploadRepository(create_session_factory(engine))
    if parse_defaults is None:
        gateway = load_config()
        parse_defaults = ParseConfig.from_defaults(
            runtime=VideoParseRuntimeConfig(), gateway=gateway,
            upload=_config(), images=ImageUploadConfig(), embedding_model=None,
            embedding_dimensions=(video_parse_index_store.semantic_dimensions
                                  if video_parse_index_store is not None
                                  else gateway.resolve_embedding_binding().dimensions),
            embedding_fps=None,
        )
    active_video_parse_config = parse_defaults.to_video_parse_v2()
    active_embedding = active_video_parse_config.models.embedding
    return (
        engine,
        store,
        upload_repository,
        UploadService(
            upload_repository,
            store,
            config=_config(),
            probe_duration=probe_duration or (lambda _path: 12_000),
            clock=clock,
            parse_defaults=parse_defaults,
            video_parse_index_store=video_parse_index_store,
            video_parse_query_encoder=EncoderContract(
                provider="ark",
                model=active_embedding.model,
                encoder_revision=DOUBAO_MULTIMODAL_EMBEDDING_REVISION,
                dimensions=active_embedding.dimensions,
            ),
            video_parse_query_encoder_source=active_embedding.source,
        ),
    )


class _VideoParseIndexStore:
    def __init__(self, semantic_dimensions: int, vision_dimensions: int) -> None:
        self.semantic_dimensions = semantic_dimensions
        self.vision_dimensions = vision_dimensions


@pytest.mark.parametrize(("chunk_duration", "expected_count"), [(8, 2), (60, 1)])
def test_v2_upload_preserves_fractional_media_duration_with_seconds_configuration(
    repository, tmp_path, chunk_duration, expected_count,
):
    engine, _store, _repository, service = _upload_service(
        repository, tmp_path, probe_duration=lambda _path: 8008,
    )
    upload = service.create_session(
        tenant_id="local-default", series_id="77285f16-3e5b-592e-ba15-8b224b619806",
        episode_order=1, original_filename="fractional.mp4", media_type="video/mp4",
        total_size_bytes=10, idempotency_key="fractional-duration",
        parse_config=default_video_parse_v2_config().model_copy(update={
            "chunk_duration": chunk_duration, "interval_duration": chunk_duration * 3,
        }),
    )
    for number, data in enumerate((b"abcd", b"efgh", b"ij")):
        _put(service, upload.upload_session_id, number, data)
    completed = service.complete(upload.upload_session_id, tenant_id="local-default")
    with create_session_factory(engine)() as session:
        video = session.get(Video, completed.video_id)
        task = session.get(VideoParseTask, completed.video_parse_task_id)
        assert video.duration_ms == 8008
        assert task.chunk_count == expected_count
        assert task.parse_config["chunk_duration"] == chunk_duration


def _video_parse_config_with_dimensions(dimensions: int) -> VideoParseV2Config:
    config = default_video_parse_v2_config()
    embedding = config.models.embedding.model_copy(
        update={"dimensions": dimensions}
    )
    models = config.models.model_copy(update={"embedding": embedding})
    return config.model_copy(update={"models": models})


def _video_parse_config_with_model(
    model: str,
    *,
    dimensions: int,
) -> VideoParseV2Config:
    config = _video_parse_config_with_dimensions(dimensions)
    embedding = config.models.embedding.model_copy(update={"model": model})
    return config.model_copy(update={
        "models": config.models.model_copy(update={"embedding": embedding})
    })


@pytest.mark.parametrize("encoder_model", [None, "doubao-embedding-vision-250615"])
def test_upload_freezes_endpoint_encoder_identity_without_changing_its_route(
    repository, tmp_path, encoder_model,
):
    _engine, _store, _repository, service = _upload_service(
        repository, tmp_path, video_parse_index_store=_VideoParseIndexStore(2, 2),
    )
    config = _video_parse_config_with_model("ep-custom", dimensions=2)
    config = config.model_copy(update={"models": config.models.model_copy(update={
        "embedding": config.models.embedding.model_copy(update={"encoder_model": encoder_model}),
    })})
    session = service.create_session(
        tenant_id="local-default", series_id="77285f16-3e5b-592e-ba15-8b224b619806",
        episode_order=7, original_filename="episode-07.mp4", media_type="video/mp4",
        total_size_bytes=10, idempotency_key="endpoint-identity", parse_config=config,
    )
    frozen = session.parse_config["models"]["embedding"]
    assert frozen["model"] == "ep-custom"
    assert frozen["encoder_model"] == "doubao-embedding-vision-250615"
    assert config.models.embedding.encoder_model == encoder_model
    assert service.get_session(session.upload_session_id, tenant_id="local-default").parse_config == session.parse_config


def test_upload_rejects_endpoint_bound_to_a_different_encoder(repository, tmp_path):
    _engine, _store, _repository, service = _upload_service(
        repository, tmp_path, video_parse_index_store=_VideoParseIndexStore(2, 2),
    )
    config = _video_parse_config_with_model("ep-custom", dimensions=2)
    config = config.model_copy(update={"models": config.models.model_copy(update={
        "embedding": config.models.embedding.model_copy(update={
            "encoder_model": "doubao-embedding-vision-251215",
        }),
    })})
    with pytest.raises(ValueError, match="exact active V2 query encoder"):
        service.create_session(
            tenant_id="local-default", series_id="77285f16-3e5b-592e-ba15-8b224b619806",
            episode_order=7, original_filename="episode-07.mp4", media_type="video/mp4",
            total_size_bytes=10, idempotency_key="wrong-endpoint-identity", parse_config=config,
        )
    assert service.list_sessions(tenant_id="local-default") == []


@pytest.mark.parametrize(
    ("parse_config", "error"),
    [
        (_video_parse_config_with_dimensions(3), "active Elasticsearch"),
        (
            _video_parse_config_with_model("another-embedding", dimensions=2),
            "exact active V2 query encoder",
        ),
    ],
)
def test_upload_rejects_v2_encoder_incompatible_with_active_elasticsearch(
    repository,
    tmp_path,
    parse_config,
    error,
) -> None:
    _engine, _store, _repository, service = _upload_service(
        repository,
        tmp_path,
        video_parse_index_store=_VideoParseIndexStore(2, 2),
    )

    with pytest.raises(ValueError, match=error):
        service.create_session(
            tenant_id="local-default",
            series_id="77285f16-3e5b-592e-ba15-8b224b619806",
            episode_order=7,
            original_filename="episode-07.mp4",
            media_type="video/mp4",
            total_size_bytes=10,
            idempotency_key="incompatible-es-dimensions",
            parse_config=parse_config,
        )

    assert service.list_sessions(tenant_id="local-default") == []


def test_upload_completion_rechecks_legacy_v2_dimensions_before_enqueue(
    repository,
    tmp_path,
) -> None:
    engine, _store, _repository, service = _upload_service(
        repository,
        tmp_path,
    )
    upload = service.create_session(
        tenant_id="local-default",
        series_id="77285f16-3e5b-592e-ba15-8b224b619806",
        episode_order=7,
        original_filename="episode-07.mp4",
        media_type="video/mp4",
        total_size_bytes=10,
        idempotency_key="legacy-incompatible-es-dimensions",
        parse_config=_video_parse_config_with_dimensions(3),
    )
    for number, data in enumerate((b"abcd", b"efgh", b"ij")):
        _put(service, upload.upload_session_id, number, data)
    service._video_parse_index_store = _VideoParseIndexStore(2, 2)

    with pytest.raises(ValueError, match="active Elasticsearch"):
        service.complete(
            upload.upload_session_id,
            tenant_id="local-default",
        )

    with create_session_factory(engine)() as session:
        stored = session.get(UploadSession, upload.upload_session_id)
        assert stored.status == UploadSessionStatus.UPLOADING.value
        assert stored.initial_task_id is None


def test_pending_takeover_rechecks_v2_dimensions_before_accepting(
    repository,
    tmp_path,
) -> None:
    engine, _store, _repository, service = _upload_service(
        repository,
        tmp_path,
    )
    upload = service.create_session(
        tenant_id="local-default",
        series_id="77285f16-3e5b-592e-ba15-8b224b619806",
        episode_order=7,
        original_filename="episode-07.mp4",
        media_type="video/mp4",
        total_size_bytes=10,
        idempotency_key="pending-incompatible-es-dimensions",
        parse_config=_video_parse_config_with_dimensions(3),
    )
    for number, data in enumerate((b"abcd", b"efgh", b"ij")):
        _put(service, upload.upload_session_id, number, data)
    completed = service.complete(
        upload.upload_session_id,
        tenant_id="local-default",
    )
    with create_session_factory(engine).begin() as session:
        task = session.get(VideoParseTask, completed.video_parse_task_id)
        task.detail = {RUNTIME_DETAIL_KEY: {"accepted": False, "execution_mode": "v1"}}
    service._video_parse_index_store = _VideoParseIndexStore(2, 2)

    with pytest.raises(ValueError, match="active Elasticsearch"):
        service.reparse(
            upload.upload_session_id,
            tenant_id="local-default",
            expected_job_version=1,
        )

    with create_session_factory(engine)() as session:
        task = session.get(
            VideoParseTask,
            completed.video_parse_task_id,
        )
        assert task.version == 1
        assert task.detail["_worker_runtime_v2"]["accepted"] is False


@pytest.mark.parametrize(
    ("parse_config", "error"),
    [
        (_video_parse_config_with_dimensions(3), "active Elasticsearch"),
        (
            _video_parse_config_with_model("another-embedding", dimensions=2),
            "exact active V2 query encoder",
        ),
    ],
)
def test_reparse_rejects_v2_encoder_before_creating_a_new_task(
    repository,
    tmp_path,
    parse_config,
    error,
) -> None:
    engine, _store, _repository, service = _upload_service(
        repository,
        tmp_path,
        video_parse_index_store=_VideoParseIndexStore(2, 2),
    )
    upload = service.create_session(
        tenant_id="local-default",
        series_id="77285f16-3e5b-592e-ba15-8b224b619806",
        episode_order=7,
        original_filename="episode-07.mp4",
        media_type="video/mp4",
        total_size_bytes=10,
        idempotency_key="reparse-incompatible-es-dimensions",
        parse_config=_video_parse_config_with_dimensions(2),
    )
    for number, data in enumerate((b"abcd", b"efgh", b"ij")):
        _put(service, upload.upload_session_id, number, data)
    completed = service.complete(
        upload.upload_session_id,
        tenant_id="local-default",
    )
    with create_session_factory(engine).begin() as session:
        task = session.get(
            VideoParseTask,
            completed.video_parse_task_id,
        )
        task.status = "COMPLETE"

    with pytest.raises(ValueError, match=error):
        service.reparse(
            upload.upload_session_id,
            tenant_id="local-default",
            expected_job_version=1,
            parse_config=parse_config,
        )

    with create_session_factory(engine)() as session:
        tasks = list(session.scalars(
            select(VideoParseTask).where(
                VideoParseTask.video_id == completed.video_id
            )
        ))
        assert [task.id for task in tasks] == [completed.video_parse_task_id]


def test_reparse_without_config_inherits_the_latest_task_and_freezes_endpoint_identity(
    repository, tmp_path,
):
    engine, _store, _repository, service = _upload_service(
        repository, tmp_path, video_parse_index_store=_VideoParseIndexStore(2, 2),
    )
    upload = service.create_session(
        tenant_id="local-default", series_id="77285f16-3e5b-592e-ba15-8b224b619806",
        episode_order=7, original_filename="episode-07.mp4", media_type="video/mp4",
        total_size_bytes=10, idempotency_key="inherit-latest-config",
        parse_config=_video_parse_config_with_dimensions(2),
    )
    for number, data in enumerate((b"abcd", b"efgh", b"ij")):
        _put(service, upload.upload_session_id, number, data)
    completed = service.complete(upload.upload_session_id, tenant_id="local-default")
    factory = create_session_factory(engine)
    with factory.begin() as session:
        session.get(VideoParseTask, completed.video_parse_task_id).status = "COMPLETE"

    replacement = _video_parse_config_with_model("ep-replacement", dimensions=2)
    replacement = replacement.model_copy(update={
        "understanding_enabled": False,
        "batch_max_size": replacement.batch_max_size + 1,
        "execution": replacement.execution.model_copy(update={
            "max_retry_count": 4, "max_model_calls": 17,
        }),
    })
    second = service.reparse(
        upload.upload_session_id, tenant_id="local-default", expected_job_version=1,
        parse_config=replacement,
    )
    expected = replacement.model_dump(mode="json")
    expected["models"]["embedding"]["encoder_model"] = "doubao-embedding-vision-250615"
    expected["indexing"]["contract_fingerprint"] = index_contract_fingerprint(
        replacement.models.embedding.model_copy(update={"encoder_model": "doubao-embedding-vision-250615"}),
        provider="ark", encoder_revision=DOUBAO_MULTIMODAL_EMBEDDING_REVISION,
        max_height=replacement.visual_input.max_height,
    )
    assert second.version == 2
    assert second.parse_config == expected
    assert replacement.models.embedding.encoder_model is None
    with factory.begin() as session:
        session.get(VideoParseTask, second.video_parse_task_id).status = "COMPLETE"

    third = service.reparse(
        upload.upload_session_id, tenant_id="local-default", expected_job_version=2,
    )
    assert third.version == 3
    assert third.parse_config == second.parse_config
    assert third.max_attempts == 5
    with factory() as session:
        tasks = list(session.scalars(select(VideoParseTask).where(
            VideoParseTask.video_id == completed.video_id,
        ).order_by(VideoParseTask.version)))
        assert [task.version for task in tasks] == [1, 2, 3]
        assert tasks[0].parse_config == upload.parse_config
        assert tasks[1].parse_config == expected
        assert tasks[2].parse_config == expected
        assert session.get(UploadSession, upload.upload_session_id).parse_config == upload.parse_config


@pytest.mark.parametrize("status", ["INDEXING_FAILED", "PENDING"])
def test_reparse_freezes_a_legacy_endpoint_snapshot_after_selecting_the_latest_task(
    repository, tmp_path, status,
):
    engine, _store, _repository, service = _upload_service(
        repository, tmp_path, video_parse_index_store=_VideoParseIndexStore(2, 2),
    )
    upload = service.create_session(
        tenant_id="local-default", series_id="77285f16-3e5b-592e-ba15-8b224b619806",
        episode_order=7, original_filename="episode-07.mp4", media_type="video/mp4",
        total_size_bytes=10, idempotency_key="legacy-reparse-config",
        parse_config=_video_parse_config_with_dimensions(2),
    )
    for number, data in enumerate((b"abcd", b"efgh", b"ij")):
        _put(service, upload.upload_session_id, number, data)
    completed = service.complete(upload.upload_session_id, tenant_id="local-default")
    factory = create_session_factory(engine)
    with factory.begin() as session:
        task = session.get(VideoParseTask, completed.video_parse_task_id)
        legacy = json.loads(json.dumps(task.parse_config))
        legacy["models"]["embedding"]["model"] = "ep-legacy"
        legacy["models"]["embedding"].pop("encoder_model")
        legacy["understanding_enabled"] = False
        task.parse_config = legacy
        task.status = status
        task.detail = {RUNTIME_DETAIL_KEY: {"accepted": False, "execution_mode": "v1"}}

    reparsed = service.reparse(
        upload.upload_session_id, tenant_id="local-default", expected_job_version=1,
    )
    expected = json.loads(json.dumps(legacy))
    expected["models"]["embedding"]["encoder_model"] = "doubao-embedding-vision-250615"
    expected["indexing"]["contract_fingerprint"] = index_contract_fingerprint(
        VideoParseV2Config.model_validate(expected).models.embedding.model_copy(update={"encoder_model": "doubao-embedding-vision-250615"}),
        provider="ark", encoder_revision=DOUBAO_MULTIMODAL_EMBEDDING_REVISION,
        max_height=VideoParseV2Config.model_validate(expected).visual_input.max_height,
    )
    assert reparsed.parse_config == expected
    assert reparsed.detail["_worker_runtime_v2"]["accepted"] is True
    assert reparsed.version == (1 if status == "PENDING" else 2)
    with factory() as session:
        original = session.get(VideoParseTask, completed.video_parse_task_id)
        assert original.parse_config == (expected if status == "PENDING" else legacy)
        assert session.get(UploadSession, upload.upload_session_id).parse_config == upload.parse_config
        tasks = list(session.scalars(select(VideoParseTask).where(
            VideoParseTask.video_id == completed.video_id,
        )))
        assert len(tasks) == (1 if status == "PENDING" else 2)


def test_v2_execution_mode_creates_only_an_accepted_v2_task(
    repository,
    tmp_path,
) -> None:
    engine, _store, _repository, service = _upload_service(
        repository,
        tmp_path,
    )
    upload = _create(service)
    for number, data in enumerate((b"abcd", b"efgh", b"ij")):
        _put(service, upload.upload_session_id, number, data)

    completed = service.complete(
        upload.upload_session_id,
        tenant_id="local-default",
    )
    repeated = service.complete(
        upload.upload_session_id,
        tenant_id="local-default",
    )

    assert completed.job_id is repeated.job_id is None
    assert repeated.video_parse_task_id == completed.video_parse_task_id
    with create_session_factory(engine)() as session:
        assert list(session.scalars(select(SeriesParseJob))) == []
        task = session.get(VideoParseTask, completed.video_parse_task_id)
        assert task is not None
        assert task.detail["_worker_runtime_v2"] == {
            "accepted": True,
            "execution_mode": "v2",
        }


def _create(service: UploadService):
    return service.create_session(
        tenant_id="local-default",
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
        tenant_id="local-default",
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
        tenant_id="local-default",
    )

    assert first.upload_session_id == second.upload_session_id
    assert first.episode_order == first.upload_sequence == 7
    assert first.total_segments == 3
    assert repeated.segment_number == 0
    assert current.uploaded_segment_count == 2
    assert current.uploaded_size_bytes == 8
    assert service.get_detail(
        first.upload_session_id,
        tenant_id="local-default",
    ).missing_segment_numbers == (2,)
    with engine.connect() as connection:
        assert connection.execute(
            select(UploadSegment.segment_number)
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
        assert session.get(Series, series_id).status == "READY"

    upload = _create(service)
    for number, data in enumerate((b"abcd", b"efgh", b"ij")):
        _put(service, upload.upload_session_id, number, data)
    completed = service.complete(upload.upload_session_id, tenant_id="local-default")
    with session_factory() as session:
        assert session.get(Series, series_id).status == "READY"

    with session_factory.begin() as session:
        task = session.get(VideoParseTask, completed.video_parse_task_id)
        task.status = "PARSING_FAILED"
        task.attempt_count = task.max_attempts
        task.last_error = {"code": "MODEL_ERROR"}
    service.retry_parse(
        upload.upload_session_id,
        tenant_id="local-default",
        expected_job_version=1, expected_lease_version=0,
    )
    with session_factory() as session:
        assert session.get(Series, series_id).status == "READY"


def test_legacy_parse_execution_resolves_defaults_without_changing_request_fingerprint(
    repository, tmp_path,
):
    defaults = ParseConfig.from_defaults(
        runtime=VideoParseRuntimeConfig(batch_max_size=7, dispatch_interval_seconds=0.25),
        mq=MQConsumerConfig(
            consumer_id="test", retry_base=timedelta(seconds=9), retry_max=timedelta(seconds=81),
        ),
        gateway=load_config(), upload=_config(), images=ImageUploadConfig(),
        embedding_model="test-embedding", embedding_dimensions=1024, embedding_fps=1.0,
    )
    raw = defaults.model_dump(mode="json")
    for name in (
        "batch_max_size", "dispatch_interval_seconds",
        "retry_initial_delay_seconds", "retry_max_delay_seconds",
    ):
        raw["execution"].pop(name)
    legacy = ParseConfig.model_validate(raw)
    assert legacy.model_dump(mode="json") == raw
    serialized = legacy.model_dump(by_alias=True)["execution"]
    assert serialized["requestTimeoutSeconds"] == raw["execution"]["request_timeout_seconds"]
    assert serialized["maxAttempts"] == raw["execution"]["max_attempts"]
    assert "retryInitialDelaySeconds" not in serialized
    assert "retryMaxDelaySeconds" not in serialized
    assert serialized["parseConcurrency"] == raw["execution"]["parse_concurrency"]
    with pytest.raises(ValueError, match="must be resolved"):
        legacy.to_video_parse_v2()
    _engine, _store, _repository, service = _upload_service(
        repository, tmp_path, parse_defaults=defaults,
    )
    request = dict(
        tenant_id="local-default", series_id="77285f16-3e5b-592e-ba15-8b224b619806",
        episode_order=7, original_filename="episode-07.mp4", media_type="video/mp4",
        total_size_bytes=10, idempotency_key="legacy-execution-policy", parse_config=legacy,
    )
    upload = service.create_session(**request)
    assert upload.parse_config == defaults.to_video_parse_v2().model_dump(mode="json")
    assert legacy.model_dump(mode="json") == raw

    service._parse_defaults = defaults.model_copy(update={
        "execution": defaults.execution.model_copy(update={"batch_max_size": 5}),
    })
    replay = service.create_session(**request)
    assert replay.upload_session_id == upload.upload_session_id
    assert replay.parse_config == upload.parse_config
    with pytest.raises(ValueError, match="defaults"):
        legacy.resolve_execution(None)

    raw["execution"]["retry_initial_delay_seconds"] = 100
    with pytest.raises(ValueError, match="retry_max_delay_seconds"):
        service.create_session(**{
            **request, "episode_order": 8, "idempotency_key": "invalid-execution-policy",
            "parse_config": ParseConfig.model_validate(raw),
        })


def test_partial_retry_policy_replay_preserves_snapshot_after_defaults_change(
    repository, tmp_path,
):
    defaults = ParseConfig.from_defaults(
        runtime=VideoParseRuntimeConfig(), gateway=load_config(),
        mq=MQConsumerConfig(consumer_id="test"),
        upload=_config(), images=ImageUploadConfig(),
        embedding_model="test-embedding", embedding_dimensions=1024, embedding_fps=1.0,
    )
    raw = defaults.model_dump(mode="json")
    for name in (
        "batch_max_size", "dispatch_interval_seconds",
        "retry_initial_delay_seconds", "retry_max_delay_seconds",
    ):
        raw["execution"].pop(name)
    raw["execution"]["retry_max_delay_seconds"] = 60
    partial = ParseConfig.model_validate(raw)
    _engine, _store, _repository, service = _upload_service(
        repository, tmp_path, parse_defaults=defaults,
    )
    request = dict(
        tenant_id="local-default", series_id="77285f16-3e5b-592e-ba15-8b224b619806",
        episode_order=7, original_filename="episode-07.mp4", media_type="video/mp4",
        total_size_bytes=10, idempotency_key="partial-retry-policy", parse_config=partial,
    )
    upload = service.create_session(**request)
    assert upload.parse_config["execution"]["retry_initial_delay_seconds"] == 30
    assert upload.parse_config["execution"]["retry_max_delay_seconds"] == 60

    service._parse_defaults = defaults.model_copy(update={
        "execution": defaults.execution.model_copy(update={"retry_initial_delay_seconds": 90}),
    })
    replay = service.create_session(**request)
    assert replay.upload_session_id == upload.upload_session_id
    assert replay.parse_config == upload.parse_config
    assert partial.model_dump(mode="json") == raw

    with pytest.raises(ValueError, match="retry_max_delay_seconds"):
        service.create_session(**{
            **request, "episode_order": 8, "idempotency_key": "new-partial-retry-policy",
        })
    assert len(service.list_sessions(tenant_id="local-default")) == 1


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
        tenant_id="local-default",
        series_id="77285f16-3e5b-592e-ba15-8b224b619806",
        episode_order=7,
        original_filename="episode-07.mp4",
        media_type="video/mp4",
        total_size_bytes=10,
        idempotency_key="configured-upload",
    )
    upload = service.create_session(**request, parse_config=selected)
    assert upload.parse_config == selected.to_video_parse_v2().model_dump(mode="json")
    assert service.create_session(**request, parse_config=selected).upload_session_id == upload.upload_session_id
    with pytest.raises(UploadConflictError, match="idempotency"):
        service.create_session(**request, parse_config=defaults)

    default_request = {
        **request,
        "episode_order": 8,
        "idempotency_key": "default-config-upload",
    }
    default_upload = service.create_session(**default_request)
    service._parse_defaults = selected
    replay = service.create_session(**default_request)
    assert replay.upload_session_id == default_upload.upload_session_id
    assert replay.parse_config == defaults.to_video_parse_v2().model_dump(mode="json")

    explicit_v2 = defaults.to_video_parse_v2().model_copy(
        update={"batch_max_size": defaults.to_video_parse_v2().batch_max_size + 1}
    )
    explicit_request = {
        **request,
        "episode_order": 10,
        "idempotency_key": "explicit-v2-config-upload",
    }
    explicit_upload = service.create_session(
        **explicit_request,
        parse_config=explicit_v2,
    )
    explicit_replay = service.create_session(
        **explicit_request,
        parse_config=explicit_v2,
    )
    assert explicit_replay.upload_session_id == explicit_upload.upload_session_id
    assert explicit_upload.parse_config == explicit_v2.model_dump(mode="json")
    changed_explicit_v2 = explicit_v2.model_copy(
        update={"batch_max_size": explicit_v2.batch_max_size + 1}
    )
    with pytest.raises(UploadConflictError, match="idempotency"):
        service.create_session(
            **explicit_request,
            parse_config=changed_explicit_v2,
        )

    _put(service, upload.upload_session_id, 0, b"abcd")
    _put(service, upload.upload_session_id, 1, b"efgh")
    _put(service, upload.upload_session_id, 2, b"ij")
    completed = service.complete(upload.upload_session_id, tenant_id="local-default")
    assert completed.video_parse_task_id is not None
    task = ParseJobRepository(create_session_factory(engine)).get_video_parse_input(
        completed.video_parse_task_id
    )
    assert task.task.parse_config == selected.to_video_parse_v2().model_dump(
        mode="json"
    )
    assert task.task.parse_config == completed.session.parse_config
    assert task.task.video_id == completed.video_id
    assert task.original_filename == upload.original_filename
    assert task.duration_seconds == 12_000
    lease = MySqlVideoParseQueue(create_session_factory(engine), video_concurrency=3).claim_video(
        "configured-worker", lease_duration=timedelta(minutes=5),
    )
    assert lease is not None and lease.video_parse_task_id == completed.video_parse_task_id
    assert completed.job_id is None
    with create_session_factory(engine)() as session:
        assert list(session.scalars(select(SeriesParseJob))) == []


@pytest.mark.parametrize("config_kind", ["profile", "v2"])
def test_upload_replays_explicit_config_with_a_pre_encoder_identity_fingerprint(
    repository, tmp_path, config_kind,
):
    defaults = ParseConfig.from_defaults(
        runtime=VideoParseRuntimeConfig(), gateway=load_config(),
        upload=_config(), images=ImageUploadConfig(),
        embedding_model="test-embedding", embedding_dimensions=1024,
        embedding_fps=1.0,
    )
    engine, _store, _repository, service = _upload_service(
        repository, tmp_path, parse_defaults=defaults,
    )
    config = defaults if config_kind == "profile" else defaults.to_video_parse_v2()
    request = dict(
        tenant_id="local-default", series_id="77285f16-3e5b-592e-ba15-8b224b619806",
        episode_order=7, original_filename="episode-07.mp4", media_type="video/mp4",
        total_size_bytes=10, idempotency_key="legacy-explicit-fingerprint",
    )
    upload = service.create_session(**request, parse_config=config)
    legacy_config = config.model_dump(mode="json")
    embedding = (
        legacy_config["embedding"] if config_kind == "profile"
        else legacy_config["models"]["embedding"]
    )
    assert embedding.pop("encoder_model") is None
    legacy_requested = {**request, "content_sha256": None,
                        ("parse_config" if config_kind == "profile" else "video_parse_config"): legacy_config}
    fingerprint = hashlib.sha256(json.dumps(
        legacy_requested, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()
    with create_session_factory(engine).begin() as session:
        session.get(UploadSession, upload.upload_session_id).request_fingerprint = fingerprint

    replay = service.create_session(**request, parse_config=config)
    assert replay.upload_session_id == upload.upload_session_id
    assert replay.parse_config == upload.parse_config
    changed = (
        config.model_copy(update={"vision": config.vision.model_copy(update={"fps": 0.5})})
        if config_kind == "profile"
        else config.model_copy(update={"batch_max_size": config.batch_max_size + 1})
    )
    with pytest.raises(UploadConflictError, match="idempotency"):
        service.create_session(**request, parse_config=changed)


def test_completion_rejects_a_missing_fixed_v2_config_snapshot(
    repository,
    tmp_path,
) -> None:
    engine, _store, _upload_repository, service = _upload_service(
        repository,
        tmp_path,
    )
    upload = _create(service)
    for number, data in enumerate((b"abcd", b"efgh", b"ij")):
        _put(service, upload.upload_session_id, number, data)
    session_factory = create_session_factory(engine)
    with session_factory.begin() as session:
        session.get(
            UploadSession,
            upload.upload_session_id,
        ).parse_config = None

    with pytest.raises(UploadConflictError, match="fixed V2 parse configuration"):
        service.complete(
            upload.upload_session_id,
            tenant_id="local-default",
        )

    with session_factory() as session:
        persisted = session.get(UploadSession, upload.upload_session_id)
        assert persisted.status == UploadSessionStatus.UPLOADING.value
        assert persisted.initial_task_id is None
        assert list(session.scalars(select(VideoParseTask))) == []


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
        service.get_detail(upload.upload_session_id, tenant_id="another-workspace")
    with pytest.raises(UploadSessionNotFoundError, match="workspace"):
        service.complete(upload.upload_session_id, tenant_id="another-workspace")
    for tenant_id, total_size, end_byte, expected_error in (
        ("another-workspace", 10, 4, UploadSessionNotFoundError),
        ("local-default", 11, 4, UploadConflictError),
        ("local-default", 10, 3, UploadConflictError),
    ):
        with pytest.raises(expected_error):
            service.upload_segment(
                upload.upload_session_id,
                0,
                b"abcd",
                tenant_id=tenant_id,
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
            tenant_id="local-default",
            checksum_sha256="0" * 64,
            start_byte=0,
            end_byte_exclusive=4,
            total_size_bytes=10,
        )
    with pytest.raises(UploadStateError, match="missing segments: 0, 1, 2"):
        service.complete(upload.upload_session_id, tenant_id="local-default")


def test_delete_removes_segments_and_unblocks_later_parse_without_reusing_order(repository, tmp_path):
    engine, store, upload_repository, service = _upload_service(repository, tmp_path)
    upload = _create(service)
    _put(service, upload.upload_session_id, 0, b"abcd")
    staging = store.base_directory / "staging" / "uploads" / upload.upload_session_id
    with pytest.raises(UploadSessionNotFoundError, match="workspace"):
        service.delete_session(upload.upload_session_id, tenant_id="other")
    assert staging.exists()
    assert upload_repository.get_session(upload.upload_session_id).status == "UPLOADING"

    later = service.create_session(
        tenant_id="local-default", series_id=upload.series_id,
        episode_order=8,
        original_filename="later.mp4", media_type="video/mp4", total_size_bytes=4,
        idempotency_key="later",
    )
    _put(service, later.upload_session_id, 0, b"efgh", total_size_bytes=4)
    completed = service.complete(later.upload_session_id, tenant_id="local-default")
    completed = _with_historical_v1_job(engine, completed, service.parse_defaults)
    session_factory = create_session_factory(engine)
    jobs = ParseJobRepository(session_factory)
    queue = MySqlParseJobQueue(session_factory)
    assert queue.claim_next(
        "test-worker", lease_duration=timedelta(minutes=5),
    ) is None

    deleted = service.delete_session(upload.upload_session_id, tenant_id="local-default")
    repeated = service.delete_session(upload.upload_session_id, tenant_id="local-default")
    assert deleted.status == repeated.status == "EXPIRED"
    assert repeated.version == deleted.version
    assert deleted.uploaded_size_bytes == deleted.uploaded_segment_count == 0
    assert upload_repository.list_segments(upload.upload_session_id) == []
    assert not staging.exists()
    assert service.list_sessions(tenant_id="local-default") == []
    lease = queue.claim_next(
        "test-worker", lease_duration=timedelta(minutes=5),
    )
    assert lease is not None and lease.job_id == completed.job_id
    with pytest.raises(UploadStateError, match="expired"):
        _put(service, upload.upload_session_id, 0, b"abcd")
    with pytest.raises(UploadStateError, match="expired"):
        service.complete(upload.upload_session_id, tenant_id="local-default")
    assert _create(service).status == "EXPIRED"
    next_upload = service.create_session(
        tenant_id="local-default", series_id=upload.series_id,
        episode_order=9,
        original_filename="next.mp4", media_type="video/mp4", total_size_bytes=4,
        idempotency_key="next",
    )
    assert next_upload.episode_order == next_upload.upload_sequence == 9


@pytest.mark.parametrize("delete_before_write", [True, False])
def test_delete_fences_in_flight_segment_and_cleans_late_bytes(repository, tmp_path, monkeypatch, delete_before_write):
    _engine, store, upload_repository, service = _upload_service(repository, tmp_path)
    upload = _create(service)
    put = store.put_upload_segment

    def put_during_delete(*args):
        if delete_before_write:
            service.delete_session(upload.upload_session_id, tenant_id="local-default")
        stored = put(*args)
        if not delete_before_write:
            service.delete_session(upload.upload_session_id, tenant_id="local-default")
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
        service.delete_session(upload.upload_session_id, tenant_id="local-default")
    assert upload_repository.get_session(upload.upload_session_id).status == "COMPLETING"
    assert len(upload_repository.list_segments(upload.upload_session_id)) == 3
    upload_repository.release_completion(upload.upload_session_id, claim.completion_token, error="retry")
    completed = service.complete(upload.upload_session_id, tenant_id="local-default")
    completed = _with_historical_v1_job(engine, completed, service.parse_defaults)
    lease = MySqlParseJobQueue(create_session_factory(engine)).claim_next(
        "test-worker",
        lease_duration=timedelta(minutes=5),
    )
    assert lease is not None and lease.job_id == completed.job_id
    with pytest.raises(UploadStateError, match="being parsed"):
        service.delete_session(upload.upload_session_id, tenant_id="local-default")
    with create_session_factory(engine)() as session:
        video = session.get(Video, completed.video_id)
        assert video is not None
        assert store.resolve(video.source_uri).exists()
        assert session.get(UploadSession, upload.upload_session_id).status == "COMPLETED"


def test_delete_rejects_active_v2_prefix_lease(repository, tmp_path):
    engine, store, _upload_repository, service = _upload_service(
        repository,
        tmp_path,
    )
    upload = _create(service)
    for number, data in enumerate((b"abcd", b"efgh", b"ij")):
        _put(service, upload.upload_session_id, number, data)
    completed = service.complete(
        upload.upload_session_id,
        tenant_id="local-default",
    )
    with create_session_factory(engine).begin() as session:
        task = session.get(
            VideoParseTask,
            completed.video_parse_task_id,
        )
        task.status = "COMPLETE"
        task.finish_time = unix_time_ms_to_datetime(1_789_560_000_000)
        task.detail = {
            **task.detail,
            "_worker_runtime_v2": {
                **task.detail["_worker_runtime_v2"],
                "prefix": {
                    "lease_owner": "prefix-worker",
                    "lease_version": 1,
                    "lease_expire_time_ms": 9_999_999_999_999,
                    "execution_task_id": str(uuid4()),
                },
            },
        }

    with pytest.raises(UploadStateError, match="being parsed"):
        service.delete_session(
            upload.upload_session_id,
            tenant_id="local-default",
        )
    with pytest.raises(UploadStateError, match="Prefix worker is active"):
        service.reparse(
            upload.upload_session_id,
            tenant_id="local-default",
            expected_job_version=1,
        )

    with create_session_factory(engine)() as session:
        task = session.get(
            VideoParseTask,
            completed.video_parse_task_id,
        )
        assert task.is_deleted is False
        assert store.resolve(
            session.get(Video, completed.video_id).source_uri
        ).exists()


@pytest.mark.parametrize(
    "take_over_pending",
    [False, True],
    ids=["ordinary-reparse", "pending-takeover"],
)
def test_reparse_atomically_invalidates_the_series_prefix_suffix(
    repository,
    tmp_path,
    take_over_pending,
):
    now_ms = 1_789_646_400_000
    now = unix_time_ms_to_datetime(now_ms)
    engine, _store, upload_repository, service = _upload_service(
        repository,
        tmp_path,
        clock=lambda: now_ms,
    )
    completed_by_order = {}
    upload_by_order = {}
    for order in range(1, 4):
        upload = service.create_session(
            tenant_id="local-default",
            series_id="77285f16-3e5b-592e-ba15-8b224b619806",
            episode_order=order,
            original_filename=f"episode-{order}.mp4",
            media_type="video/mp4",
            total_size_bytes=10,
            idempotency_key=f"series-prefix-{take_over_pending}-{order}",
        )
        episode_parts = (
            f"{order}aaa".encode(),
            f"{order}bbb".encode(),
            f"{order}c".encode(),
        )
        for number, data in enumerate(episode_parts):
            _put(service, upload.upload_session_id, number, data)
        upload_by_order[order] = upload
        completed_by_order[order] = service.complete(
            upload.upload_session_id,
            tenant_id="local-default",
        )

    factory = create_session_factory(engine)
    prefix_before = {}
    with factory.begin() as session:
        for order, completed in completed_by_order.items():
            task = session.get(
                VideoParseTask,
                completed.video_parse_task_id,
            )
            config = dict(task.parse_config)
            config["compact_enabled"] = order != 2
            task.parse_config = config
            if order != 1 or not take_over_pending:
                task.status = "COMPLETE"
                task.finish_time = now

            detail = dict(task.detail)
            runtime = dict(detail["_worker_runtime_v2"])
            runtime["accepted"] = order != 1 or not take_over_pending
            prefix = {
                "next_chunk_sequence_no": 0 if order == 3 else 1,
                "lease_owner": "later-prefix-worker" if order == 3 else None,
                "lease_version": order + 4,
                "lease_expire_time_ms": (
                    datetime_to_unix_time_ms(now + timedelta(minutes=10))
                    if order == 3
                    else None
                ),
                "execution_task_id": "later-prefix-chunk" if order == 3 else None,
                "next_execute_time_ms": 0,
                "retry_count": 2,
                "video_prefix_compact": f"video-{order}",
                "series_prefix_compact": f"series-{order}",
                "last_error": {"message": "old"},
            }
            runtime["prefix"] = prefix
            detail["_worker_runtime_v2"] = runtime
            task.detail = detail
            prefix_before[order] = dict(prefix)

    target_upload = upload_by_order[1]
    target_task_id = completed_by_order[1].video_parse_task_id
    with pytest.raises(UploadStateError, match="active lease"):
        upload_repository.reparse(
            target_upload.upload_session_id,
            tenant_id="local-default",
            expected_job_version=1,
            now=now,
        )

    with factory() as session:
        target_tasks = list(session.scalars(
            select(VideoParseTask)
            .where(VideoParseTask.video_id == completed_by_order[1].video_id)
            .order_by(VideoParseTask.version)
        ))
        assert [task.id for task in target_tasks] == [target_task_id]
        assert target_tasks[0].detail["_worker_runtime_v2"]["prefix"] == (
            prefix_before[1]
        )
        assert target_tasks[0].detail["_worker_runtime_v2"]["accepted"] is (
            not take_over_pending
        )
        third = session.get(
            VideoParseTask,
            completed_by_order[3].video_parse_task_id,
        )
        assert third.detail["_worker_runtime_v2"]["prefix"] == prefix_before[3]

    with factory.begin() as session:
        third = session.get(
            VideoParseTask,
            completed_by_order[3].video_parse_task_id,
        )
        detail = dict(third.detail)
        runtime = dict(detail["_worker_runtime_v2"])
        prefix = dict(runtime["prefix"])
        prefix["lease_expire_time_ms"] = datetime_to_unix_time_ms(
            now - timedelta(seconds=1)
        )
        runtime["prefix"] = prefix
        detail["_worker_runtime_v2"] = runtime
        third.detail = detail

    reparsed = upload_repository.reparse(
        target_upload.upload_session_id,
        tenant_id="local-default",
        expected_job_version=1,
        now=now,
    )
    if take_over_pending:
        assert (reparsed.video_parse_task_id, reparsed.version) == (target_task_id, 1)
    else:
        assert reparsed.video_parse_task_id != target_task_id
        assert reparsed.version == 2

    with factory() as session:
        target_tasks = list(session.scalars(
            select(VideoParseTask)
            .where(VideoParseTask.video_id == completed_by_order[1].video_id)
            .order_by(VideoParseTask.version)
        ))
        assert target_tasks[-1].detail["_worker_runtime_v2"]["accepted"] is True
        target_prefix = target_tasks[-1].detail["_worker_runtime_v2"]["prefix"]
        if take_over_pending:
            assert target_prefix["lease_version"] == (
                prefix_before[1]["lease_version"] + 1
            )
        else:
            assert len(target_tasks) == 2
            assert target_tasks[0].detail["_worker_runtime_v2"]["prefix"] == (
                prefix_before[1]
            )
            assert target_prefix["lease_version"] == 1
        assert (
            target_prefix["next_chunk_sequence_no"],
            target_prefix["series_prefix_compact"],
            target_prefix["lease_owner"],
        ) == (0, "", None)

        disabled = session.get(
            VideoParseTask,
            completed_by_order[2].video_parse_task_id,
        )
        assert disabled.detail["_worker_runtime_v2"]["prefix"] == prefix_before[2]
        third = session.get(
            VideoParseTask,
            completed_by_order[3].video_parse_task_id,
        )
        third_prefix = third.detail["_worker_runtime_v2"]["prefix"]
        assert (
            third_prefix["next_chunk_sequence_no"],
            third_prefix["video_prefix_compact"],
            third_prefix["series_prefix_compact"],
            third_prefix["lease_owner"],
            third_prefix["lease_expire_time_ms"],
            third_prefix["execution_task_id"],
            third_prefix["retry_count"],
            third_prefix["last_error"],
            third_prefix["lease_version"],
        ) == (
            0,
            "",
            "",
            None,
            None,
            None,
            0,
            None,
            prefix_before[3]["lease_version"] + 1,
        )


def test_manual_parse_retry_reuses_v2_identity_config_and_results(repository, tmp_path):
    retry_at = 1_789_560_000_000
    engine, store, _upload_repository, service = _upload_service(
        repository,
        tmp_path,
        clock=lambda: retry_at,
    )
    upload = _create(service)
    for number, data in enumerate((b"abcd", b"efgh", b"ij")):
        _put(service, upload.upload_session_id, number, data)
    completed = service.complete(upload.upload_session_id, tenant_id="local-default")
    completed = _with_historical_v1_job(engine, completed, service.parse_defaults)
    assert completed.video_parse_task_id is not None
    video_chunk_id = str(uuid4())
    chunk_task_id = str(uuid4())
    content_id = str(uuid4())
    started_at = retry_at - timedelta_to_ms(timedelta(minutes=10))
    completed_at = retry_at - timedelta_to_ms(timedelta(minutes=1))
    with create_session_factory(engine).begin() as session:
        job = session.get(SeriesParseJob, completed.job_id)
        legacy_job_before = (
            job.status,
            job.stage,
            job.progress,
            job.version,
            job.attempt_count,
            job.max_attempts,
        )
        task = session.get(VideoParseTask, completed.video_parse_task_id)
        original_config = task.parse_config
        task.status = "PARSING_FAILED"
        task.attempt_count = 3
        task.max_attempts = 3
        task.start_time = unix_time_ms_to_datetime(started_at)
        task.finish_time = unix_time_ms_to_datetime(completed_at)
        task.processing_duration_ms = 1234
        task.last_error = {"code": "MODEL_TIMEOUT", "message": "provider timed out"}
        task.detail = {
            **task.detail,
            RUNTIME_DETAIL_KEY: {**task.detail.get(RUNTIME_DETAIL_KEY, {}), PREPARE_FAILURES_KEY: 3},
        }
        session.add(VideoChunk(
            id=video_chunk_id,
            video_id=completed.video_id,
            sequence_no=0,
            start_ms=0,
            end_ms=12_000,
            window_start_ms=0,
            window_end_ms=12_000,
            chunk_media_ref="artifact://test/old-chunk.mp4",
            analysis_window_media_ref="artifact://test/old-window.mp4",
            transcript=[],
            baseline_visual_caption=None,
            keyframe_uris=[],
        ))
        session.add(ChunkParseTask(
            id=chunk_task_id,
            video_task_id=completed.video_parse_task_id,
            chunk_id=video_chunk_id,
            phase=3, indexed_content_version=1, status="COMPLETE",
            next_execute_time=unix_time_ms_to_datetime(started_at),
            retry_count=0,
            lease_version=1,
            start_time=unix_time_ms_to_datetime(started_at),
            finish_time=unix_time_ms_to_datetime(completed_at),
            detail={"attempt": 3},
        ))
        session.add(ChunkParseContent(
            id=content_id,
            chunk_task_id=chunk_task_id,
            content={"persons": [], "timeline": []},
            version=1,
            detail={"compact": "retained compact context"},
        ))
        session.add(VideoChunkCheckpoint(
            job_id=completed.job_id,
            sequence_no=0,
            chunk_id=video_chunk_id,
            stage=VideoChunkCheckpointStage.PARSED.value,
            parse_artifact_uri="artifact://test/jobs/parse.json",
        ))
        session.add(ParseUsageAttempt(
            job_id=completed.job_id,
            video_task_id=completed.video_parse_task_id,
            attempt_count=3,
            lease_version=3,
            start_time=unix_time_ms_to_datetime(started_at),
            recording_complete=True,
        ))
    work_file = store.work_directory(completed.job_id) / "resume.json"
    work_file.write_text("keep", encoding="utf-8")

    retried = service.retry_parse(
        upload.upload_session_id,
        tenant_id="local-default",
        expected_job_version=1, expected_lease_version=0,
    )

    session_factory = create_session_factory(engine)
    jobs = ParseJobRepository(session_factory)
    assert retried.video_parse_task_id == completed.video_parse_task_id
    assert (retried.version, retried.status) == (1, "PREPARING")
    assert (retried.attempt_count, retried.max_attempts) == (3, 3)
    assert PREPARE_FAILURES_KEY not in retried.detail[RUNTIME_DETAIL_KEY]
    assert retried.parse_config == original_config
    assert retried.next_execute_time == unix_time_ms_to_datetime(retry_at)
    assert retried.start_time == unix_time_ms_to_datetime(started_at)
    assert retried.finish_time is None
    assert retried.last_error is None
    assert retried.processing_duration_ms == 1234
    assert work_file.read_text(encoding="utf-8") == "keep"
    with create_session_factory(engine)() as session:
        assert session.get(ParseUsageAttempt, (completed.job_id, 3, 3)) is not None
        content = session.get(ChunkParseContent, content_id)
        assert content.chunk_task_id == chunk_task_id
        assert content.detail == {"compact": "retained compact context"}
        assert session.get(ChunkParseTask, chunk_task_id) is not None
        assert session.get(VideoChunkCheckpoint, (completed.job_id, 0)) is not None
        job = session.get(SeriesParseJob, completed.job_id)
        assert (
            job.status,
            job.stage,
            job.progress,
            job.version,
            job.attempt_count,
            job.max_attempts,
        ) == legacy_job_before
        assert session.scalar(select(VideoParseTask).where(
            VideoParseTask.video_id == completed.video_id
        )) is not None
    assert jobs.get_video_parse_task(completed.video_parse_task_id) == retried

    with pytest.raises(UploadConflictError, match="version or lease changed"):
        service.retry_parse(
            upload.upload_session_id,
            tenant_id="local-default",
            expected_job_version=1, expected_lease_version=0,
        )


def test_manual_parse_retry_requires_the_latest_failed_v2_task(repository, tmp_path):
    engine, _store, _upload_repository, service = _upload_service(repository, tmp_path)
    upload = _create(service)
    for number, data in enumerate((b"abcd", b"efgh", b"ij")):
        _put(service, upload.upload_session_id, number, data)
    completed = service.complete(upload.upload_session_id, tenant_id="local-default")
    completed = _with_historical_v1_job(engine, completed, service.parse_defaults)

    with pytest.raises(UploadSessionNotFoundError, match="workspace"):
        service.retry_parse(
            upload.upload_session_id,
            tenant_id="other-workspace",
            expected_job_version=1, expected_lease_version=0,
        )
    with pytest.raises(UploadStateError, match="failed VideoParseTask"):
        service.retry_parse(
            upload.upload_session_id,
            tenant_id="local-default",
            expected_job_version=1, expected_lease_version=0,
        )

    with create_session_factory(engine).begin() as session:
        primary = session.get(SeriesParseJob, completed.job_id)
        primary.status = ParseJobStatus.FAILED.value
        primary.attempt_count = primary.max_attempts
        primary.last_error = "failed"
    with pytest.raises(UploadStateError, match="failed VideoParseTask"):
        service.retry_parse(
            upload.upload_session_id,
            tenant_id="local-default",
            expected_job_version=1, expected_lease_version=0,
        )

    with create_session_factory(engine).begin() as session:
        task = session.get(VideoParseTask, completed.video_parse_task_id)
        task.status = "PARSING_FAILED"
        task.last_error = {"code": "MODEL_ERROR"}
    retried = service.retry_parse(
        upload.upload_session_id,
        tenant_id="local-default",
        expected_job_version=1, expected_lease_version=0,
    )
    assert retried.video_parse_task_id == completed.video_parse_task_id
    assert retried.version == 1


@pytest.fixture
def prepared_v2_retry(repository, tmp_path):
    retry_at = 1_789_560_000_000
    engine, _store, _repository, service = _upload_service(
        repository, tmp_path, clock=lambda: retry_at,
    )
    upload = service.create_session(
        tenant_id="local-default",
        series_id="77285f16-3e5b-592e-ba15-8b224b619806",
        episode_order=7, original_filename="episode.mp4", media_type="video/mp4",
        total_size_bytes=10, idempotency_key="interval-retry",
        parse_config=default_video_parse_v2_config().model_copy(
            update={"chunk_duration": 3, "interval_duration": 6},
        ),
    )
    for number, data in enumerate((b"abcd", b"efgh", b"ij")):
        _put(service, upload.upload_session_id, number, data)
    completed = service.complete(upload.upload_session_id, tenant_id="local-default")
    factory = create_session_factory(engine)
    chunk_ids = [str(uuid4()) for _ in range(4)]
    interval_ids = [str(uuid4()) for _ in range(3)]
    now = unix_time_ms_to_datetime(retry_at)
    before = now - timedelta(minutes=1)
    with factory.begin() as session:
        task = session.get(VideoParseTask, completed.video_parse_task_id)
        task.status = "PARSING_FAILED"
        task.finish_time = before
        task.last_error = {"code": "CHILD_FAILED", "message": "retained failure"}
        for sequence, chunk_task_id in enumerate(chunk_ids):
            chunk_id = str(uuid4())
            session.add(VideoChunk(
                id=chunk_id, video_id=completed.video_id,
                sequence_no=sequence, start_ms=sequence * 3000, end_ms=(sequence + 1) * 3000,
                window_start_ms=sequence * 3000,
                window_end_ms=(sequence + 1) * 3000,
                chunk_media_ref="artifact://test/chunk.mp4",
                analysis_window_media_ref="artifact://test/window.mp4",
            ))
            session.add(ChunkParseTask(
                id=chunk_task_id, video_task_id=task.id, chunk_id=chunk_id,
                phase=3, indexed_content_version=3, status="COMPLETE", next_execute_time=before, retry_count=2,
                lease_version=4, finish_time=before, last_error={"code": "PRIOR_ERROR"},
            ))
            session.add(ChunkParseContent(
                id=str(uuid4()), chunk_task_id=chunk_task_id,
                content={"persons": [], "timeline": []}, version=3,
            ))
        for start, interval_id in enumerate(interval_ids):
            session.add(IntervalParseTask(
                id=interval_id, video_task_id=task.id, chunk_task_id=chunk_ids[start],
                start_sequence_no=start, chunk_count=2, chunk_task_ids=chunk_ids[start:start + 2],
                ready_mask=3, status="COMPLETE", next_execute_time=before, retry_count=2,
                lease_version=5, finish_time=before, last_error={"code": "PRIOR_ERROR"},
            ))
            session.add(IntervalParseContent(
                interval_task_id=interval_id, content={"retained": start},
            ))
    return service, upload, completed.video_parse_task_id, factory, chunk_ids, interval_ids, now


@pytest.mark.parametrize("parent_state", ["PARSING_FAILED", "INDEXING_FAILED"])
def test_parse_retry_preserves_snapshot_after_yaml_changes(
    prepared_v2_retry, tmp_path, monkeypatch, parent_state,
):
    service, upload, task_id, factory, chunk_ids, interval_ids, _now = prepared_v2_retry
    monkeypatch.delenv("ANIVAG_VIDEO_MODEL", raising=False)
    config_path = tmp_path / "retry-models.yaml"
    payload = yaml.safe_load((Path(__file__).resolve().parents[2] / "config.yaml").read_text())
    payload["models"]["vision"].update(
        source="ark_default", model="doubao-seed-2-1-pro-260628", max_output_tokens=65536,
    )
    for field in ("auto_compact_tokens", "compact_target_tokens"):
        payload["models"]["vision"].pop(field, None)
    config_path.write_text(yaml.safe_dump(payload))

    def load_defaults():
        return ParseConfig.from_defaults(
            runtime=VideoParseRuntimeConfig(
                chunk_duration_seconds=90, asr_concurrency=2,
                transcript_model="changed-current-asr", request_timeout_seconds=45,
            ),
            gateway=load_config(config_path), upload=_config(), images=ImageUploadConfig(),
            embedding_model="changed-current-embedding", embedding_dimensions=512,
            embedding_fps=2.0,
        )

    monkeypatch.setattr(service, "_parse_defaults", load_defaults())
    assert load_defaults().vision.model == "doubao-seed-2-1-pro-260628"
    with factory.begin() as session:
        parent = session.get(VideoParseTask, task_id)
        parent.status = parent_state
        old = VideoParseV2Config.model_validate(parent.parse_config)
        pro = old.models.understanding.model_copy(update={
            "source": "ark_default", "model": "doubao-seed-2-1-pro-260628",
            "max_output_tokens": 65536, "reasoning_effort": "none",
        })
        parent.parse_config = old.model_copy(update={
            "models": old.models.model_copy(update={"understanding": pro, "refine": pro}),
            "visual_input": old.visual_input.model_copy(update={"mode": "video"}),
        }).model_dump(mode="json")
        before_config = parent.parse_config
        session.get(ChunkParseTask, chunk_ids[-1]).status = "FAILED"
        session.get(IntervalParseTask, interval_ids[0]).status = "PARSING_FAILED"
        chunk_results = [(row.id, row.content, row.version, row.detail)
                         for row in session.scalars(select(ChunkParseContent).order_by(ChunkParseContent.id))]
        interval_results = [(row.interval_task_id, row.content, row.detail)
                            for row in session.scalars(select(IntervalParseContent).order_by(IntervalParseContent.interval_task_id))]
        topology = [(row.id, row.start_sequence_no, row.chunk_count, row.chunk_task_ids)
                    for row in session.scalars(select(IntervalParseTask).order_by(IntervalParseTask.id))]

    payload["models"]["vision"].update(
        source="deepseek_api", model="deepseek-flash", max_output_tokens=8192,
        reasoning_effort="none",
    )
    config_path.write_text(yaml.safe_dump(payload))
    retried = service.retry_parse(
        upload.upload_session_id, tenant_id="local-default", expected_job_version=1, expected_lease_version=0,
    )

    assert (retried.video_parse_task_id, retried.version, retried.status) == (task_id, 1, "PARSING")
    assert retried.parse_config == before_config
    assert retried.parse_config["visual_input"]["mode"] == "video"
    with factory() as session:
        assert [session.get(ChunkParseTask, task_id).status for task_id in chunk_ids] == [
            "COMPLETE", "COMPLETE", "COMPLETE", "QUEUED",
        ]
        assert session.get(IntervalParseTask, interval_ids[0]).status == "WAITING"
        assert [(row.id, row.content, row.version, row.detail)
                for row in session.scalars(select(ChunkParseContent).order_by(ChunkParseContent.id))] == chunk_results
        assert [(row.interval_task_id, row.content, row.detail)
                for row in session.scalars(select(IntervalParseContent).order_by(IntervalParseContent.interval_task_id))] == interval_results
        assert [(row.id, row.start_sequence_no, row.chunk_count, row.chunk_task_ids)
                for row in session.scalars(select(IntervalParseTask).order_by(IntervalParseTask.id))] == topology


def test_parse_retry_does_not_read_bad_current_yaml(
    prepared_v2_retry, tmp_path, monkeypatch,
):
    service, upload, task_id, factory, chunk_ids, interval_ids, _now = prepared_v2_retry
    config_path = tmp_path / "invalid-retry-models.yaml"
    config_path.write_text("models: [")
    monkeypatch.setattr(service, "_resolve_parse_config", lambda *_args: load_config(config_path))
    with factory.begin() as session:
        parent = session.get(VideoParseTask, task_id)
        parent.detail = {**parent.detail, "_worker_runtime_v2": {
            **parent.detail["_worker_runtime_v2"], "failure_status": "PARSING_FAILED",
        }}
        before = (parent.parse_config, parent.detail, parent.next_execute_time,
                  parent.finish_time, parent.last_error)
        session.get(ChunkParseTask, chunk_ids[-1]).status = "FAILED"
        session.get(IntervalParseTask, interval_ids[0]).status = "PARSING_FAILED"

    result = service.retry_parse(upload.upload_session_id, tenant_id="local-default",
                                 expected_job_version=1, expected_lease_version=0)
    assert result.parse_config == before[0]
    with factory() as session:
        parent = session.get(VideoParseTask, task_id)
        assert parent.status == "PARSING"
        assert "failure_status" not in parent.detail["_worker_runtime_v2"]
        assert session.get(ChunkParseTask, chunk_ids[-1]).status == "QUEUED"
        assert session.get(IntervalParseTask, interval_ids[0]).status == "WAITING"


def test_parse_retry_rejects_stale_version_before_loading_defaults(prepared_v2_retry, monkeypatch):
    service, upload, task_id, factory, _chunk_ids, _interval_ids, _now = prepared_v2_retry

    def forbidden_loader():
        raise AssertionError("stale retry must not load new model defaults")

    monkeypatch.setattr(service, "_resolve_parse_config", forbidden_loader)
    with factory() as session:
        before = session.get(VideoParseTask, task_id).parse_config
    with pytest.raises(UploadConflictError, match="version or lease changed"):
        service.retry_parse(upload.upload_session_id, tenant_id="local-default", expected_job_version=2, expected_lease_version=0)
    with factory() as session:
        parent = session.get(VideoParseTask, task_id)
        assert parent.status == "PARSING_FAILED" and parent.parse_config == before


@pytest.fixture
def prepared_short_v2_retry(prepared_v2_retry):
    service, upload, task_id, factory, chunk_ids, interval_ids, now = prepared_v2_retry
    with factory.begin() as session:
        task = session.get(VideoParseTask, task_id)
        task.parse_config = {**task.parse_config, "interval_duration": 15}
        task.status = "INDEXING_FAILED"
        short_interval = session.get(IntervalParseTask, interval_ids[0])
        short_interval.chunk_count = len(chunk_ids)
        short_interval.chunk_task_ids = chunk_ids
        short_interval.ready_mask = (1 << len(chunk_ids)) - 1
        short_interval.status = "COMPLETE"
        session.get(ChunkParseTask, chunk_ids[0]).status = "FAILED"
        for interval_id in interval_ids[1:]:
            session.delete(session.get(IntervalParseTask, interval_id))
    return service, upload, task_id, factory, chunk_ids, interval_ids[0], now


def test_indexing_retry_accepts_the_unique_short_interval_and_retains_results(
    prepared_short_v2_retry, monkeypatch,
):
    service, upload, task_id, factory, chunk_ids, interval_id, now = (
        prepared_short_v2_retry
    )

    def forbidden_loader():
        raise AssertionError("index retry must preserve its frozen analysis binding")

    monkeypatch.setattr(service, "_resolve_parse_config", forbidden_loader)
    with factory() as session:
        task = session.get(VideoParseTask, task_id)
        frozen_config = task.parse_config
        chunk_results = [
            (row.id, row.content, row.version, row.detail)
            for row in session.scalars(
                select(ChunkParseContent).order_by(ChunkParseContent.id)
            )
        ]
        interval_result = session.get(IntervalParseContent, interval_id).content

    retried = service.retry_parse(
        upload.upload_session_id,
        tenant_id="local-default",
        expected_job_version=1, expected_lease_version=0,
    )

    assert (retried.video_parse_task_id, retried.status, retried.version) == (
        task_id,
        "INDEXING",
        1,
    )
    assert retried.parse_config == frozen_config
    with factory() as session:
        interval = session.get(IntervalParseTask, interval_id)
        assert (
            interval.start_sequence_no,
            interval.chunk_count,
            interval.chunk_task_ids,
            interval.status,
            interval.next_execute_time,
        ) == (0, len(chunk_ids), chunk_ids, "COMPLETE", now - timedelta(minutes=1))
        assert sorted([
            child.status for child in session.scalars(
                select(ChunkParseTask)
                .where(ChunkParseTask.video_task_id == task_id)
                .order_by(ChunkParseTask.id)
            )
        ]) == ["COMPLETE"] * (len(chunk_ids) - 1) + ["QUEUED"]
        assert [
            (row.id, row.content, row.version, row.detail)
            for row in session.scalars(
                select(ChunkParseContent).order_by(ChunkParseContent.id)
            )
        ] == chunk_results
        assert session.get(IntervalParseContent, interval_id).content == interval_result


@pytest.mark.parametrize(
    ("invalid", "error"),
    [

        ("wrong_start", "Interval preparation"),
        ("wrong_count", "Interval preparation"),
        ("wrong_members", "Interval members"),
    ],
)
def test_indexing_retry_rejects_invalid_short_window_without_mutation(
    prepared_short_v2_retry,
    invalid,
    error,
):
    service, upload, task_id, factory, chunk_ids, interval_id, _now = (
        prepared_short_v2_retry
    )
    with factory.begin() as session:
        failed_child = session.get(ChunkParseTask, chunk_ids[-1])
        failed_child.status = "FAILED"
        interval = session.get(IntervalParseTask, interval_id)
        if invalid == "missing_window":
            session.delete(interval)
        elif invalid == "wrong_start":
            interval.start_sequence_no = 1
        elif invalid == "wrong_count":
            interval.chunk_count = len(chunk_ids) - 1
            interval.chunk_task_ids = chunk_ids[:-1]
            interval.ready_mask = (1 << (len(chunk_ids) - 1)) - 1
        else:
            interval.chunk_task_ids = [*chunk_ids[1:], chunk_ids[0]]

    def persisted_state():
        with factory() as session:
            parent = session.get(VideoParseTask, task_id)
            chunks = list(session.scalars(
                select(ChunkParseTask)
                .where(ChunkParseTask.video_task_id == task_id)
                .order_by(ChunkParseTask.id)
            ))
            intervals = list(session.scalars(
                select(IntervalParseTask)
                .where(IntervalParseTask.video_task_id == task_id)
                .order_by(IntervalParseTask.id)
            ))
            return (
                (
                    parent.status,
                    parent.parse_config,
                    parent.next_execute_time,
                    parent.finish_time,
                    parent.last_error,
                ),
                [
                    (
                        child.id,
                        child.status,
                        child.next_execute_time,
                        child.finish_time,
                        child.last_error,
                    )
                    for child in chunks
                ],
                [
                    (
                        interval.id,
                        interval.start_sequence_no,
                        interval.chunk_count,
                        interval.chunk_task_ids,
                        interval.status,
                        interval.next_execute_time,
                        interval.finish_time,
                        interval.last_error,
                    )
                    for interval in intervals
                ],
                [
                    (row.id, row.chunk_task_id, row.content, row.version, row.detail)
                    for row in session.scalars(
                        select(ChunkParseContent)
                        .order_by(ChunkParseContent.id)
                    )
                ],
                [
                    (row.interval_task_id, row.content, row.detail)
                    for row in session.scalars(
                        select(IntervalParseContent)
                        .order_by(IntervalParseContent.interval_task_id)
                    )
                ],
            )

    before = persisted_state()
    with pytest.raises(UploadConflictError, match=error):
        service.retry_parse(
            upload.upload_session_id,
            tenant_id="local-default",
            expected_job_version=1, expected_lease_version=0,
        )
    assert persisted_state() == before


@pytest.mark.parametrize("chunk_states,interval_states,parent_state,expected", [
    ([(3, "COMPLETE"), (2, "WAITING"), (1, "FAILED"), (1, "QUEUED")],
     ["COMPLETE", "PARSING_FAILED", "WAITING"], "PARSING_FAILED", "PARSING"),
    ([(3, "FAILED"), (3, "QUEUED"), (3, "COMPLETE"), (3, "COMPLETE")],
     ["COMPLETE"] * 3, "INDEXING_FAILED", "INDEXING"),
    ([(2, "WAITING")] * 4,
     ["PARSING_FAILED", "COMPLETE", "COMPLETE"], "PARSING_FAILED", "PARSING"),
    ([(2, "WAITING")] * 4,
     ["WAITING", None, "WAITING"], "PARSING_FAILED", "PREPARING"),
])
def test_manual_retry_restores_each_child_phase_and_retains_results(
    prepared_v2_retry, chunk_states, interval_states, parent_state, expected,
):
    service, upload, task_id, factory, chunk_ids, interval_ids, now = prepared_v2_retry
    with factory.begin() as session:
        task = session.get(VideoParseTask, task_id)
        task.status = parent_state
        config = task.parse_config
        runtime = {
            **task.detail["_worker_runtime_v2"],
            "failure_status": parent_state,
            "model_calls": {"used_call_ids": ["call-1", "call-2"], "active": {}},
            "prefix": {
                "next_chunk_sequence_no": 2,
                "lease_owner": None,
                "lease_version": 7,
                "lease_expire_time_ms": None,
                "execution_task_id": None,
                "next_execute_time_ms": 0,
                "retry_count": 0,
                "video_prefix_compact": "retained-video-prefix",
                "series_prefix_compact": "retained-series-prefix",
                "last_error": None,
                "committed_leases": {"6": {"sequence_no": 1, "execution_task_id": "prior-prefix"}},
            },
        }
        task.detail = {**task.detail, "_worker_runtime_v2": runtime}
        for model, ids, states in (
            (ChunkParseTask, chunk_ids, chunk_states),
            (IntervalParseTask, interval_ids, interval_states),
        ):
            for child_id, status in zip(ids, states):
                child = session.get(model, child_id)
                if status is None:
                    session.delete(child)
                    continue
                if model is ChunkParseTask:
                    child.phase, child.status = status
                    if child.phase == 1:
                        result = session.scalar(select(ChunkParseContent).where(ChunkParseContent.chunk_task_id == child.id))
                        session.delete(result)
                else:
                    child.status = status

    retried = service.retry_parse(
        upload.upload_session_id, tenant_id="local-default", expected_job_version=1, expected_lease_version=0,
    )
    assert (retried.video_parse_task_id, retried.status, retried.version) == (task_id, expected, 1)
    assert retried.parse_config == config
    assert retried.finish_time is None
    assert retried.last_error is None
    transitions = {"FAILED": "QUEUED", "PARSING_FAILED": "WAITING"}
    with factory() as session:
        assert session.get(VideoParseTask, task_id).detail["_worker_runtime_v2"] == {
            key: value for key, value in runtime.items() if key != "failure_status"
        }
        for model, ids, states in (
            (ChunkParseTask, chunk_ids, chunk_states),
            (IntervalParseTask, interval_ids, interval_states),
        ):
            for child_id, before in zip(ids, states):
                child = session.get(model, child_id)
                if before is None:
                    assert child is None
                    continue
                if model is ChunkParseTask:
                    phase, before = before
                    assert child.phase == phase
                assert child.status == transitions.get(before, before)
                reset_retry = before in transitions or before in {"QUEUED", "WAITING"}
                assert child.retry_count == (0 if reset_retry else 2)
                assert child.last_error == (None if reset_retry else {"code": "PRIOR_ERROR"})
                if reset_retry:
                    assert child.next_execute_time == now and child.finish_time is None
                    assert child.lease_owner is None and child.lease_expire_time is None
                else:
                    assert child.finish_time == now - timedelta(minutes=1)
        assert [row.version for row in session.scalars(select(ChunkParseContent))] == [3] * sum(phase != 1 for phase, _ in chunk_states)
        for start, interval_id in enumerate(interval_ids):
            content = session.get(IntervalParseContent, interval_id)
            if interval_states[start] is not None:
                assert content.content == {"retained": start}


@pytest.mark.parametrize("lease_expired", [False, True])
def test_manual_retry_rejects_failure_drain_without_clearing_intent(
    prepared_v2_retry, lease_expired,
):
    service, upload, task_id, factory, chunk_ids, _interval_ids, now = prepared_v2_retry
    with factory.begin() as session:
        parent = session.get(VideoParseTask, task_id)
        parent.status = "PARSING"
        parent.finish_time = None
        parent.detail = {
            **parent.detail,
            "_worker_runtime_v2": {
                **parent.detail["_worker_runtime_v2"],
                "failure_status": "PARSING_FAILED",
            },
        }
        session.get(ChunkParseTask, chunk_ids[-1]).status = "FAILED"
        inflight = session.get(ChunkParseTask, chunk_ids[0])
        inflight.phase = 1
        inflight.status = "RUNNING"
        inflight.start_time = now
        inflight.lease_owner = "inflight-worker"
        inflight.lease_expire_time = now if lease_expired else now + timedelta(minutes=5)
        before = parent.detail

    with pytest.raises(UploadStateError):
        service.retry_parse(
            upload.upload_session_id,
            tenant_id="local-default",
            expected_job_version=1, expected_lease_version=0,
        )
    with factory() as session:
        parent = session.get(VideoParseTask, task_id)
        assert parent.status == "PARSING" and parent.finish_time is None
        assert parent.detail == before
        assert session.get(ChunkParseTask, chunk_ids[-1]).status == "FAILED"
        assert session.get(ChunkParseTask, chunk_ids[0]).lease_owner == "inflight-worker"


@pytest.mark.parametrize(
    ("model", "status", "phase"),
    [
        (ChunkParseTask, "RUNNING", 1),
        (IntervalParseTask, "REFINING", None),
        (ChunkParseTask, "RUNNING", 3),
    ],
)
def test_manual_retry_rejects_active_execution_lease_without_partial_reset(
    prepared_v2_retry,
    model,
    status,
    phase,
    monkeypatch,
):
    service, upload, task_id, factory, chunk_ids, interval_ids, now = (
        prepared_v2_retry
    )

    def forbidden_loader():
        raise AssertionError("active execution must be rejected before loading defaults")

    monkeypatch.setattr(service, "_resolve_parse_config", forbidden_loader)
    target_id = (
        chunk_ids[0]
        if model is ChunkParseTask
        else interval_ids[0]
    )
    expires = now + timedelta(minutes=5)
    with factory.begin() as session:
        parent = session.get(VideoParseTask, task_id)
        parent.status = "PARSING_FAILED"
        target = session.get(model, target_id)
        target.status = status
        if phase is not None:
            target.phase = phase
        target.start_time = now
        target.lease_owner = "active-worker"
        target.lease_expire_time = expires
        target.finish_time = None
        failed_child = session.get(ChunkParseTask, chunk_ids[-1])
        failed_child.status = "FAILED"

    with pytest.raises(UploadStateError, match="execution has not been reclaimed"):
        service.retry_parse(
            upload.upload_session_id,
            tenant_id="local-default",
            expected_job_version=1, expected_lease_version=0,
        )

    with factory() as session:
        assert session.get(VideoParseTask, task_id).status == "PARSING_FAILED"
        target = session.get(model, target_id)
        assert (target.status, target.lease_owner, target.lease_expire_time) == (
            status,
            "active-worker",
            expires,
        )
        assert session.get(
            ChunkParseTask, chunk_ids[-1]
        ).status == "FAILED"


@pytest.mark.parametrize("invalid", ["missing_refine", "wrong_members"])
def test_indexing_retry_rejects_invalid_sources_without_resetting_children(prepared_v2_retry, invalid):
    service, upload, task_id, factory, chunk_ids, interval_ids, _now = prepared_v2_retry
    with factory.begin() as session:
        session.get(VideoParseTask, task_id).status = "INDEXING_FAILED"
        session.get(ChunkParseTask, chunk_ids[0]).status = "FAILED"
        interval = session.get(IntervalParseTask, interval_ids[0])
        interval.status = "COMPLETE"
        if invalid == "missing_refine":
            session.delete(session.get(IntervalParseContent, interval.id))
        elif invalid == "wrong_members":
            interval.chunk_task_ids = chunk_ids[1:3]
        else:
            session.delete(session.get(IntervalParseTask, interval_ids[-1]))
    with pytest.raises(UploadConflictError):
        service.retry_parse(
            upload.upload_session_id, tenant_id="local-default", expected_job_version=1, expected_lease_version=0,
        )
    with factory() as session:
        assert session.get(VideoParseTask, task_id).status == "INDEXING_FAILED"
        assert session.get(ChunkParseTask, chunk_ids[0]).status == "FAILED"
        assert session.get(IntervalParseTask, interval_ids[0]).status == "COMPLETE"


@pytest.mark.parametrize("conflicting_boundary", [False, True])
def test_parse_retry_resumes_partial_fixed_topology(prepared_v2_retry, conflicting_boundary):
    service, upload, task_id, factory, chunk_ids, interval_ids, _now = prepared_v2_retry
    with factory.begin() as session:
        for interval_id in interval_ids:
            session.delete(session.get(IntervalParseTask, interval_id))
        for child_id in chunk_ids[2:]:
            child = session.get(ChunkParseTask, child_id)
            chunk = session.get(VideoChunk, child.chunk_id)
            session.delete(child)
            session.flush()
            session.delete(chunk)
        if conflicting_boundary:
            first = session.get(ChunkParseTask, chunk_ids[0])
            session.get(VideoChunk, first.chunk_id).end_ms = 2

    if conflicting_boundary:
        with pytest.raises(UploadConflictError, match="fixed chunk topology"):
            service.retry_parse(
                upload.upload_session_id, tenant_id="local-default", expected_job_version=1, expected_lease_version=0,
            )
    else:
        result = service.retry_parse(
            upload.upload_session_id, tenant_id="local-default", expected_job_version=1, expected_lease_version=0,
        )
        assert result.status == "PREPARING"
    with factory() as session:
        assert session.get(VideoParseTask, task_id).status == (
            "PARSING_FAILED" if conflicting_boundary else "PREPARING"
        )
        for child_id in chunk_ids[:2]:
            assert session.get(ChunkParseTask, child_id).status == "COMPLETE"


@pytest.mark.parametrize("terminal_status", ["COMPLETE", "PARSING_FAILED", "INDEXING_FAILED"])
def test_full_reparse_creates_new_v2_version_and_retains_history(
    repository,
    tmp_path,
    terminal_status,
):
    reparsed_at = 1_789_646_400_000
    engine, store, _upload_repository, service = _upload_service(
        repository,
        tmp_path,
        clock=lambda: reparsed_at,
    )
    upload = _create(service)
    for number, data in enumerate((b"abcd", b"efgh", b"ij")):
        _put(service, upload.upload_session_id, number, data)
    completed = service.complete(
        upload.upload_session_id,
        tenant_id="local-default",
    )
    completed = _with_historical_v1_job(engine, completed, service.parse_defaults)
    assert completed.video_parse_task_id is not None
    video_chunk_id = str(uuid4())
    chunk_task_id = str(uuid4())
    chunk_content_id = str(uuid4())
    with create_session_factory(engine).begin() as session:
        job = session.get(SeriesParseJob, completed.job_id)
        job.status = ParseJobStatus.SUCCEEDED.value
        job.stage = ParseJobStage.COMPLETED.value
        job.progress = 100
        job.attempt_count = 3
        job.max_attempts = 3
        job.version = 7
        job.start_time = reparsed_at - timedelta_to_ms(timedelta(hours=1))
        job.finish_time = reparsed_at - timedelta_to_ms(timedelta(minutes=30))
        legacy_job_before = (
            job.status,
            job.stage,
            job.progress,
            job.attempt_count,
            job.max_attempts,
            job.version,
            job.start_time,
            job.finish_time,
        )
        task = session.get(VideoParseTask, completed.video_parse_task_id)
        original_config = task.parse_config
        task.status = terminal_status
        task.attempt_count = 1
        task.start_time = unix_time_ms_to_datetime(job.start_time)
        task.finish_time = unix_time_ms_to_datetime(job.finish_time)
        task.processing_duration_ms = timedelta_to_ms(timedelta(minutes=30))
        task.detail = {"result": "published"}
        video = session.get(Video, completed.video_id)
        video.parse_status = ParseJobStatus.SUCCEEDED.value
        video.parse_progress = 100
        session.get(Series, upload.series_id).status = "READY"
        session.add(VideoChunk(
            id=video_chunk_id,
            video_id=completed.video_id,
            sequence_no=0,
            start_ms=0,
            end_ms=12_000,
            window_start_ms=0,
            window_end_ms=12_000,
            chunk_media_ref="artifact://test/old-chunk.mp4",
            analysis_window_media_ref="artifact://test/old-window.mp4",
            transcript=[],
            baseline_visual_caption=None,
            keyframe_uris=[],
        ))
        session.add(ChunkParseTask(
            id=chunk_task_id,
            video_task_id=completed.video_parse_task_id,
            chunk_id=video_chunk_id,
            phase=3, indexed_content_version=1, status="COMPLETE",
            next_execute_time=unix_time_ms_to_datetime(job.start_time),
            retry_count=0,
            lease_version=1,
            start_time=unix_time_ms_to_datetime(job.start_time),
            finish_time=unix_time_ms_to_datetime(job.finish_time),
            detail={"attempt": 1},
        ))
        session.add(ChunkParseContent(
            id=chunk_content_id,
            chunk_task_id=chunk_task_id,
            content={"persons": [], "timeline": []},
            version=1,
            detail={"compact": "old compact result"},
        ))
        session.add(VideoChunkEmbedding(
            chunk_id=video_chunk_id,
            text_input="old searchable facts",
            text_vector=[0.1],
            vision_vector=[0.2],
        ))
        session.add(IndexSyncIntent(
            chunk_id=video_chunk_id,
            operation=IndexSyncOperation.UPSERT.value,
            source_revision="old-revision",
            generation=1,
            status=IndexSyncStatus.SYNCED.value,
            attempt_count=0,
            next_execute_time=reparsed_at,
            sync_time=reparsed_at,
        ))
        session.add(VideoChunkCheckpoint(
            job_id=completed.job_id,
            sequence_no=0,
            chunk_id=video_chunk_id,
            stage=VideoChunkCheckpointStage.INDEXED.value,
            start_ms=0,
            end_ms=12_000,
        ))
        session.add(ParseUsageAttempt(
            job_id=completed.job_id,
            video_task_id=completed.video_parse_task_id,
            attempt_count=3,
            lease_version=3,
            start_time=unix_time_ms_to_datetime(
                reparsed_at - timedelta_to_ms(timedelta(hours=1))
            ),
            recording_complete=True,
        ))
    old_attempt_artifact = (
        store.work_directory(completed.job_id) / "old-attempt.json"
    )
    old_attempt_artifact.write_text("keep", encoding="utf-8")
    replacement_config = VideoParseV2Config.model_validate(
        original_config
    ).model_copy(update={"batch_max_size": 4})
    session_factory = create_session_factory(engine)
    jobs = ParseJobRepository(session_factory)
    incompatible_config = replacement_config.model_copy(
        update={"chunk_duration": 5}
    )
    with pytest.raises(UploadConflictError, match="fixed chunk topology"):
        service.reparse(
            upload.upload_session_id,
            tenant_id="local-default",
            expected_job_version=1,
            parse_config=incompatible_config,
        )
    assert len(jobs.list_video_parse_tasks(completed.video_id)) == 1

    reparsed = service.reparse(
        upload.upload_session_id,
        tenant_id="local-default",
        expected_job_version=1,
        parse_config=replacement_config,
    )

    assert reparsed.video_parse_task_id != completed.video_parse_task_id
    assert reparsed.video_id == completed.video_id
    assert (reparsed.status, reparsed.version) == (
        "PENDING",
        2,
    )
    assert reparsed.parse_config == replacement_config.model_dump(mode="json")
    assert reparsed.next_execute_time == unix_time_ms_to_datetime(reparsed_at)
    assert reparsed.chunk_count == 1
    assert reparsed.max_attempts == replacement_config.execution.max_retry_count + 1
    tasks = jobs.list_video_parse_tasks(completed.video_id)
    assert [task.video_parse_task_id for task in tasks] == [
        completed.video_parse_task_id,
        reparsed.video_parse_task_id,
    ]
    assert [task.version for task in tasks] == [1, 2]
    assert tasks[0].parse_config == original_config
    assert tasks[1].parse_config == replacement_config.model_dump(mode="json")
    assert jobs.latest_video_parse_task(completed.video_id) == reparsed
    with session_factory() as session:
        old_task = session.get(VideoParseTask, completed.video_parse_task_id)
        assert (old_task.status, old_task.detail) == (
            terminal_status,
            {"result": "published"},
        )
        assert session.get(VideoParseTask, reparsed.video_parse_task_id) is not None
        old_content = session.get(ChunkParseContent, chunk_content_id)
        assert old_content.chunk_task_id == chunk_task_id
        assert old_content.detail == {"compact": "old compact result"}
        assert session.get(ChunkParseTask, chunk_task_id) is not None
        assert session.get(VideoChunk, video_chunk_id) is not None
        assert session.get(VideoChunkEmbedding, video_chunk_id) is not None
        intent = session.get(IndexSyncIntent, video_chunk_id)
        assert intent.status == IndexSyncStatus.SYNCED.value
        assert session.get(
            ParseUsageAttempt,
            (completed.job_id, 3, 3),
        ) is not None
        assert session.get(
            VideoChunkCheckpoint,
            (completed.job_id, 0),
        ) is not None
        job = session.get(SeriesParseJob, completed.job_id)
        assert (
            job.status,
            job.stage,
            job.progress,
            job.attempt_count,
            job.max_attempts,
            job.version,
            job.start_time,
            job.finish_time,
        ) == legacy_job_before
        video = session.get(Video, completed.video_id)
        assert (video.parse_status, video.parse_progress) == (
            ParseJobStatus.SUCCEEDED.value,
            100,
        )
        assert session.get(Series, upload.series_id).status == "READY"
    assert old_attempt_artifact.read_text(encoding="utf-8") == "keep"

    with pytest.raises(UploadConflictError, match="version changed"):
        service.reparse(
            upload.upload_session_id,
            tenant_id="local-default",
            expected_job_version=1,
        )
    assert len(jobs.list_video_parse_tasks(completed.video_id)) == 2


@pytest.mark.parametrize(
    "cleanup_state",
    ["prepare_interrupted", "success", "bulk_failure", "not_visible", "unavailable"],
)
def test_v2_delete_cleans_chunks_and_recovers_index_cleanup(repository, tmp_path, cleanup_state):
    class ParseIndexStore(_VideoParseIndexStore):
        def __init__(self):
            super().__init__(1024, 1024)
            self.documents = set()
            self.deleted = []
            self.verified = []

        def bulk_delete(self, keys, *, refresh=False):
            self.deleted.append((tuple(keys), refresh))
            if cleanup_state == "bulk_failure":
                raise RuntimeError("index cleanup failed")
            self.documents.difference_update(keys)

        def verify_absent(self, keys):
            self.verified.append(tuple(keys))
            return cleanup_state != "not_visible" and not self.documents.intersection(keys)

    parse_index = ParseIndexStore()
    now = unix_time_ms_to_datetime(1_789_560_000_000)
    clock = [now]
    engine, store, upload_repository, service = _upload_service(
        repository, tmp_path, clock=lambda: clock[0],
        video_parse_index_store=parse_index,
    )
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys = ON")
    upload = _create(service)
    for number, data in enumerate((b"abcd", b"efgh", b"ij")):
        _put(service, upload.upload_session_id, number, data)
    completed = service.complete(upload.upload_session_id, tenant_id="local-default")
    assert completed.job_id is None
    factory = create_session_factory(engine)
    lease = MySqlVideoParseQueue(factory, video_concurrency=3).claim_video(
        "prepare", lease_duration=timedelta(minutes=1), now=now,
    )
    assert lease is not None
    parse_repository = VideoParseRepository(factory)
    chunks = parse_repository.ensure_video_chunks(lease, [(0, 0, 12_000)], now=now)
    chunk_id = chunks[0].video_chunk_id
    prepared = None
    if cleanup_state != "prepare_interrupted":
        prepared = parse_repository.ensure_parse_tasks(lease, [chunk_id], [(0, 1)], now=now)
        with factory.begin() as session:
            task = session.get(VideoParseTask, completed.video_parse_task_id)
            task.status = "COMPLETE"
            task.lease_owner = None
            task.lease_expire_time = None
            task.heartbeat_time = None
            child = session.get(ChunkParseTask, prepared.chunk_parse_task_ids[0])
            child.phase = 3
            child.status = "COMPLETE"
            child.indexed_content_version = 1
            interval = session.get(IntervalParseTask, prepared.interval_parse_task_ids[0])
            interval.status = "COMPLETE"
            interval.ready_mask = 1
        parse_index.documents.add((completed.video_parse_task_id, chunk_id))
    expected_keys = tuple(sorted(parse_index.documents))
    if cleanup_state == "prepare_interrupted":
        with pytest.raises(UploadStateError, match="being parsed"):
            service.delete_session(upload.upload_session_id, tenant_id="local-default")
        with factory() as session:
            task = session.get(VideoParseTask, completed.video_parse_task_id)
            assert not task.is_deleted and task.capacity_acquired
    # A crashed Prepare lease must expire before task-level deletion can fence it.
    clock[0] += timedelta(minutes=2)
    if cleanup_state == "unavailable":
        service._video_parse_index_store = None
    failed = cleanup_state in {"bulk_failure", "not_visible", "unavailable"}
    if failed:
        with pytest.raises(RuntimeError, match="cleanup"):
            service.delete_session(upload.upload_session_id, tenant_id="local-default")
        with factory() as session:
            tombstone = session.get(UploadSession, upload.upload_session_id)
            assert tombstone.last_error_code == "DELETION_PENDING"
            assert tombstone.video_id == completed.video_id
            assert session.get(Video, completed.video_id) is not None
            assert session.get(VideoChunk, chunk_id) is not None
            task = session.get(VideoParseTask, completed.video_parse_task_id)
            assert task.is_deleted and not task.capacity_acquired
            assert session.get(ChunkParseTask, prepared.chunk_parse_task_ids[0]) is not None
        cleanup_state = "success"
        service._video_parse_index_store = parse_index
        assert service.reconcile_deletions() == (1, 0)
    else:
        service.delete_session(upload.upload_session_id, tenant_id="local-default")

    deleted = upload_repository.get_session(upload.upload_session_id)
    cleanup_calls = (list(parse_index.deleted), list(parse_index.verified))
    repeated = service.delete_session(upload.upload_session_id, tenant_id="local-default")
    assert deleted.status == repeated.status == "EXPIRED"
    assert deleted.version == repeated.version
    assert deleted.video_id is None and deleted.last_error_code is None
    assert (parse_index.deleted, parse_index.verified) == cleanup_calls
    assert not parse_index.documents
    assert all(keys == expected_keys and refresh == "wait_for" for keys, refresh in parse_index.deleted)
    assert all(keys == expected_keys for keys in parse_index.verified)
    assert bool(parse_index.deleted) == bool(expected_keys)
    assert bool(parse_index.verified) == bool(expected_keys)
    assert not (
        store.base_directory / "media" / "series" / upload.series_id / "videos" / completed.video_id
    ).exists()
    with factory() as session:
        assert session.get(Video, completed.video_id) is None
        assert session.get(VideoChunk, chunk_id) is None
        assert session.get(VideoParseTask, completed.video_parse_task_id) is None
        assert list(session.scalars(select(ChunkParseTask))) == []
        assert list(session.scalars(select(IntervalParseTask))) == []


def test_delete_completed_upload_removes_full_task_aggregate(repository, tmp_path):
    engine, store, upload_repository, service = _upload_service(repository, tmp_path)
    upload = _create(service)
    for number, data in enumerate((b"abcd", b"efgh", b"ij")):
        _put(service, upload.upload_session_id, number, data)
    completed = service.complete(upload.upload_session_id, tenant_id="local-default")
    completed = _with_historical_v1_job(engine, completed, service.parse_defaults)
    now = 1_789_560_000_000
    video_chunk_id = str(uuid4())
    call_id = str(uuid4())
    snapshot_id = str(uuid4())
    second_job_id = str(uuid4())
    chunk_parse_task_id = str(uuid4())
    with create_session_factory(engine).begin() as session:
        job = session.get(SeriesParseJob, completed.job_id)
        job.status = ParseJobStatus.FAILED.value
        job.last_error = "fixture failure"
        session.add(SeriesParseJob(
            id=second_job_id,
            series_id=upload.series_id,
            video_id=completed.video_id,
            pipeline_version="pipeline-v2",
            status=ParseJobStatus.FAILED.value,
            stage=ParseJobStage.PARSING.value,
            progress=40,
            attempt_count=1,
            max_attempts=3,
            version=1,
            next_execute_time=now,
            lease_version=0,
            last_error="second pipeline failure",
        ))
        session.add(VideoChunkCheckpoint(
            job_id=completed.job_id,
            sequence_no=0,
            chunk_id=video_chunk_id,
            stage=VideoChunkCheckpointStage.INDEXED.value,
        ))
        session.add(VideoChunk(
            id=video_chunk_id,
            video_id=completed.video_id,
            sequence_no=0,
            start_ms=0,
            end_ms=1000,
            window_start_ms=0,
            window_end_ms=1000,
            chunk_media_ref="artifact://test/chunk.mp4",
            analysis_window_media_ref="artifact://test/window.mp4",
            transcript=[],
            baseline_visual_caption=None,
            keyframe_uris=[],
        ))
        session.add(ChunkParseTask(
            id=chunk_parse_task_id,
            video_task_id=completed.video_parse_task_id,
            chunk_id=video_chunk_id,
            status="QUEUED",
            next_execute_time=unix_time_ms_to_datetime(now),
            retry_count=0,
            lease_version=0,
            create_time=datetime_to_unix_time_ms(unix_time_ms_to_datetime(now)),
            update_time=datetime_to_unix_time_ms(unix_time_ms_to_datetime(now)),
        ))
        session.add(VideoChunkInfo(
            chunk_id=video_chunk_id,
            all_time_events=[],
            all_time_event_text="",
            video_compact="compact",
            context_before_compact="",
            context_after_compact="compact",
            window_summary="summary",
            pipeline_version="pipeline-v1",
            is_fallback=False,
            parse_time=now,
        ))
        session.add(VideoChunkEmbedding(
            chunk_id=video_chunk_id,
            text_input="summary",
            text_vector=[0.1],
            vision_vector=[0.2],
        ))
        session.add(ContextSnapshot(
            id=snapshot_id,
            engine_snapshot_id="engine-snapshot",
            series_id=upload.series_id,
            video_id=completed.video_id,
            after_chunk_sequence_no=0,
            compact_text="compact",
            structured_state={},
            source_chunk_id=video_chunk_id,
            create_time=now,
        ))
        session.add(IndexSyncIntent(
            chunk_id=video_chunk_id,
            operation=IndexSyncOperation.UPSERT.value,
            source_revision="revision",
            generation=1,
            status=IndexSyncStatus.SYNCED.value,
            attempt_count=1,
            next_execute_time=now,
            sync_time=now,
        ))
        session.add(ParseUsageAttempt(
            job_id=completed.job_id,
            attempt_count=1,
            lease_version=1,
            start_time=unix_time_ms_to_datetime(now),
            recording_complete=True,
        ))
        session.add(ParseModelCall(
            id=call_id,
            job_id=completed.job_id,
            attempt_count=1,
            lease_version=1,
            tenant_id="local-default",
            series_id=upload.series_id,
            video_id=completed.video_id,
            sequence_no=0,
            chunk_id=video_chunk_id,
            provider="test",
            model="test-model",
            category="parse",
            operation="parse",
            start_time=unix_time_ms_to_datetime(now),
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

    class ParseIndexStore:
        def __init__(self):
            self.deleted = []
            self.verified = []

        def bulk_delete(self, keys, *, refresh=False):
            self.deleted.append((tuple(keys), refresh))

        def verify_absent(self, keys):
            self.verified.append(tuple(keys))
            return True

    parse_index = ParseIndexStore()
    service._video_parse_index_store = parse_index
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys = ON")
    deleted = service.delete_session(
        upload.upload_session_id,
        tenant_id="local-default",
    )
    repeated = service.delete_session(
        upload.upload_session_id,
        tenant_id="local-default",
    )

    assert deleted.status == repeated.status == UploadSessionStatus.EXPIRED.value
    assert repeated.version == deleted.version
    assert deleted.video_id is None and deleted.job_id is None
    expected_parse_key = ((completed.video_parse_task_id, video_chunk_id),)
    assert parse_index.deleted == [(expected_parse_key, "wait_for")]
    assert parse_index.verified == [expected_parse_key]
    assert not job_directory.exists()
    assert not second_job_directory.exists()
    assert not video_directory.exists()
    with create_session_factory(engine)() as session:
        tombstone = session.get(UploadSession, upload.upload_session_id)
        assert tombstone is not None
        assert tombstone.video_id is None and tombstone.job_id is None
        for model, key in (
            (Video, completed.video_id),
            (VideoParseTask, completed.video_parse_task_id),
            (ChunkParseTask, chunk_parse_task_id),
            (SeriesParseJob, completed.job_id),
            (SeriesParseJob, second_job_id),
            (VideoChunk, video_chunk_id),
            (VideoChunkCheckpoint, (completed.job_id, 0)),
            (VideoChunkInfo, video_chunk_id),
            (VideoChunkEmbedding, video_chunk_id),
            (ContextSnapshot, snapshot_id),
            (IndexSyncIntent, video_chunk_id),
            (ParseUsageAttempt, (completed.job_id, 1, 1)),
            (ParseModelCall, call_id),
        ):
            assert session.get(model, key) is None
        assert session.get(Series, upload.series_id).status == "EMPTY"


def test_completed_upload_cleanup_failure_retains_retry_identity(repository, tmp_path, monkeypatch):
    engine, store, _upload_repository, service = _upload_service(repository, tmp_path)
    upload = _create(service)
    for number, data in enumerate((b"abcd", b"efgh", b"ij")):
        _put(service, upload.upload_session_id, number, data)
    completed = service.complete(upload.upload_session_id, tenant_id="local-default")
    completed = _with_historical_v1_job(engine, completed, service.parse_defaults)
    cleanup_video = store.delete_video_artifacts

    def fail_video_cleanup(_series_id, _video_id):
        raise PermissionError("locked")

    monkeypatch.setattr(store, "delete_video_artifacts", fail_video_cleanup)
    with pytest.raises(PermissionError, match="locked"):
        service.delete_session(upload.upload_session_id, tenant_id="local-default")
    with create_session_factory(engine)() as session:
        tombstone = session.get(UploadSession, upload.upload_session_id)
        assert tombstone.status == UploadSessionStatus.EXPIRED.value
        assert tombstone.video_id == completed.video_id
        assert tombstone.job_id == completed.job_id
        assert session.get(Video, completed.video_id) is not None
        assert session.get(SeriesParseJob, completed.job_id) is not None

    monkeypatch.setattr(store, "delete_video_artifacts", cleanup_video)
    assert service.reconcile_deletions() == (1, 0)
    with create_session_factory(engine)() as session:
        assert session.get(Video, completed.video_id) is None
        assert session.get(SeriesParseJob, completed.job_id) is None


def test_reconcile_resumes_active_upload_cleanup_after_begin_crash(repository, tmp_path):
    engine, store, upload_repository, service = _upload_service(repository, tmp_path)
    upload = _create(service)
    _put(service, upload.upload_session_id, 0, b"abcd")
    staging = store.base_directory / "staging" / "uploads" / upload.upload_session_id
    assert staging.exists()

    deletion = upload_repository.begin_delete_session(
        upload.upload_session_id,
        tenant_id="local-default",
        now=unix_time_ms_to_datetime(1_789_560_000_000),
    )
    assert deletion.session.status == UploadSessionStatus.EXPIRED.value
    with create_session_factory(engine)() as session:
        tombstone = session.get(UploadSession, upload.upload_session_id)
        assert tombstone.last_error_code == "DELETION_PENDING"

    assert service.reconcile_deletions() == (1, 0)
    assert not staging.exists()
    with create_session_factory(engine)() as session:
        tombstone = session.get(UploadSession, upload.upload_session_id)
        assert tombstone.last_error_code is None
        assert tombstone.status == UploadSessionStatus.EXPIRED.value


def test_delete_rejects_an_inconsistent_shared_video_reference(repository, tmp_path):
    engine, _store, _upload_repository, service = _upload_service(repository, tmp_path)
    upload = _create(service)
    for number, data in enumerate((b"abcd", b"efgh", b"ij")):
        _put(service, upload.upload_session_id, number, data)
    completed = service.complete(upload.upload_session_id, tenant_id="local-default")
    completed = _with_historical_v1_job(engine, completed, service.parse_defaults)
    shared_upload_id = str(uuid4())
    with create_session_factory(engine).begin() as session:
        original = session.get(UploadSession, upload.upload_session_id)
        session.add(UploadSession(
            id=shared_upload_id,
            tenant_id=original.tenant_id,
            series_id=original.series_id,
            episode_order=original.episode_order + 1,
            upload_sequence=original.upload_sequence + 1,
            filename=original.filename,
            media_type=original.media_type,
            size_bytes=original.size_bytes,
            segment_size_bytes=original.segment_size_bytes,
            segment_count=original.segment_count,
            sha256=original.sha256,
            version=1,
            status=UploadSessionStatus.COMPLETED.value,
            idempotency_key="inconsistent-shared-video",
            request_fingerprint="shared",
            expire_time=original.expire_time,
            finish_time=original.finish_time,
            video_id=completed.video_id,
            job_id=completed.job_id,
        ))

    with pytest.raises(UploadConflictError, match="another upload session"):
        service.delete_session(upload.upload_session_id, tenant_id="local-default")
    with create_session_factory(engine)() as session:
        assert session.get(Video, completed.video_id) is not None
        assert session.get(SeriesParseJob, completed.job_id) is not None
        assert session.get(UploadSession, upload.upload_session_id).status == "COMPLETED"


def test_failed_completion_removes_unregistered_published_video(repository, tmp_path):
    _engine, store, upload_repository, _service = _upload_service(repository, tmp_path)
    service = UploadService(
        upload_repository,
        store,
        config=_config(),
        probe_duration=lambda _path: (_ for _ in ()).throw(RuntimeError("probe failed")),
        parse_defaults=_service.parse_defaults,
    )
    upload = _create(service)
    for number, data in enumerate((b"abcd", b"efgh", b"ij")):
        _put(service, upload.upload_session_id, number, data)

    with pytest.raises(RuntimeError, match="probe failed"):
        service.complete(upload.upload_session_id, tenant_id="local-default")

    videos_directory = (
        store.base_directory / "media" / "series" / upload.series_id / "videos"
    )
    assert not videos_directory.exists() or list(videos_directory.iterdir()) == []
    assert service.get_session(
        upload.upload_session_id,
        tenant_id="local-default",
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
        service.delete_session(upload.upload_session_id, tenant_id="local-default")
    monkeypatch.setattr(store, "delete_upload_staging", cleanup)
    assert service.delete_session(upload.upload_session_id, tenant_id="local-default").status == "EXPIRED"
    assert not (store.base_directory / "staging" / "uploads" / upload.upload_session_id).exists()


def test_deleting_segments_from_an_expired_session_advances_its_visible_version(repository, tmp_path):
    now = 1_789_560_000_000
    _engine, _store, _repo, service = _upload_service(repository, tmp_path, clock=lambda: now)
    upload = _create(service)
    _put(service, upload.upload_session_id, 0, b"abcd")
    now += timedelta_to_ms(timedelta(hours=2))
    expired = service.get_session(upload.upload_session_id, tenant_id="local-default")
    assert expired.status == "EXPIRED" and expired.uploaded_size_bytes == 4
    deleted = service.delete_session(upload.upload_session_id, tenant_id="local-default")
    assert deleted.version > expired.version
    assert deleted.uploaded_size_bytes == 0
    assert service.delete_session(upload.upload_session_id, tenant_id="local-default").version == deleted.version


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

    first = service.complete(upload.upload_session_id, tenant_id="local-default")
    second = service.complete(upload.upload_session_id, tenant_id="local-default")

    assert first.video_id == second.video_id
    assert first.job_id == second.job_id
    assert first.video_parse_task_id == second.video_parse_task_id
    assert first.video_parse_task_id is not None
    assert (
        first.session.initial_video_parse_task_id
        == first.video_parse_task_id
        == second.session.initial_video_parse_task_id
    )
    assert first.session.status == UploadSessionStatus.COMPLETED.value
    with create_session_factory(engine)() as session:
        [video] = list(session.scalars(select(Video)))
        assert list(session.scalars(select(SeriesParseJob))) == []
        [video_parse_task] = list(session.scalars(select(VideoParseTask)))
    assert video is not None
    assert video.filename == "episode-07.mp4"
    assert video.episode_order == video.upload_sequence == 7
    assert video.hash == hashlib.sha256(b"abcdefghij").hexdigest()
    assert video.duration_ms == 12_000
    assert video.parse_status == ParseJobStatus.QUEUED.value
    assert store.resolve(video.source_uri).read_bytes() == b"abcdefghij"
    assert first.job_id is None
    assert video_parse_task.id == first.video_parse_task_id
    assert video_parse_task.video_id == video.id
    assert video_parse_task.status == "PENDING"
    assert video_parse_task.version == 1
    assert video_parse_task.chunk_count == 1
    assert video_parse_task.parse_config == first.session.parse_config


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
        probe_duration=lambda _path: 12_000,
        clock=lambda: now + timedelta_to_ms(timedelta(hours=2)),
        parse_defaults=service.parse_defaults,
    )

    with pytest.raises(UploadStateError, match="expired"):
        _put(expired_service, upload.upload_session_id, 0, b"abcd")
    assert (
        upload_repository.get_session(upload.upload_session_id).status
        == UploadSessionStatus.EXPIRED.value
    )
    renewed = expired_service.create_session(
        tenant_id="local-default",
        series_id="77285f16-3e5b-592e-ba15-8b224b619806",
        episode_order=7,
        original_filename="episode-07.mp4",
        media_type="video/mp4",
        total_size_bytes=10,
        idempotency_key="upload-episode-07-retry",
    )
    assert renewed.upload_session_id != upload.upload_session_id
    assert renewed.status == UploadSessionStatus.UPLOADING.value
    assert renewed.episode_order == renewed.upload_sequence == upload.episode_order


@pytest.mark.parametrize("content_sha256", ["0" * 64, hashlib.sha256(b"abcd").hexdigest()])
def test_source_checksum_and_visible_completion_versions(repository, tmp_path, content_sha256):
    _engine, _store, upload_repository, service = _upload_service(repository, tmp_path)
    upload = service.create_session(
        tenant_id="local-default", series_id="77285f16-3e5b-592e-ba15-8b224b619806",
        episode_order=7, original_filename="episode.mp4", media_type="video/mp4",
        total_size_bytes=4, idempotency_key="checksum", content_sha256=content_sha256,
    )
    assert upload.upload_sequence == 7
    _put(service, upload.upload_session_id, 0, b"abcd", total_size_bytes=4)
    received = service.get_session(upload.upload_session_id, tenant_id="local-default")
    assert received.version > upload.version
    _put(service, upload.upload_session_id, 0, b"abcd", total_size_bytes=4)
    assert service.get_session(upload.upload_session_id, tenant_id="local-default").version == received.version
    if content_sha256 == "0" * 64:
        with pytest.raises(UploadChecksumMismatchError):
            service.complete(upload.upload_session_id, tenant_id="local-default")
        current = service.get_session(upload.upload_session_id, tenant_id="local-default")
        assert current.status == "UPLOADING" and current.missing_segment_numbers == ()
        assert current.job_id is None and current.completion_stage is None
        assert current.version > received.version
        public = project_upload_session(current)
        assert public.last_error.code == "CHECKSUM_MISMATCH"
        assert public.completion_progress is public.estimated_remaining_ms is None
    else:
        completed = service.complete(upload.upload_session_id, tenant_id="local-default")
        assert completed.session.job_id == completed.job_id
        assert completed.session.version > received.version
        assert completed.session.content_sha256 == content_sha256
        assert completed.session.completion_stage is None
        replay = service.complete(upload.upload_session_id, tenant_id="local-default")
        assert replay.session.version == completed.session.version
        assert replay.job_id == completed.job_id
        assert replay.video_parse_task_id == completed.video_parse_task_id


def test_episode_order_is_stable_on_replay_and_used_as_upload_sequence(repository, tmp_path):
    _engine, _store, _repo, service = _upload_service(repository, tmp_path)
    first = _create(service)
    second = service.create_session(
        tenant_id="local-default", series_id=first.series_id, episode_order=8,
        original_filename="episode-08.mp4", media_type="video/mp4", total_size_bytes=4,
        idempotency_key="second-upload",
    )
    assert first.episode_order == first.upload_sequence == 7
    assert second.episode_order == second.upload_sequence == 8
    assert _create(service).upload_sequence == first.upload_sequence
    with pytest.raises(UploadConflictError, match="idempotency"):
        service.create_session(
            tenant_id="local-default",
            series_id=first.series_id,
            episode_order=9,
            original_filename=first.original_filename,
            media_type=first.media_type,
            total_size_bytes=first.total_size_bytes,
            idempotency_key="upload-episode-07",
        )
    with pytest.raises(UploadConflictError, match="episode"):
        service.create_session(
            tenant_id="local-default",
            series_id=first.series_id,
            episode_order=7,
            original_filename="duplicate-slot.mp4",
            media_type="video/mp4",
            total_size_bytes=4,
            idempotency_key="duplicate-slot",
        )


@pytest.mark.parametrize("first_optional,changed_optional", [
    ({"content_sha256": "0" * 64}, {}),
    ({}, {"content_sha256": hashlib.sha256(b"abcd").hexdigest()}),
])
def test_upload_key_keeps_optional_original_request_parameters(repository, tmp_path, first_optional, changed_optional):
    _engine, _store, _repo, service = _upload_service(repository, tmp_path)
    request = {
        "tenant_id": "local-default", "series_id": "77285f16-3e5b-592e-ba15-8b224b619806",
        "episode_order": 7, "original_filename": "episode.mp4", "media_type": "video/mp4",
        "total_size_bytes": 4, "idempotency_key": "optional-parameters",
    }
    first = service.create_session(**request, **first_optional)
    if not first_optional and "content_sha256" in changed_optional:
        _put(service, first.upload_session_id, 0, b"abcd", total_size_bytes=4)
        service.complete(first.upload_session_id, tenant_id="local-default")
    with pytest.raises(UploadConflictError):
        service.create_session(**request, **changed_optional)
    replay = service.create_session(**request, **first_optional)
    assert replay.upload_session_id == first.upload_session_id


@pytest.mark.parametrize("episode_order", [None, True, 0, -1, 2_147_483_648])
def test_episode_order_must_be_a_positive_signed_32_bit_integer(
    repository,
    tmp_path,
    episode_order,
):
    _engine, _store, _repo, service = _upload_service(repository, tmp_path)
    with pytest.raises(ValueError):
        service.create_session(
            tenant_id="local-default",
            series_id="77285f16-3e5b-592e-ba15-8b224b619806",
            episode_order=episode_order,
            original_filename="episode.mp4",
            media_type="video/mp4",
            total_size_bytes=4,
            idempotency_key=f"invalid-order-{episode_order!r}",
        )


def test_episode_order_conflicts_with_existing_video_in_the_same_series(
    repository,
    tmp_path,
):
    engine, _store, _repo, service = _upload_service(repository, tmp_path)
    with create_session_factory(engine).begin() as session:
        session.add(Video(
            id="5772855a-8c93-440c-aae1-91c9cf9eebf5",
            series_id="77285f16-3e5b-592e-ba15-8b224b619806",
            episode_order=9,
            upload_sequence=9,
            filename="existing.mp4",
            source_uri="artifact://test/existing.mp4",
            hash="0" * 64, size_bytes=4, duration_ms=1000,
            upload_status="COMPLETED", parse_status="SUCCEEDED", upload_progress=100, parse_progress=100,
        ))
    with pytest.raises(UploadConflictError, match="episode"):
        service.create_session(
            tenant_id="local-default",
            series_id="77285f16-3e5b-592e-ba15-8b224b619806",
            episode_order=9,
            original_filename="conflict.mp4",
            media_type="video/mp4",
            total_size_bytes=4,
            idempotency_key="video-order-conflict",
        )


def test_legacy_session_without_fingerprint_compares_file_fields(repository, tmp_path):
    engine, store, upload_repository, service = _upload_service(repository, tmp_path)
    first = _create(service)
    with create_session_factory(engine).begin() as session:
        upload = session.get(UploadSession, first.upload_session_id)
        upload.request_fingerprint = None
    changed_config_service = UploadService(
        upload_repository, store, config=replace(_config(), preferred_segment_size_bytes=2),
        parse_defaults=service.parse_defaults,
    )
    request = {
        "tenant_id": "local-default", "series_id": first.series_id,
        "episode_order": first.episode_order,
        "original_filename": first.original_filename, "media_type": first.media_type,
        "total_size_bytes": first.total_size_bytes, "idempotency_key": "upload-episode-07",
    }
    replay = changed_config_service.create_session(**request)
    assert replay.upload_session_id == first.upload_session_id
    assert replay.preferred_segment_size_bytes == first.preferred_segment_size_bytes
    for changed in (
        {"episode_order": 8},
        {"original_filename": "changed.mp4"},
        {"total_size_bytes": 8},
        {"content_sha256": "0" * 64},
    ):
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
        parse_defaults=service.parse_defaults,
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

def test_upload_creator_stays_stable_while_retry_actor_is_persisted(repository, tmp_path):
    engine, _, _, service = _upload_service(repository, tmp_path)
    upload = service.create_session(
        tenant_id="local-default", created_by_user_id="user-A",
        series_id="77285f16-3e5b-592e-ba15-8b224b619806",
        episode_order=7, original_filename="episode-07.mp4", media_type="video/mp4",
        total_size_bytes=10, idempotency_key="owned-upload",
    )
    for number, data in enumerate((b"abcd", b"efgh", b"ij")):
        _put(service, upload.upload_session_id, number, data)
    completed = service.complete(upload.upload_session_id, tenant_id="local-default")
    factory = create_session_factory(engine)
    with factory.begin() as session:
        task = session.get(VideoParseTask, completed.video_parse_task_id)
        assert task.creator_id == "user-A"
        assert task.detail["_worker_runtime_v2"]["actor_user_id"] == "user-A"
        assert completed.job_id is None
        assert list(session.scalars(select(SeriesParseJob))) == []
        assert session.get(UploadSession, upload.upload_session_id).creator_id == "user-A"
        task.status = "PARSING_FAILED"
    retried = service.retry_parse(
        upload.upload_session_id, tenant_id="local-default", expected_job_version=1, expected_lease_version=0, actor_user_id="user-B",
    )
    assert retried.video_parse_task_id == completed.video_parse_task_id
    assert retried.created_by_user_id == "user-A"
    assert retried.detail["_worker_runtime_v2"]["actor_user_id"] == "user-B"
    with factory.begin() as session:
        session.get(VideoParseTask, completed.video_parse_task_id).status = "COMPLETE"
    reparsed = service.reparse(
        upload.upload_session_id, tenant_id="local-default", expected_job_version=1, actor_user_id="user-C",
    )
    assert reparsed.video_parse_task_id != completed.video_parse_task_id
    assert reparsed.created_by_user_id == "user-C"
    assert reparsed.detail["_worker_runtime_v2"]["actor_user_id"] == "user-C"
    with factory() as session:
        old = session.get(VideoParseTask, completed.video_parse_task_id)
        assert old.creator_id == "user-A"
        assert old.detail["_worker_runtime_v2"]["actor_user_id"] == "user-B"


def test_repeated_old_retry_cannot_reset_a_later_failure_budget(prepared_v2_retry):
    service, upload, task_id, factory, chunk_ids, _interval_ids, now = prepared_v2_retry
    with factory.begin() as session:
        parent = session.get(VideoParseTask, task_id)
        parent.status = "INDEXING_FAILED"
        child = session.get(ChunkParseTask, chunk_ids[0])
        child.status = "FAILED"
        child.retry_count = 2
    first = service.retry_parse(upload.upload_session_id, tenant_id="local-default",
                                expected_job_version=1, expected_lease_version=0)
    assert first.lease_version == 1
    with factory.begin() as session:
        parent = session.get(VideoParseTask, task_id)
        parent.status = "INDEXING_FAILED"
        parent.finish_time = now
        child = session.get(ChunkParseTask, chunk_ids[0])
        assert (child.phase, child.status, child.retry_count, child.lease_version) == (3, "QUEUED", 0, 5)
        child.status = "FAILED"
        child.retry_count = 2
        child.lease_version = 7
    with pytest.raises(UploadConflictError, match="version or lease changed"):
        service.retry_parse(upload.upload_session_id, tenant_id="local-default",
                            expected_job_version=1, expected_lease_version=0)
    with factory() as session:
        child = session.get(ChunkParseTask, chunk_ids[0])
        assert (child.phase, child.status, child.retry_count, child.lease_version) == (3, "FAILED", 2, 7)
        assert session.get(VideoParseTask, task_id).lease_version == 1
    second = service.retry_parse(upload.upload_session_id, tenant_id="local-default",
                                 expected_job_version=1, expected_lease_version=1)
    assert second.lease_version == 2
    with factory() as session:
        child = session.get(ChunkParseTask, chunk_ids[0])
        assert (child.phase, child.status, child.retry_count, child.lease_version) == (3, "QUEUED", 0, 8)


def test_index_failure_retry_returns_to_parsing_for_ununderstood_later_chunk(prepared_v2_retry):
    service, upload, task_id, factory, chunk_ids, interval_ids, _now = prepared_v2_retry
    with factory.begin() as session:
        parent = session.get(VideoParseTask, task_id)
        parent.status = "INDEXING_FAILED"
        frozen_config = parent.parse_config
        session.get(ChunkParseTask, chunk_ids[0]).status = "FAILED"
        later = session.get(ChunkParseTask, chunk_ids[-1])
        later.phase, later.status = 1, "QUEUED"
        later.indexed_content_version = None
        session.delete(session.scalar(select(ChunkParseContent).where(ChunkParseContent.chunk_task_id == later.id)))
        window = session.get(IntervalParseTask, interval_ids[-1])
        window.status, window.ready_mask = "WAITING", 1
        session.delete(session.get(IntervalParseContent, window.id))
    retried = service.retry_parse(upload.upload_session_id, tenant_id="local-default",
                                  expected_job_version=1, expected_lease_version=0)
    assert retried.status == "PARSING"
    assert retried.parse_config == frozen_config
    with factory() as session:
        failed_index = session.get(ChunkParseTask, chunk_ids[0])
        later = session.get(ChunkParseTask, chunk_ids[-1])
        assert (failed_index.phase, failed_index.status, failed_index.retry_count) == (3, "QUEUED", 0)
        assert (later.phase, later.status, later.lease_version) == (1, "QUEUED", 4)
        assert session.get(IntervalParseTask, interval_ids[0]).status == "COMPLETE"


def test_index_retry_with_missing_topology_resumes_preparation_and_preserves_results(prepared_short_v2_retry):
    service, upload, task_id, factory, chunk_ids, interval_id, _now = prepared_short_v2_retry
    with factory.begin() as session:
        before = [(row.id, row.content, row.version) for row in session.scalars(select(ChunkParseContent))]
        session.delete(session.get(IntervalParseTask, interval_id))
    retried = service.retry_parse(upload.upload_session_id, tenant_id="local-default",
                                 expected_job_version=1, expected_lease_version=0)
    assert retried.status == "PREPARING"
    with factory() as session:
        child = session.get(ChunkParseTask, chunk_ids[0])
        assert (child.phase, child.status) == (3, "QUEUED")
        assert [(row.id, row.content, row.version) for row in session.scalars(select(ChunkParseContent))] == before


@pytest.mark.parametrize("field", ["parseConfig", "parse_config", "videoParseConfig", "video_parse_config"])
def test_upload_request_accepts_one_configuration_field_and_rejects_competing_fields(field):
    raw = default_video_parse_v2_config().model_dump(mode="json")
    raw["indexing"].pop("index_generation")
    request = UploadReparseRequest.model_validate({field: raw})
    assert request.parse_config.model_dump(mode="json") == raw
    assert set(request.model_dump(by_alias=True)) == {"parseConfig"}
    with pytest.raises(ValidationError):
        UploadReparseRequest.model_validate({"parseConfig": raw, "videoParseConfig": raw})
    create = UploadSessionCreateRequest.model_validate({
        "tenantId": "workspace", "episodeOrder": 1, "originalFilename": "clip.mp4",
        "mediaType": "video/mp4", "totalSizeBytes": 10, field: raw,
    })
    assert create.parse_config == request.parse_config


def test_upload_keeps_one_complete_v2_snapshot_without_losing_optional_stages(repository, tmp_path):
    engine, _store, _repo, service = _upload_service(repository, tmp_path)
    raw = service.parse_defaults.to_video_parse_v2().model_dump(mode="json")
    raw["models"].update({key: dict(raw["models"]["understanding"])
                          for key in ("refine", "ocr", "prefix_context")})
    raw.update(ocr_enabled=True, compact_enabled=True, prefix_context={
        "series_compact_max_chars": 3000, "video_compact_max_chars": 1000, "max_tool_calls": 2,
    })
    selected = VideoParseV2Config.model_validate(raw)
    upload = service.create_session(
        tenant_id="local-default", series_id="77285f16-3e5b-592e-ba15-8b224b619806",
        episode_order=7, original_filename="episode-07.mp4", media_type="video/mp4",
        total_size_bytes=10, idempotency_key="all-v2-options", parse_config=selected,
    )
    assert upload.parse_config == raw
    assert "video_parse_config" not in UploadSession.__table__.columns
    assert "video_parse_config" not in {item.name for item in fields(UploadSessionState)}
    public = project_upload_session(upload).model_dump(by_alias=True)
    assert public["parseConfig"] == raw and "videoParseConfig" not in public
    for number, data in enumerate((b"abcd", b"efgh", b"ij")):
        _put(service, upload.upload_session_id, number, data)
    completed = service.complete(upload.upload_session_id, tenant_id="local-default")
    with create_session_factory(engine)() as session:
        assert session.get(VideoParseTask, completed.video_parse_task_id).parse_config == raw
        assert list(session.scalars(select(SeriesParseJob))) == []


@pytest.mark.parametrize("generation", [7, MAX_INDEX_GENERATION + 1])
def test_upload_rejects_index_generation_outside_the_active_contract(repository, tmp_path, generation):
    engine, _store, _repo, service = _upload_service(repository, tmp_path)
    raw = service.parse_defaults.to_video_parse_v2().model_dump(mode="json")
    raw["indexing"]["index_generation"] = generation
    selected = VideoParseV2Config.model_validate(raw)
    with pytest.raises(ValueError, match="index_generation"):
        service.create_session(
            tenant_id="local-default", series_id="77285f16-3e5b-592e-ba15-8b224b619806",
            episode_order=7, original_filename="episode-07.mp4", media_type="video/mp4",
            total_size_bytes=10, idempotency_key="wrong-generation", parse_config=selected,
        )
    with create_session_factory(engine)() as session:
        assert list(session.scalars(select(UploadSession))) == []


def test_legacy_upload_binds_generation_once_and_reparse_adopts_current_generation(repository, tmp_path):
    engine, _store, _repo, service = _upload_service(repository, tmp_path)
    upload = _create(service)
    factory = create_session_factory(engine)
    with factory.begin() as session:
        stored = session.get(UploadSession, upload.upload_session_id)
        raw = dict(stored.parse_config)
        raw["indexing"] = {key: value for key, value in raw["indexing"].items() if key != "index_generation"}
        stored.parse_config = raw
    # Old metadata stays readable before the first Parse task is accepted.
    public = project_upload_session(service.get_session(upload.upload_session_id, tenant_id="local-default"))
    assert "index_generation" not in public.parse_config.model_dump()["indexing"]
    for number, data in enumerate((b"abcd", b"efgh", b"ij")):
        _put(service, upload.upload_session_id, number, data)
    completed = service.complete(upload.upload_session_id, tenant_id="local-default")
    service._parse_defaults = service.parse_defaults.model_copy(update={"indexing":
        service.parse_defaults.indexing.model_copy(update={"index_generation": 7})})
    # Completed-upload replay does not reinterpret or validate its old execution config.
    repeated = service.complete(upload.upload_session_id, tenant_id="local-default")
    assert repeated.video_parse_task_id == completed.video_parse_task_id
    with factory.begin() as session:
        task = session.get(VideoParseTask, completed.video_parse_task_id)
        old_config = task.parse_config
        assert old_config["indexing"]["index_generation"] == 1
        task.status = "PARSING_FAILED"
    retried = service.retry_parse(upload.upload_session_id, tenant_id="local-default",
                                 expected_job_version=1, expected_lease_version=0)
    assert retried.parse_config == old_config
    with factory.begin() as session:
        session.get(VideoParseTask, completed.video_parse_task_id).status = "COMPLETE"
    reparsed = service.reparse(upload.upload_session_id, tenant_id="local-default", expected_job_version=1)
    assert reparsed.version == 2
    assert reparsed.parse_config == {**old_config, "indexing": {**old_config["indexing"], "index_generation": 7}}
    with factory() as session:
        assert session.get(VideoParseTask, completed.video_parse_task_id).parse_config == old_config
        assert session.get(UploadSession, upload.upload_session_id).parse_config == old_config



def test_upload_completion_rejects_frozen_encoder_contract_drift(repository, tmp_path):
    engine, _store, _repo, service = _upload_service(
        repository, tmp_path, video_parse_index_store=_VideoParseIndexStore(2, 2),
    )
    upload = _create(service)
    for number, data in enumerate((b"abcd", b"efgh", b"ij")):
        _put(service, upload.upload_session_id, number, data)
    service._video_parse_query_encoder = replace(
        service._video_parse_query_encoder, encoder_revision="changed-adapter-revision",
    )
    with pytest.raises(UploadConflictError, match="frozen configuration"):
        service.complete(upload.upload_session_id, tenant_id="local-default")
    with create_session_factory(engine)() as session:
        record = session.get(UploadSession, upload.upload_session_id)
        assert record.parse_config == upload.parse_config
        assert record.status == "UPLOADING" and record.initial_task_id is None
        assert list(session.scalars(select(VideoParseTask))) == []
