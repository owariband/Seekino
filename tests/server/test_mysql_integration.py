from __future__ import annotations

import os
import hashlib
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
from threading import Barrier, Event, current_thread
from uuid import uuid4

import pytest

from tests.server.test_video_parse_repository import _refinement_inputs
from sqlalchemy import String, delete, event, func, inspect, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from server.database import (
    DatabaseEnvironment,
    create_database_engine,
    create_schema,
    create_session_factory,
)
from server.model.video import Series, Video, VideoChunk
from server.model.job import (
    ChunkParseContent,
    ChunkParseTask,
    IntervalParseContent,
    IntervalParseTask,
    SeriesParseJob,
    VideoParseTask,
)
from server.model.agent import AgentConversation, AgentMessage
from server.model.parse_usage import ParseModelCall, ParseUsageAttempt
from server.model.upload import UploadSession, UploadSessionStatus
from server.mq.video_parse_queue import MySqlVideoParseQueue
from server.utils.time import now_unix_time_ms, unix_time_ms_to_datetime
from server.repository.agent import AgentRepository
from server.model.dto.jobDTO import ChunkParseLease, IntervalParseLease, PrefixParseLease
from engine.common.config import VideoParseRuntimeConfig
from gateway.config import load_config
from server.config.media import ImageUploadConfig
from server.config.parse_profile import ParseConfig, default_video_parse_v2_config
from server.repository.library import LibraryRepository
from server.repository.upload import UploadConflictError, UploadRepository, UploadStateError
from server.service.video_parse_persistence import VideoParseLeaseLostError, VideoParsePersistenceService
from server.service.library import LibraryService
from server.service.upload import UploadService, UploadServiceConfig
from server.storage.artifact import LocalArtifactStore
from server.utils.time import datetime_to_unix_time_ms

TEST_DATABASE_URL = os.getenv("SEEKINO_TEST_DATABASE_URL")


def test_mysql_agent_attachments_migration_preserves_history_and_is_repeatable():
    url = os.getenv("SEEKINO_MIGRATION_TEST_DATABASE_URL")
    if not url:
        pytest.skip("SEEKINO_MIGRATION_TEST_DATABASE_URL is not configured")
    engine = create_database_engine(url, environment=DatabaseEnvironment.TEST)
    assert engine.url.database and engine.url.database.endswith("_migration_test"), (
        "requires dedicated disposable migration test database"
    )
    if inspect(engine).get_table_names():
        engine.dispose()
        pytest.skip("attachment migration test requires an empty dedicated database")
    migration = (Path(__file__).parents[2] /
                 "docs/migrations/20261016_agent_message_attachments.sql").read_text()
    routine, verification = migration.split("DELIMITER $$", 1)[1].split("DELIMITER ;", 1)
    statements = [statement.strip() for statement in routine.split("$$") if statement.strip()]
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql(
                "CREATE TABLE agent_message (id VARCHAR(36) PRIMARY KEY, content LONGTEXT, "
                "detail JSON, update_time BIGINT, update_date DATETIME(3) "
                "DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3))"
            )
            connection.execute(text(
                "INSERT INTO agent_message VALUES (:id, :content, :detail, 1234, '2020-01-01 00:00:00.123')"
            ), {"id": "kept", "content": "历史正文", "detail": '{"request":{"idempotencyKey":"kept"}}'})
            connection.commit()
            before = dict(connection.exec_driver_sql("SELECT * FROM agent_message").mappings().one())
            for _ in range(2):
                for statement in statements:
                    connection.exec_driver_sql(statement).close()
                column = connection.exec_driver_sql(verification.strip()).mappings().one()
                assert column["COLUMN_TYPE"] == "json" and column["IS_NULLABLE"] == "YES"
                after = dict(connection.exec_driver_sql("SELECT * FROM agent_message").mappings().one())
                assert after.pop("attachments") is None
                assert after == before
            connection.execute(text("UPDATE agent_message SET attachments = :refs"),
                               {"refs": '[{"mediaUri":"artifact://test/kept"}]'})
            connection.commit()
            with_attachment = dict(connection.exec_driver_sql("SELECT * FROM agent_message").mappings().one())
            for statement in statements:
                connection.exec_driver_sql(statement).close()
            assert dict(connection.exec_driver_sql("SELECT * FROM agent_message").mappings().one()) == with_attachment

            for definition in ("VARCHAR(255) NULL", "JSON NOT NULL"):
                connection.exec_driver_sql("ALTER TABLE agent_message DROP COLUMN attachments")
                connection.exec_driver_sql("DELETE FROM agent_message")
                connection.commit()
                connection.exec_driver_sql("ALTER TABLE agent_message ADD COLUMN attachments " + definition)
                with pytest.raises(DBAPIError, match="incompatible type or nullability"):
                    for statement in statements:
                        connection.exec_driver_sql(statement).close()
                connection.rollback()
                column = next(column for column in inspect(connection).get_columns("agent_message")
                              if column["name"] == "attachments")
                if definition.startswith("VARCHAR"):
                    assert isinstance(column["type"], String)
                    assert column["type"].length == 255
                else:
                    assert column["nullable"] is False
    finally:
        with engine.begin() as connection:
            connection.exec_driver_sql("DROP PROCEDURE IF EXISTS migrate_agent_message_attachments_20261016")
            connection.exec_driver_sql("DROP TABLE IF EXISTS agent_message")
        engine.dispose()


@pytest.mark.skipif(not TEST_DATABASE_URL, reason="SEEKINO_TEST_DATABASE_URL is not configured")
def test_mysql_conversation_attachment_publication_lock_serializes_deletion():
    engine = create_database_engine(TEST_DATABASE_URL or "", environment=DatabaseEnvironment.TEST)
    create_schema(engine)
    session_factory = create_session_factory(engine)
    repository = AgentRepository(session_factory)
    conversation_id = str(uuid4())
    requested, finished = Event(), Event()

    def watch_delete(_connection, _cursor, statement, _parameters, _context, _executemany):
        if (current_thread().name.startswith("mysql-attachment-delete")
                and "FROM agent_conversation" in statement and "FOR UPDATE" in statement):
            requested.set()

    def delete_conversation():
        result = repository.delete_conversation(conversation_id, "mysql-attachments", owner_user_id="owner")
        finished.set()
        return result

    event.listen(engine, "before_cursor_execute", watch_delete)
    try:
        repository.create_conversation(
            conversation_id=conversation_id, tenant_id="mysql-attachments", owner_user_id="owner",
            title="Attachments", idempotency_key=str(uuid4()),
        )
        assert repository.conversation_exists(conversation_id)
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="mysql-attachment-delete") as executor:
            with repository.lock_conversation(conversation_id, "mysql-attachments", owner_user_id="owner") as state:
                assert state.conversation_id == conversation_id
                future = executor.submit(delete_conversation)
                assert requested.wait(5), "delete did not request the publication lock"
                assert not finished.wait(0.1), "delete bypassed the publication lock"
            assert future.result(timeout=5).conversation_id == conversation_id
        assert not repository.conversation_exists(conversation_id)
    finally:
        event.remove(engine, "before_cursor_execute", watch_delete)
        with session_factory.begin() as session:
            session.execute(delete(AgentConversation).where(AgentConversation.id == conversation_id))
        engine.dispose()


def _upload_defaults():
    gateway = load_config()
    return ParseConfig.from_defaults(
        runtime=VideoParseRuntimeConfig(), gateway=gateway,
        upload=UploadServiceConfig(), images=ImageUploadConfig(), embedding_model=None,
        embedding_dimensions=gateway.resolve_embedding_binding().dimensions,
        embedding_fps=None,
    )


def _seed_legacy_job(factory, *, video_id, series_id):
    """Preserve historical V1 queue/cleanup coverage after uploads became V2-only."""
    job_id = str(uuid4())
    with factory.begin() as session:
        session.add(SeriesParseJob(
            id=job_id, video_id=video_id, series_id=series_id,
            pipeline_version="historical-v1", status="QUEUED", stage="QUEUED", progress=0,
        ))
    return job_id


@pytest.mark.skipif(not TEST_DATABASE_URL, reason="SEEKINO_TEST_DATABASE_URL is not configured")
def test_mysql_agent_message_detail_request_idempotency_and_pairing():
    engine = create_database_engine(
        TEST_DATABASE_URL or "", environment=DatabaseEnvironment.TEST,
    )
    create_schema(engine)
    session_factory = create_session_factory(engine)
    repository = AgentRepository(session_factory)
    conversation_id = str(uuid4())
    try:
        repository.create_conversation(
            conversation_id=conversation_id,
            tenant_id="mysql-agent-detail-request",
            title="Agent detail request test",
            idempotency_key=str(uuid4()),
        )
        request = dict(
            conversation_id=conversation_id,
            tenant_id="mysql-agent-detail-request",
            content="Find the scene",
            model_name="test-model",
            requested_top_k=5,
            requested_run_options={},
            idempotency_key="request-key",
            series_ids=[],
            requested_series_ids=[],
            user_message_id=str(uuid4()),
            assistant_message_id=str(uuid4()),
            now=now_unix_time_ms(),
        )
        started = repository.start_message(**request)
        replayed = repository.start_message(**{
            **request,
            "user_message_id": str(uuid4()),
            "assistant_message_id": str(uuid4()),
        })
        assert replayed.reused is True
        assert replayed.user_message.message_id == started.user_message.message_id
        assert replayed.assistant_message.message_id == started.assistant_message.message_id
        assert repository.get_message_pair(
            conversation_id, "mysql-agent-detail-request",
            idempotency_key="request-key",
        ).assistant_message.message_id == started.assistant_message.message_id
        assert repository.get_message_pair(
            conversation_id, "mysql-agent-detail-request",
            message_id=started.assistant_message.message_id,
        ).user_message.message_id == started.user_message.message_id

        with session_factory() as session:
            user = session.get(AgentMessage, started.user_message.message_id)
            assert user is not None
            assert user.detail == {"request": {"idempotencyKey": "request-key"}}

        with pytest.raises(IntegrityError):
            with session_factory.begin() as session:
                session.add(AgentMessage(
                    id=str(uuid4()),
                    conversation_id=conversation_id,
                    sequence_no=2,
                    role="USER",
                    status="COMPLETED",
                    version=1,
                    series_ids=[],
                    detail={"request": {"idempotencyKey": "request-key"}},
                ))
    finally:
        with session_factory.begin() as session:
            session.execute(delete(AgentMessage).where(
                AgentMessage.conversation_id == conversation_id,
            ))
            session.execute(delete(AgentConversation).where(
                AgentConversation.id == conversation_id,
            ))
        engine.dispose()


@pytest.mark.skipif(not TEST_DATABASE_URL, reason="SEEKINO_TEST_DATABASE_URL is not configured")
def test_mysql_manual_parse_retry_is_single_delivery_under_concurrent_requests(tmp_path):
    engine = create_database_engine(TEST_DATABASE_URL or "", environment=DatabaseEnvironment.TEST)
    create_schema(engine)
    session_factory = create_session_factory(engine)
    series = LibraryService(LibraryRepository(session_factory)).create_series(
        tenant_id="mysql-manual-retry",
        name=f"Manual Retry {uuid4()}",
    )
    store = LocalArtifactStore(tmp_path / "artifacts", environment="test")
    uploads = UploadService(
        UploadRepository(session_factory),
        store, parse_defaults=_upload_defaults(),
        config=UploadServiceConfig(
            preferred_segment_size_bytes=4,
            maximum_upload_size_bytes=1024,
            pipeline_version="mysql-manual-retry-v1",
            max_job_attempts=3,
        ),
        probe_duration=lambda _path: 10_000,
    )
    upload = uploads.create_session(
        tenant_id="mysql-manual-retry",
        series_id=series.series_id,
        episode_order=1,
        original_filename="episode.mp4",
        media_type="video/mp4",
        total_size_bytes=4,
        idempotency_key="manual-retry",
    )
    uploads.upload_segment(
        upload.upload_session_id,
        0,
        b"abcd",
        tenant_id="mysql-manual-retry",
        checksum_sha256=hashlib.sha256(b"abcd").hexdigest(),
        start_byte=0,
        end_byte_exclusive=4,
        total_size_bytes=4,
    )
    completed = uploads.complete(
        upload.upload_session_id,
        tenant_id="mysql-manual-retry",
    )
    assert completed.video_parse_task_id is not None
    assert completed.job_id is None
    with session_factory.begin() as session:
        task = session.get(VideoParseTask, completed.video_parse_task_id)
        original_config = task.parse_config
        task.status = "PARSING_FAILED"
        task.attempt_count = 3
        task.max_attempts = 3
        task.last_error = {"code": "MODEL_ERROR"}

    barrier = Barrier(2)

    def retry_once():
        barrier.wait()
        try:
            return uploads.retry_parse(
                upload.upload_session_id,
                tenant_id="mysql-manual-retry",
                expected_job_version=1, expected_lease_version=0,
            )
        except (UploadConflictError, UploadStateError) as exc:
            return exc

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(retry_once) for _ in range(2)]
            results = [future.result(timeout=10) for future in futures]
        retried = [
            result
            for result in results
            if not isinstance(result, (UploadConflictError, UploadStateError))
        ]
        assert len(retried) == 1
        assert retried[0].video_parse_task_id == completed.video_parse_task_id
        assert retried[0].version == 1
        assert sum(
            isinstance(result, (UploadConflictError, UploadStateError))
            for result in results
        ) == 1
        with session_factory() as session:
            task = session.get(VideoParseTask, completed.video_parse_task_id)
            assert (task.status, task.attempt_count, task.max_attempts) == (
                "PREPARING", 3, 3,
            )
            assert task.version == 1
            assert task.parse_config == original_config
            assert session.scalar(select(func.count(SeriesParseJob.id)).where(
                SeriesParseJob.series_id == series.series_id,
            )) == 0
    finally:
        with session_factory.begin() as session:
            session.execute(delete(Series).where(Series.id == series.series_id))
        engine.dispose()


@pytest.mark.skipif(not TEST_DATABASE_URL, reason="SEEKINO_TEST_DATABASE_URL is not configured")
def test_mysql_reparse_allocates_one_new_v2_version_under_concurrent_requests(
    tmp_path,
):
    engine = create_database_engine(
        TEST_DATABASE_URL or "",
        environment=DatabaseEnvironment.TEST,
    )
    create_schema(engine)
    session_factory = create_session_factory(engine)
    series = LibraryService(LibraryRepository(session_factory)).create_series(
        tenant_id="mysql-reparse",
        name=f"Concurrent Reparse {uuid4()}",
    )
    uploads = UploadService(
        UploadRepository(session_factory),
        LocalArtifactStore(tmp_path / "artifacts", environment="test"), parse_defaults=_upload_defaults(),
        config=UploadServiceConfig(
            preferred_segment_size_bytes=4,
            maximum_upload_size_bytes=1024,
            pipeline_version="mysql-reparse-v2",
        ),
        probe_duration=lambda _path: 120,
    )
    upload = uploads.create_session(
        tenant_id="mysql-reparse",
        series_id=series.series_id,
        episode_order=1,
        original_filename="episode.mp4",
        media_type="video/mp4",
        total_size_bytes=4,
        idempotency_key="reparse",
    )
    uploads.upload_segment(
        upload.upload_session_id,
        0,
        b"abcd",
        tenant_id="mysql-reparse",
        checksum_sha256=hashlib.sha256(b"abcd").hexdigest(),
        start_byte=0,
        end_byte_exclusive=4,
        total_size_bytes=4,
    )
    completed = uploads.complete(
        upload.upload_session_id,
        tenant_id="mysql-reparse",
    )
    assert completed.video_parse_task_id is not None
    with session_factory.begin() as session:
        initial = session.get(
            VideoParseTask,
            completed.video_parse_task_id,
        )
        original_config = initial.parse_config
        initial.status = "COMPLETE"
        assert completed.job_id is None

    barrier = Barrier(2)

    def reparse_once():
        barrier.wait()
        try:
            return uploads.reparse(
                upload.upload_session_id,
                tenant_id="mysql-reparse",
                expected_job_version=1,
            )
        except UploadConflictError as exc:
            return exc

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = [
                future.result(timeout=10)
                for future in [executor.submit(reparse_once) for _ in range(2)]
            ]
        created = [
            result
            for result in results
            if not isinstance(result, UploadConflictError)
        ]
        assert len(created) == 1
        assert sum(isinstance(result, UploadConflictError) for result in results) == 1
        assert created[0].video_parse_task_id != completed.video_parse_task_id
        assert created[0].version == 2
        assert created[0].parse_config == original_config
        with session_factory() as session:
            tasks = list(session.scalars(
                select(VideoParseTask)
                .where(VideoParseTask.video_id == completed.video_id)
                .order_by(VideoParseTask.version)
            ))
            assert [task.id for task in tasks] == [
                completed.video_parse_task_id,
                created[0].video_parse_task_id,
            ]
            assert [task.version for task in tasks] == [1, 2]
            assert tasks[0].parse_config == tasks[1].parse_config == original_config
            assert session.scalar(select(func.count(SeriesParseJob.id)).where(
                SeriesParseJob.series_id == series.series_id,
            )) == 0
    finally:
        with session_factory.begin() as session:
            session.execute(
                delete(Series).where(Series.id == series.series_id)
            )
        engine.dispose()


@pytest.fixture
def mysql_v2_series():
    if not TEST_DATABASE_URL:
        pytest.skip("SEEKINO_TEST_DATABASE_URL is not configured")
    engine = create_database_engine(
        TEST_DATABASE_URL, environment=DatabaseEnvironment.TEST,
    )
    assert engine.dialect.name == "mysql", "V2 lock evidence requires real MySQL"
    create_schema(engine)

    def bounded_lock_wait(dbapi_connection, _record, _proxy):
        with dbapi_connection.cursor() as cursor:
            cursor.execute("SET SESSION innodb_lock_wait_timeout = 3")

    event.listen(engine, "checkout", bounded_lock_wait)
    factory = create_session_factory(engine)
    now = unix_time_ms_to_datetime(now_unix_time_ms())
    series_id = str(uuid4())
    tenant_id = f"mysql-v2-{uuid4()}"
    config = default_video_parse_v2_config().model_dump(mode="json")
    config.update({
        "chunk_duration": 10,
        "interval_duration": 10,
        "compact_enabled": True,
        "prefix_context": {
            "series_compact_max_chars": 1000,
            "video_compact_max_chars": 1000,
            "max_tool_calls": 2,
        },
    })
    config["models"]["prefix_context"] = deepcopy(config["models"]["understanding"])
    episodes = []
    try:
        with factory.begin() as session:
            session.add(Series(
                id=series_id, tenant_id=tenant_id,
                name=f"V2 lock test {series_id}", status="READY",
                character_bible=[],
            ))
            session.flush()
            for order in range(1, 4):
                ids = {name: str(uuid4()) for name in (
                    "upload_id", "video_id", "task_id", "chunk_id",
                    "chunk_task_id", "interval_id",
                )}
                episodes.append(ids)
                session.add(Video(
                    id=ids["video_id"], series_id=series_id,
                    episode_order=order, upload_sequence=order,
                    filename=f"episode-{order}.mp4",
                    source_uri=f"artifact://test/{order}.mp4",
                    hash=str(order) * 64, size_bytes=4, duration_ms=10,
                    upload_status="COMPLETED", parse_status="COMPLETE",
                    upload_progress=100, parse_progress=100,
                ))
                session.flush()
                session.add(VideoParseTask(
                    id=ids["task_id"], video_id=ids["video_id"],
                    pipeline_version="video-parse-v2", status="COMPLETE",
                    parse_config=deepcopy(config), chunk_count=1, version=1,
                    next_execute_time=now, max_attempts=3, lease_version=0,
                    is_test=True, is_deleted=False,
                    detail={"_worker_runtime_v2": {
                        "accepted": True, "execution_mode": "v2",
                        "unrelated": {"preserved": True},
                        "prefix": {
                            "next_chunk_sequence_no": 1,
                            "lease_owner": None, "lease_version": order,
                            "lease_expire_time_ms": None, "execution_task_id": None,
                            "retry_count": 0, "next_execute_time_ms": 0,
                            "video_prefix_compact": f"video-{order}",
                            "series_prefix_compact": f"series-{order}",
                        },
                    }},
                    create_time=datetime_to_unix_time_ms(now), update_time=datetime_to_unix_time_ms(now),
                ))
                session.flush()
                session.add(VideoChunk(
                    id=ids["chunk_id"], video_id=ids["video_id"],
                    sequence_no=0, start_ms=0, end_ms=10,
                    window_start_ms=0, window_end_ms=10,
                    chunk_media_ref="artifact://test/chunk.mp4",
                    analysis_window_media_ref="artifact://test/window.mp4",
                    transcript=[], keyframe_uris=[],
                ))
                session.flush()
                session.add(ChunkParseTask(
                    id=ids["chunk_task_id"], video_task_id=ids["task_id"],
                    chunk_id=ids["chunk_id"], phase=3, status="COMPLETE",
                    indexed_content_version=1,
                    next_execute_time=now, lease_version=1,
                ))
                session.flush()
                session.add(ChunkParseContent(
                    id=str(uuid4()), chunk_task_id=ids["chunk_task_id"],
                    content={"persons": ["retained"], "timeline": []}, version=1,
                    detail={"scope": [0], "compact": "retained child"},
                ))
                session.add(IntervalParseTask(
                    id=ids["interval_id"], chunk_task_id=ids["chunk_task_id"],
                    video_task_id=ids["task_id"],
                    chunk_task_ids=[ids["chunk_task_id"]],
                    start_sequence_no=0, chunk_count=1, ready_mask=1,
                    status="COMPLETE", next_execute_time=now, lease_version=1,
                ))
                session.flush()
                session.add(IntervalParseContent(
                    interval_task_id=ids["interval_id"], content={"retained": True},
                ))
                session.add(UploadSession(
                    id=ids["upload_id"], tenant_id=tenant_id,
                    series_id=series_id, episode_order=order, upload_sequence=order,
                    filename=f"episode-{order}.mp4", media_type="video/mp4",
                    size_bytes=4, segment_size_bytes=4, segment_count=1,
                    status="COMPLETED", idempotency_key=str(uuid4()),
                    expire_time=now + timedelta(hours=1), finish_time=now,
                    video_id=ids["video_id"], initial_task_id=ids["task_id"],
                    parse_config=deepcopy(config),
                ))
        yield engine, factory, series_id, tenant_id, now, episodes
    finally:
        event.remove(engine, "checkout", bounded_lock_wait)
        with factory.begin() as session:
            session.execute(delete(Series).where(Series.id == series_id))
        engine.dispose()


@pytest.mark.parametrize("takeover", [False, True], ids=["reparse", "pending-takeover"])
@pytest.mark.parametrize("active", [True, False], ids=["active-prefix", "expired-prefix"])
def test_mysql_reparse_prefix_suffix_is_atomic_across_connections(
    mysql_v2_series, takeover, active, tmp_path,
):
    engine, factory, _series_id, tenant_id, now, episodes = mysql_v2_series
    uploads = UploadService(
        UploadRepository(factory),
        LocalArtifactStore(tmp_path / "artifacts", environment="test"),
        parse_defaults=_upload_defaults(),
        clock=lambda: now,
    )
    first, second, third = episodes
    expires = now + (timedelta(minutes=5) if active else -timedelta(seconds=1))
    with factory.begin() as session:
        parent = session.get(VideoParseTask, first["task_id"])
        if takeover:
            parent.status = "PENDING"
            detail = deepcopy(parent.detail)
            detail["_worker_runtime_v2"]["accepted"] = False
            parent.detail = detail
        later = session.get(VideoParseTask, second["task_id"])
        detail = deepcopy(later.detail)
        detail["_worker_runtime_v2"]["prefix"].update({
            "next_chunk_sequence_no": 0, "lease_owner": "old-prefix",
            "lease_expire_time_ms": int(expires.timestamp() * 1000),
            "execution_task_id": second["chunk_task_id"],
        })
        later.detail = detail
        session.flush()
        before = {
            item["task_id"]: deepcopy(session.get(VideoParseTask, item["task_id"]).detail)
            for item in episodes
        }

    locked, requested, observed, release = Event(), Event(), Event(), Event()
    connections = {}

    def watch_reparse(connection, _cursor, statement, parameters, _context, _many):
        sql = " ".join(statement.lower().replace("`", "").split())
        values = parameters.values() if isinstance(parameters, dict) else (parameters or ())
        if (
            current_thread().name.startswith("mysql-reparse-contender")
            and "from video_parse_task" in sql and "for update" in sql
            and second["task_id"] in values
        ):
            connections["reparse"] = id(connection.connection.driver_connection)
            requested.set()

    def hold_later_parent():
        with factory.begin() as session:
            session.scalar(select(VideoParseTask).where(
                VideoParseTask.id == second["task_id"],
            ).with_for_update())
            connections["holder"] = id(session.connection().connection.driver_connection)
            locked.set()
            assert requested.wait(5), "reparse never requested the locked suffix parent"
            visible = list(session.scalars(select(VideoParseTask).where(
                VideoParseTask.video_id == first["video_id"],
            )))
            assert [(task.id, task.version) for task in visible] == [(first["task_id"], 1)]
            assert visible[0].detail == before[first["task_id"]]
            observed.set()
            assert release.wait(5), "suffix lock was not released by the test"

    event.listen(engine, "before_cursor_execute", watch_reparse)
    try:
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="mysql-prefix-holder") as holders, \
                ThreadPoolExecutor(max_workers=1, thread_name_prefix="mysql-reparse-contender") as contenders:
            holder = holders.submit(hold_later_parent)
            try:
                assert locked.wait(5), "holder did not acquire the later parent lock"
                future = contenders.submit(
                    uploads.reparse, first["upload_id"],
                    tenant_id=tenant_id, expected_job_version=1,
                )
                assert observed.wait(5), "uncommitted entry changes were not observed"
            finally:
                release.set()
            holder.result(timeout=5)
            assert connections["holder"] != connections["reparse"]
            if active:
                with pytest.raises(UploadStateError, match="active lease"):
                    future.result(timeout=5)
            else:
                result = future.result(timeout=5)
    finally:
        release.set()
        event.remove(engine, "before_cursor_execute", watch_reparse)

    with factory() as session:
        all_tasks = list(session.scalars(select(VideoParseTask).where(
            VideoParseTask.video_id.in_([item["video_id"] for item in episodes]),
        )))
        assert len(all_tasks) == (3 if active or takeover else 4)
        if active:
            assert {task.id: task.detail for task in all_tasks} == before
            assert session.get(VideoParseTask, first["task_id"]).status == (
                "PENDING" if takeover else "COMPLETE"
            )
        else:
            assert result.version == (1 if takeover else 2)
            invalidated_ids = [result.video_parse_task_id, second["task_id"], third["task_id"]]
            for task_id in invalidated_ids:
                runtime = session.get(VideoParseTask, task_id).detail["_worker_runtime_v2"]
                prefix = runtime["prefix"]
                assert prefix["next_chunk_sequence_no"] == 0
                assert prefix["series_prefix_compact"] == prefix["video_prefix_compact"] == ""
                assert prefix["lease_owner"] is prefix["execution_task_id"] is None
                assert prefix["lease_version"] > before.get(task_id, {}).get(
                    "_worker_runtime_v2", {},
                ).get("prefix", {}).get("lease_version", 0)
            if takeover:
                assert result.video_parse_task_id == first["task_id"]
                runtime = session.get(VideoParseTask, first["task_id"]).detail["_worker_runtime_v2"]
                assert runtime["accepted"] is True
                assert runtime["unrelated"] == {"preserved": True}
            else:
                assert result.video_parse_task_id != first["task_id"]
                assert session.get(VideoParseTask, first["task_id"]).detail == before[first["task_id"]]
        for item in episodes:
            content = session.scalar(select(ChunkParseContent).where(
                ChunkParseContent.chunk_task_id == item["chunk_task_id"],
            ))
            assert content.content == {"persons": ["retained"], "timeline": []}
            assert content.detail == {"scope": [0], "compact": "retained child"}

    if not active:
        stale = PrefixParseLease(
            video_parse_task_id=second["task_id"], chunk_parse_task_id=second["chunk_task_id"],
            video_chunk_id=second["chunk_id"], sequence_no=0, worker_id="old-prefix",
            lease_version=2, lease_expire_time=expires, retry_count=0,
            content_version=1, content={"persons": ["retained"], "timeline": []},
            content_detail={"scope": [0], "compact": "retained child"},
            series_prefix_compact="series-2", video_prefix_compact="video-2",
        )
        repository = VideoParsePersistenceService(factory)
        assert not repository.heartbeat_prefix(stale, lease_duration=timedelta(minutes=1), now=now)
        assert not repository.retry_prefix(stale, error="late", now=now)
        with pytest.raises(VideoParseLeaseLostError):
            repository.commit_prefix(
                stale, content_updates={"persons": ["late"]},
                video_prefix_compact="late", now=now,
            )


@pytest.mark.parametrize("kind", ["chunk", "refine", "index"])
def test_mysql_retry_fences_worker_claim_after_its_advisory_read(mysql_v2_series, kind):
    engine, factory, _series_id, tenant_id, now, episodes = mysql_v2_series
    target = episodes[0]
    queue = MySqlVideoParseQueue(factory, video_concurrency=3)
    table = "video_parse_task"
    with factory.begin() as session:
        parent = session.get(VideoParseTask, target["task_id"])
        parent.status = "INDEXING" if kind == "index" else "PARSING"
        parent.capacity_acquired = True
        chunk = session.get(ChunkParseTask, target["chunk_task_id"])
        interval = session.get(IntervalParseTask, target["interval_id"])
        if kind == "chunk":
            chunk.phase, chunk.status = 1, "QUEUED"
            chunk.indexed_content_version = None
            session.execute(delete(ChunkParseContent).where(ChunkParseContent.chunk_task_id == chunk.id))
            interval.status = "WAITING"
            interval.ready_mask = 0
        elif kind == "refine":
            chunk.phase, chunk.status = 2, "WAITING"
            chunk.indexed_content_version = None
            interval.status = "WAITING"
        else:
            chunk.phase, chunk.status = 3, "QUEUED"
            chunk.indexed_content_version = None
        if kind != "index":
            session.execute(delete(IntervalParseContent).where(
                IntervalParseContent.interval_task_id == target["interval_id"],
            ))

    candidates_read, resume_claim = Event(), Event()
    retry_locked, release_retry, claim_attempted = Event(), Event(), Event()
    connections = {}

    def after_query(connection, _cursor, statement, _parameters, _context, _many):
        sql = " ".join(statement.lower().replace("`", "").split())
        role = current_thread().name
        if f"from {table}" not in sql:
            return
        if role.startswith("mysql-claim-worker") and "for update" not in sql:
            if not candidates_read.is_set():
                connections["claim"] = id(connection.connection.driver_connection)
                candidates_read.set()
                assert resume_claim.wait(5), "claim advisory read was not released"
        elif role.startswith("mysql-manual-retry") and "for update" in sql:
            if not retry_locked.is_set():
                connections["retry"] = id(connection.connection.driver_connection)
                retry_locked.set()
                assert release_retry.wait(5), "retry parent lock were not released"

    def before_query(_connection, _cursor, statement, _parameters, _context, _many):
        sql = " ".join(statement.lower().replace("`", "").split())
        if (
            current_thread().name.startswith("mysql-claim-worker")
            and f"from {table}" in sql and "for update" in sql
        ):
            claim_attempted.set()

    claim = {
        "chunk": queue.claim_chunk,
        "refine": queue.claim_interval_refine,
        "index": queue.claim_chunk_index,
    }[kind]
    event.listen(engine, "after_cursor_execute", after_query)
    event.listen(engine, "before_cursor_execute", before_query)
    try:
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="mysql-claim-worker") as workers, \
                ThreadPoolExecutor(max_workers=1, thread_name_prefix="mysql-manual-retry") as retries:
            worker = workers.submit(
                claim, "claim-worker", lease_duration=timedelta(minutes=5),
                video_parse_task_id=target["task_id"], now=now,
            )
            try:
                assert candidates_read.wait(5), "worker never read an eligible candidate"
                # Fail the parent after the advisory read. Keep the child runnable
                # so rejection below requires the actual held parent lock.
                with factory.begin() as session:
                    parent = session.get(VideoParseTask, target["task_id"])
                    parent.status = "INDEXING_FAILED" if kind == "index" else "PARSING_FAILED"
                retry = retries.submit(
                    UploadRepository(factory).retry_parse, target["upload_id"],
                    tenant_id=tenant_id, expected_job_version=1, expected_lease_version=0, now=now,
                )
                assert retry_locked.wait(5), "retry never acquired the candidate parent lock"
                resume_claim.set()
                assert worker.result(timeout=5) is None
                assert claim_attempted.is_set(), "worker never attempted the locked candidate"
                assert connections["claim"] != connections["retry"]
            finally:
                resume_claim.set()
                release_retry.set()
            restored = retry.result(timeout=5)
        assert restored.video_parse_task_id == target["task_id"]
        assert restored.status == ("INDEXING" if kind == "index" else "PARSING")
        assert queue.claim_video("restored-parent", lease_duration=timedelta(minutes=5), now=now) is not None
        claimed = claim(
            "after-retry", lease_duration=timedelta(minutes=5),
            video_parse_task_id=target["task_id"], now=now,
        )
        assert claimed is not None and claimed.video_parse_task_id == target["task_id"]
        assert claimed.lease_version == 2
    finally:
        resume_claim.set()
        release_retry.set()
        event.remove(engine, "after_cursor_execute", after_query)
        event.remove(engine, "before_cursor_execute", before_query)


@pytest.mark.parametrize("kind", ["chunk", "refine"])
def test_mysql_manual_retry_and_worker_commit_have_no_lock_cycle(mysql_v2_series, kind):
    engine, factory, _series_id, tenant_id, now, episodes = mysql_v2_series
    target = episodes[0]
    expires = now + timedelta(minutes=5)
    with factory.begin() as session:
        parent = session.get(VideoParseTask, target["task_id"])
        parent.status, parent.capacity_acquired = "PARSING", True
        detail = deepcopy(parent.detail)
        detail["_worker_runtime_v2"]["failure_status"] = "PARSING_FAILED"
        parent.detail = detail
        chunk = session.get(ChunkParseTask, target["chunk_task_id"])
        interval = session.get(IntervalParseTask, target["interval_id"])
        chunk.phase, chunk.status = (1, "RUNNING") if kind == "chunk" else (2, "WAITING")
        chunk.indexed_content_version = None
        active = chunk if kind == "chunk" else interval
        active.status = "RUNNING" if kind == "chunk" else "REFINING"
        active.lease_owner, active.lease_expire_time = "commit-worker", expires
        active.start_time = now
        phase = {"chunk": "parse", "refine": "refine", "index": "index"}[kind]
        deadline = now + timedelta(seconds=parent.parse_config["execution"][phase + "_execution_timeout_seconds"])
        if kind == "chunk":
            interval.status, interval.ready_mask = "WAITING", 0
            session.execute(delete(ChunkParseContent).where(ChunkParseContent.chunk_task_id == chunk.id))
        session.execute(delete(IntervalParseContent).where(
            IntervalParseContent.interval_task_id == target["interval_id"],
        ))
    repository = VideoParsePersistenceService(factory)
    common = dict(video_parse_task_id=target["task_id"], worker_id="commit-worker", lease_version=1,
        lease_expire_time=expires, execution_deadline=deadline, retry_count=0, max_retry_count=2,
        retry_initial_delay_seconds=5.0, retry_max_delay_seconds=300.0)
    lease = (ChunkParseLease(**common, chunk_parse_task_id=target["chunk_task_id"],
        video_chunk_id=target["chunk_id"]) if kind == "chunk" else IntervalParseLease(**common,
        interval_parse_task_id=target["interval_id"], chunk_parse_task_ids=(target["chunk_task_id"],),
        start_sequence_no=0, chunk_count=1, phase="REFINING"))
    inputs = _refinement_inputs(repository, lease, now=now) if kind == "refine" else None
    worker_locked, resume_worker = Event(), Event()
    connections = {}

    def before_query(connection, _cursor, statement, _parameters, _context, _many):
        sql = " ".join(statement.lower().replace("`", "").split())
        if (current_thread().name.startswith("mysql-commit-retry")
            and "from video_parse_task" in sql and "for update" in sql):
            connections["retry"] = id(connection.connection.driver_connection)
            resume_worker.set()

    def after_query(connection, _cursor, statement, _parameters, _context, _many):
        sql = " ".join(statement.lower().replace("`", "").split())
        if (current_thread().name.startswith("mysql-commit-worker")
            and "from video_parse_task" in sql and "for update" in sql
            and not worker_locked.is_set()):
            connections["worker"] = id(connection.connection.driver_connection)
            worker_locked.set()
            assert resume_worker.wait(5), "retry did not request the same parent lock"

    def commit():
        if kind == "chunk":
            return repository.commit_chunk_result(lease,
                content={"persons": ["worker"], "timeline": []}, now=now)
        return repository.commit_interval_refinement(lease, **inputs, interval_content={"refined": True},
            member_updates={target["chunk_task_id"]: {"persons": ["worker"]}}, now=now)

    event.listen(engine, "before_cursor_execute", before_query)
    event.listen(engine, "after_cursor_execute", after_query)
    try:
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="mysql-commit-worker") as workers, \
                ThreadPoolExecutor(max_workers=1, thread_name_prefix="mysql-commit-retry") as retries:
            worker = workers.submit(commit)
            try:
                assert worker_locked.wait(5)
                retry = retries.submit(UploadRepository(factory).retry_parse, target["upload_id"],
                    tenant_id=tenant_id, expected_job_version=1, expected_lease_version=0, now=now)
                worker.result(timeout=8)
                # Failure is still draining; publishing the terminal parent is a
                # separate transaction. An early retry cannot reset any budget.
                with pytest.raises(UploadStateError):
                    retry.result(timeout=8)
            finally:
                resume_worker.set()
        assert connections["worker"] != connections["retry"]
        MySqlVideoParseQueue(factory, video_concurrency=3).reap_expired(now=now)
        restored = UploadRepository(factory).retry_parse(target["upload_id"], tenant_id=tenant_id,
            expected_job_version=1, expected_lease_version=0, now=now)
        assert restored.video_parse_task_id == target["task_id"]
        assert restored.status == ("PARSING" if kind == "chunk" else "INDEXING")
        with factory() as session:
            content = session.scalar(select(ChunkParseContent).where(
                ChunkParseContent.chunk_task_id == target["chunk_task_id"],
            ))
            assert content.content["persons"] == ["worker"]
            assert content.version == (1 if kind == "chunk" else 2)
            chunk = session.get(ChunkParseTask, target["chunk_task_id"])
            assert (chunk.phase, chunk.status) == ((2, "WAITING") if kind == "chunk" else (3, "QUEUED"))
    finally:
        resume_worker.set()
        event.remove(engine, "before_cursor_execute", before_query)
        event.remove(engine, "after_cursor_execute", after_query)


@pytest.mark.parametrize("kind", ["chunk", "refine", "index"])
@pytest.mark.parametrize("expired", [False, True], ids=["live-lease", "expired-owner"])
def test_mysql_failure_drain_waits_for_the_child_commit_transaction(
    mysql_v2_series, kind, expired,
):
    engine, factory, _series_id, _tenant_id, now, episodes = mysql_v2_series
    target = episodes[0]
    expires = now + timedelta(minutes=5)
    checked_at = now + (timedelta(minutes=6) if expired else timedelta(seconds=2))
    failure = "INDEXING_FAILED" if kind == "index" else "PARSING_FAILED"
    running = "INDEXING" if kind == "index" else "PARSING"
    with factory.begin() as session:
        parent = session.get(VideoParseTask, target["task_id"])
        index_generation = parent.parse_config["indexing"]["index_generation"]
        parent.status = running
        parent.capacity_acquired = True
        detail = deepcopy(parent.detail)
        detail["_worker_runtime_v2"]["failure_status"] = failure
        parent.detail = detail
        chunk = session.get(ChunkParseTask, target["chunk_task_id"])
        interval = session.get(IntervalParseTask, target["interval_id"])
        active = interval if kind == "refine" else chunk
        active.status = {
            "chunk": "RUNNING", "refine": "REFINING", "index": "RUNNING",
        }[kind]
        active.lease_owner = "draining-worker"
        active.lease_expire_time = expires
        active.start_time = now
        phase = {"chunk": "parse", "refine": "refine", "index": "index"}[kind]
        deadline = now + timedelta(seconds=parent.parse_config["execution"][phase + "_execution_timeout_seconds"])
        chunk.phase = 1 if kind == "chunk" else (2 if kind == "refine" else 3)
        if kind == "refine":
            chunk.status = "WAITING"
        chunk.indexed_content_version = None
        if kind == "chunk":
            interval.status = "WAITING"
            interval.ready_mask = 0
        if kind != "index":
            session.execute(delete(IntervalParseContent).where(
                IntervalParseContent.interval_task_id == target["interval_id"],
            ))

    queue, repository = MySqlVideoParseQueue(factory, video_concurrency=3), VideoParsePersistenceService(factory)
    update_pending, release_commit = Event(), Event()
    table = "interval_parse_task" if kind == "refine" else "chunk_parse_task"
    connections = {}

    def after_query(connection, _cursor, statement, _parameters, _context, _many):
        sql = " ".join(statement.lower().replace("`", "").split())
        role = current_thread().name
        if role.startswith("mysql-drain-commit") and sql.startswith(f"update {table} "):
            # The real result commit has cleared the owner inside its SQL
            # transaction, but neither the result nor that release is visible yet.
            if not update_pending.is_set():
                connections["worker"] = id(connection.connection.driver_connection)
                update_pending.set()
                assert release_commit.wait(5), "child commit was not released"
        elif (
            role.startswith("mysql-drain-publisher")
            and "from video_parse_task" in sql and "for update" in sql
        ):
            connections["publisher"] = id(connection.connection.driver_connection)

    def commit():
        committed_at = now + timedelta(seconds=1)
        if kind == "chunk":
            lease = ChunkParseLease(
                chunk_parse_task_id=target["chunk_task_id"],
                video_parse_task_id=target["task_id"], video_chunk_id=target["chunk_id"],
                worker_id="draining-worker", lease_version=1,
                lease_expire_time=expires, execution_deadline=deadline, retry_count=0, max_retry_count=2,
                retry_initial_delay_seconds=5.0, retry_max_delay_seconds=300.0,
            )
            return repository.commit_chunk_result(
                lease, content={"persons": ["drained"], "timeline": []}, now=committed_at,
            )
        if kind == "index":
            lease = ChunkParseLease(
                chunk_parse_task_id=target["chunk_task_id"], video_parse_task_id=target["task_id"],
                video_chunk_id=target["chunk_id"], phase=3, worker_id="draining-worker", lease_version=1,
                lease_expire_time=expires, execution_deadline=deadline, retry_count=0, max_retry_count=2,
                retry_initial_delay_seconds=5.0, retry_max_delay_seconds=300.0,
            )
            repository.save_index_embeddings(lease, target["chunk_task_id"], 1,
                {"semantic": {"vector": [1.0]}}, receipt={
                    "parse_task_id": target["task_id"], "video_chunk_id": target["chunk_id"],
                    "content_version": 1, "index_generation": index_generation,
                }, now=committed_at)
            return repository.complete_chunk_indexing(lease, 1, index_generation, now=committed_at)
        lease = IntervalParseLease(
            interval_parse_task_id=target["interval_id"],
            video_parse_task_id=target["task_id"],
            chunk_parse_task_ids=(target["chunk_task_id"],),
            start_sequence_no=0, chunk_count=1,
            phase="REFINING",
            worker_id="draining-worker", lease_version=1,
            lease_expire_time=expires, execution_deadline=deadline, retry_count=0, max_retry_count=2,
                retry_initial_delay_seconds=5.0, retry_max_delay_seconds=300.0,
        )
        return repository.commit_interval_refinement(
            lease, **_refinement_inputs(repository, lease, now=committed_at), interval_content={"refined": "drained"},
            member_updates={target["chunk_task_id"]: {"persons": ["drained"]}},
            now=committed_at,
        )

    event.listen(engine, "after_cursor_execute", after_query)
    try:
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="mysql-drain-commit") as workers, \
                ThreadPoolExecutor(max_workers=1, thread_name_prefix="mysql-drain-publisher") as publishers:
            worker = workers.submit(commit)
            try:
                assert update_pending.wait(5), "result commit never updated its child row"
                publishers.submit(
                    queue.reap_expired, now=checked_at,
                ).result(timeout=5)
                assert connections["worker"] != connections["publisher"]
                assert not worker.done(), "result commit was not held during the drain scan"
                with factory() as session:
                    parent = session.get(VideoParseTask, target["task_id"])
                    assert parent.status == running and parent.finish_time is None
                    assert parent.detail["_worker_runtime_v2"]["failure_status"] == failure
                    active = session.get(
                        IntervalParseTask if kind == "refine" else ChunkParseTask,
                        target["interval_id"] if kind == "refine" else target["chunk_task_id"],
                    )
                    assert active.lease_owner == "draining-worker"
                    if expired:
                        assert active.lease_expire_time < checked_at
            finally:
                release_commit.set()
            worker.result(timeout=5)
            publishers.submit(
                queue.reap_expired, now=checked_at,
            ).result(timeout=5)

        with factory() as session:
            parent = session.get(VideoParseTask, target["task_id"])
            assert parent.status == failure and parent.finish_time is not None
            assert parent.finish_time <= checked_at and parent.capacity_acquired is False
            chunk = session.get(ChunkParseTask, target["chunk_task_id"])
            interval = session.get(IntervalParseTask, target["interval_id"])
            assert (chunk.phase, chunk.status) == {
                "chunk": (2, "WAITING"), "refine": (3, "QUEUED"), "index": (3, "COMPLETE"),
            }[kind]
            assert chunk.lease_owner is None
            assert interval.lease_owner is None
            content = session.scalar(select(ChunkParseContent).where(
                ChunkParseContent.chunk_task_id == target["chunk_task_id"],
            ))
            if kind != "index":
                assert content.content["persons"] == ["drained"]
            if kind == "refine":
                assert content.version == 2
                assert session.get(IntervalParseContent, target["interval_id"]).content == {
                    "refined": "drained",
                }
            elif kind == "index":
                assert interval.status == "COMPLETE"
                assert chunk.indexed_content_version == content.version == 1
                assert parent.target_index_generation == parent.parse_config["indexing"]["index_generation"] == index_generation
                assert content.detail["index_committed_lease_versions"] == [1]
    finally:
        release_commit.set()
        event.remove(engine, "after_cursor_execute", after_query)


@pytest.mark.skipif(not TEST_DATABASE_URL, reason="SEEKINO_TEST_DATABASE_URL is not configured")
def test_mysql_delete_fences_in_flight_upload_and_preserves_order(tmp_path, monkeypatch):
    engine = create_database_engine(TEST_DATABASE_URL or "", environment=DatabaseEnvironment.TEST)
    create_schema(engine)
    session_factory = create_session_factory(engine)
    series = LibraryService(LibraryRepository(session_factory)).create_series(
        tenant_id="mysql-delete", name=f"Upload Delete {uuid4()}",
    )
    store = LocalArtifactStore(tmp_path / "artifacts", environment="test")
    uploads = UploadService(UploadRepository(session_factory), store, parse_defaults=_upload_defaults(),
        config=UploadServiceConfig(preferred_segment_size_bytes=4, maximum_upload_size_bytes=1024))
    entered = Event()
    release = Event()
    put = store.put_upload_segment

    def delayed_put(*args):
        entered.set()
        assert release.wait(10)
        return put(*args)

    try:
        upload = uploads.create_session(
            tenant_id="mysql-delete", series_id=series.series_id,
            episode_order=1,
            original_filename="episode.mp4", media_type="video/mp4",
            total_size_bytes=4, idempotency_key="first",
        )
        monkeypatch.setattr(store, "put_upload_segment", delayed_put)
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(uploads.upload_segment,
                upload.upload_session_id, 0, b"abcd", tenant_id="mysql-delete",
                checksum_sha256=hashlib.sha256(b"abcd").hexdigest(),
                start_byte=0, end_byte_exclusive=4, total_size_bytes=4)
            try:
                assert entered.wait(10)
                assert uploads.delete_session(upload.upload_session_id, tenant_id="mysql-delete").status == "EXPIRED"
            finally:
                release.set()
            with pytest.raises(UploadStateError, match="expired"):
                future.result(timeout=10)
        assert not (store.base_directory / "staging" / "uploads" / upload.upload_session_id).exists()
        assert uploads.list_sessions(tenant_id="mysql-delete") == []
        next_upload = uploads.create_session(
            tenant_id="mysql-delete", series_id=series.series_id,
            episode_order=2,
            original_filename="next.mp4", media_type="video/mp4",
            total_size_bytes=4, idempotency_key="next",
        )
        assert next_upload.episode_order == next_upload.upload_sequence == 2
    finally:
        release.set()
        with session_factory.begin() as session:
            session.execute(delete(Series).where(Series.id == series.series_id))
        engine.dispose()


@pytest.mark.skipif(not TEST_DATABASE_URL, reason="SEEKINO_TEST_DATABASE_URL is not configured")
def test_mysql_delete_completed_upload_removes_video_job_and_usage(tmp_path):
    engine = create_database_engine(TEST_DATABASE_URL or "", environment=DatabaseEnvironment.TEST)
    create_schema(engine)
    session_factory = create_session_factory(engine)
    series = LibraryService(LibraryRepository(session_factory)).create_series(
        tenant_id="mysql-completed-delete",
        name=f"Completed Upload Delete {uuid4()}",
    )
    store = LocalArtifactStore(tmp_path / "artifacts", environment="test")
    uploads = UploadService(
        UploadRepository(session_factory),
        store, parse_defaults=_upload_defaults(),
        config=UploadServiceConfig(
            preferred_segment_size_bytes=4,
            maximum_upload_size_bytes=1024,
            pipeline_version="mysql-delete-v1",
        ),
        probe_duration=lambda _path: 10_000,
    )
    upload = uploads.create_session(
        tenant_id="mysql-completed-delete",
        series_id=series.series_id,
        episode_order=1,
        original_filename="episode.mp4",
        media_type="video/mp4",
        total_size_bytes=4,
        idempotency_key="completed-delete",
    )
    uploads.upload_segment(
        upload.upload_session_id,
        0,
        b"abcd",
        tenant_id="mysql-completed-delete",
        checksum_sha256=hashlib.sha256(b"abcd").hexdigest(),
        start_byte=0,
        end_byte_exclusive=4,
        total_size_bytes=4,
    )
    completed = uploads.complete(
        upload.upload_session_id,
        tenant_id="mysql-completed-delete",
    )
    now = now_unix_time_ms()
    call_id = str(uuid4())
    legacy_job_id = _seed_legacy_job(
        session_factory, video_id=completed.video_id, series_id=series.series_id,
    )
    with session_factory.begin() as session:
        session.add(ParseUsageAttempt(
            job_id=legacy_job_id,
            attempt_count=1,
            lease_version=1,
            start_time=unix_time_ms_to_datetime(now),
            recording_complete=True,
        ))
        session.add(ParseModelCall(
            id=call_id,
            job_id=legacy_job_id,
            attempt_count=1,
            lease_version=1,
            tenant_id="mysql-completed-delete",
            series_id=series.series_id,
            video_id=completed.video_id,
            provider="test",
            model="test-model",
            category="parse",
            operation="parse",
            start_time=unix_time_ms_to_datetime(now),
            status="failed",
        ))

    try:
        deleted = uploads.delete_session(
            upload.upload_session_id,
            tenant_id="mysql-completed-delete",
        )
        repeated = uploads.delete_session(
            upload.upload_session_id,
            tenant_id="mysql-completed-delete",
        )
        assert deleted.status == UploadSessionStatus.EXPIRED.value
        assert repeated.version == deleted.version
        with session_factory() as session:
            tombstone = session.get(UploadSession, upload.upload_session_id)
            assert tombstone.video_id is None and tombstone.job_id is None
            assert tombstone.last_error_code is None
            assert session.get(Video, completed.video_id) is None
            assert session.get(SeriesParseJob, legacy_job_id) is None
            assert session.get(ParseUsageAttempt, (legacy_job_id, 1, 1)) is None
            assert session.get(ParseModelCall, call_id) is None
    finally:
        with session_factory.begin() as session:
            session.execute(delete(Series).where(Series.id == series.series_id))
        engine.dispose()


@pytest.mark.skipif(not TEST_DATABASE_URL, reason="SEEKINO_TEST_DATABASE_URL is not configured")
def test_mysql_completes_distinct_uploads_in_one_series_concurrently(
    tmp_path,
):
    engine = create_database_engine(
        TEST_DATABASE_URL or "", environment=DatabaseEnvironment.TEST,
    )
    assert engine.dialect.name == "mysql", "Upload lock evidence requires real MySQL"
    create_schema(engine)
    session_factory = create_session_factory(engine)
    tenant_id = f"mysql-complete-{uuid4()}"
    series = LibraryService(LibraryRepository(session_factory)).create_series(
        tenant_id=tenant_id, name=f"Concurrent Upload Completion {uuid4()}",
    )
    uploads = UploadService(
        UploadRepository(session_factory),
        LocalArtifactStore(tmp_path / "artifacts", environment="test"), parse_defaults=_upload_defaults(),
        config=UploadServiceConfig(
            preferred_segment_size_bytes=4,
            maximum_upload_size_bytes=1024,
            pipeline_version="mysql-concurrent-complete",
        ),
        probe_duration=lambda _path: 10_000,
    )
    barrier = Barrier(3)
    all_inserted = Event()
    connections = set()

    def bounded_lock_wait(dbapi_connection, _record, _proxy):
        with dbapi_connection.cursor() as cursor:
            cursor.execute("SET SESSION innodb_lock_wait_timeout = 5")

    def after_insert(connection, _cursor, statement, parameters, _context, _many):
        sql = " ".join(statement.lower().replace("`", "").split())
        values = parameters.values() if isinstance(parameters, dict) else (parameters or ())
        if (
            current_thread().name.startswith("mysql-upload-complete")
            and sql.startswith("insert into video ")
            and series.series_id in values
        ):
            connections.add(id(connection.connection.driver_connection))
            if len(connections) == 3:
                all_inserted.set()
            # Let concurrent inserts hold their foreign-key shared locks before
            # updating Series. Serial completion must not need this rendezvous.
            all_inserted.wait(1)

    def complete(upload):
        barrier.wait(timeout=5)
        return uploads.complete(upload.upload_session_id, tenant_id=tenant_id)

    event.listen(engine, "checkout", bounded_lock_wait)
    event.listen(engine, "after_cursor_execute", after_insert)
    try:
        pending = []
        for order, source in enumerate((b"aaaa", b"bbbb", b"cccc"), start=1):
            upload = uploads.create_session(
                tenant_id=tenant_id, series_id=series.series_id,
                episode_order=order, original_filename=f"episode-{order}.mp4",
                media_type="video/mp4", total_size_bytes=len(source),
                idempotency_key=f"complete-{order}",
            )
            uploads.upload_segment(
                upload.upload_session_id, 0, source, tenant_id=tenant_id,
                checksum_sha256=hashlib.sha256(source).hexdigest(),
                start_byte=0, end_byte_exclusive=len(source),
                total_size_bytes=len(source),
            )
            pending.append(upload)

        with ThreadPoolExecutor(
            max_workers=3, thread_name_prefix="mysql-upload-complete",
        ) as executor:
            futures = [executor.submit(complete, upload) for upload in pending]
            completed = [future.result(timeout=15) for future in futures]

        assert len(connections) == 3
        assert all(item.session.status == "COMPLETED" for item in completed)
        assert len({item.video_id for item in completed}) == 3
        assert len({item.video_parse_task_id for item in completed}) == 3
        replayed = [
            uploads.complete(upload.upload_session_id, tenant_id=tenant_id)
            for upload in pending
        ]
        assert [
            (item.video_id, item.job_id, item.video_parse_task_id)
            for item in replayed
        ] == [
            (item.video_id, item.job_id, item.video_parse_task_id)
            for item in completed
        ]
        with session_factory() as session:
            videos = list(session.scalars(select(Video).where(
                Video.series_id == series.series_id,
            )))
            assert len(videos) == 3
            assert {video.episode_order for video in videos} == {1, 2, 3}
            tasks = list(session.scalars(select(VideoParseTask).where(
                VideoParseTask.video_id.in_([video.id for video in videos]),
            )))
            assert len(tasks) == 3
            assert {task.video_id for task in tasks} == {video.id for video in videos}
            assert {task.id for task in tasks} == {
                item.video_parse_task_id for item in completed
            }
            jobs = list(session.scalars(select(SeriesParseJob).where(
                SeriesParseJob.series_id == series.series_id,
            )))
            assert jobs == []
            assert {job.id for job in jobs} == {
                item.job_id for item in completed if item.job_id is not None
            }
    finally:
        all_inserted.set()
        event.remove(engine, "after_cursor_execute", after_insert)
        event.remove(engine, "checkout", bounded_lock_wait)
        with session_factory.begin() as session:
            session.execute(delete(Series).where(Series.id == series.series_id))
        engine.dispose()


@pytest.mark.skipif(not TEST_DATABASE_URL, reason="SEEKINO_TEST_DATABASE_URL is not configured")
def test_mysql_enforces_episode_order_under_concurrent_requests(tmp_path) -> None:
    engine = create_database_engine(TEST_DATABASE_URL or "", environment=DatabaseEnvironment.TEST)
    create_schema(engine)
    session_factory = create_session_factory(engine)
    series = LibraryService(LibraryRepository(session_factory)).create_series(
        tenant_id="mysql-order", name=f"Concurrent Upload Order {uuid4()}",
    )
    uploads = UploadService(
        UploadRepository(session_factory),
        LocalArtifactStore(tmp_path / "artifacts", environment="test"), parse_defaults=_upload_defaults(),
    )
    barrier = Barrier(4)

    def create(number: int):
        barrier.wait()
        try:
            return uploads.create_session(
                tenant_id="mysql-order", series_id=series.series_id,
                episode_order=999,
                original_filename=f"episode-999-{number}.mp4", media_type="video/mp4",
                total_size_bytes=100, idempotency_key=f"upload-{number}",
            )
        except UploadConflictError as exc:
            return exc

    try:
        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(create, range(4)))
        uploads_created = [
            item for item in results if not isinstance(item, UploadConflictError)
        ]
        assert len(uploads_created) == 1
        assert sum(isinstance(item, UploadConflictError) for item in results) == 3
        created = uploads_created[0]
        assert created.episode_order == created.upload_sequence == 999
        winner = next(
            number for number, item in enumerate(results) if item is created
        )
        replay = uploads.create_session(
            tenant_id="mysql-order", series_id=series.series_id,
            episode_order=999,
            original_filename=f"episode-999-{winner}.mp4", media_type="video/mp4",
            total_size_bytes=100, idempotency_key=f"upload-{winner}",
        )
        assert replay.upload_session_id == created.upload_session_id
        assert replay.episode_order == created.episode_order
    finally:
        with engine.begin() as connection:
            connection.execute(delete(Series).where(Series.id == series.series_id))
        engine.dispose()
