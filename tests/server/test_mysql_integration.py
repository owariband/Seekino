from __future__ import annotations

import os
import hashlib
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from threading import Barrier, Event, current_thread
from uuid import uuid4

import pytest

from tests.server.test_video_parse_repository import _refinement_inputs
from sqlalchemy import delete, event, func, select
from sqlalchemy.exc import IntegrityError

from server.database import (
    DatabaseEnvironment,
    create_database_engine,
    create_schema,
    create_session_factory,
)
from server.model.video import (
    ContextSnapshot,
    Series,
    Video,
    VideoChunkInfo,
    VideoChunkEmbedding,
    VideoChunk,
)
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
from server.model.upload import (
    UPLOAD_DELETION_PENDING_ERROR_CODE,
    UploadSession,
    UploadSessionStatus,
)
from server.mq.mysql_queue import MySqlParseJobQueue
from server.mq.video_parse_queue import MySqlVideoParseQueue
from server.utils.time import (
    now_unix_time_ms,
    timedelta_to_ms,
    unix_time_ms_to_datetime,
)
from server.mq.contracts import ParseJobLeaseLostError
from server.repository.job import ParseJobRepository
from server.repository.agent import AgentRepository
from server.repository.index_sync import IndexSyncRepository
from server.model.dto.jobDTO import (
    ChunkParseLease, IntervalParseLease, PrefixParseLease, VideoChunkCheckpointState,
)
from server.config.parse_profile import default_video_parse_v2_config
from server.repository.library import LibraryRepository
from server.repository.upload import UploadConflictError, UploadRepository, UploadStateError
from server.repository.video import VideoRepository
from server.repository.video_parse import VideoParseLeaseLostError, VideoParseRepository
from server.service.library import LibraryService
from server.model.dto.parseDTO import PreparedEmbedding
from server.service.qdrant_mutation_lock import (
    QdrantMutationLock,
    QdrantMutationLockBusyError,
)
from server.service.upload import UploadService, UploadServiceConfig
from server.storage.artifact import LocalArtifactStore

TEST_DATABASE_URL = os.getenv("ANIVAG_TEST_DATABASE_URL")


@pytest.mark.skipif(not TEST_DATABASE_URL, reason="ANIVAG_TEST_DATABASE_URL is not configured")
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
                    message_id=str(uuid4()),
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
                AgentConversation.conversation_id == conversation_id,
            ))
        engine.dispose()


@pytest.mark.skipif(not TEST_DATABASE_URL, reason="ANIVAG_TEST_DATABASE_URL is not configured")
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
        store,
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
    with session_factory.begin() as session:
        job = session.get(SeriesParseJob, completed.job_id)
        legacy_job_before = (job.status, job.attempt_count, job.version)
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
                expected_job_version=1,
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
                "PREPARING", 3, 4,
            )
            assert task.version == 1
            assert task.parse_config == original_config
            job = session.get(SeriesParseJob, completed.job_id)
            assert (job.status, job.attempt_count, job.version) == legacy_job_before
    finally:
        with session_factory.begin() as session:
            session.execute(delete(Series).where(Series.series_id == series.series_id))
        engine.dispose()


@pytest.mark.skipif(not TEST_DATABASE_URL, reason="ANIVAG_TEST_DATABASE_URL is not configured")
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
        LocalArtifactStore(tmp_path / "artifacts", environment="test"),
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
        job = session.get(SeriesParseJob, completed.job_id)
        legacy_job_before = (job.status, job.version, job.attempt_count)

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
            job = session.get(SeriesParseJob, completed.job_id)
            assert (job.status, job.version, job.attempt_count) == legacy_job_before
    finally:
        with session_factory.begin() as session:
            session.execute(
                delete(Series).where(Series.series_id == series.series_id)
            )
        engine.dispose()


@pytest.fixture
def mysql_v2_series():
    if not TEST_DATABASE_URL:
        pytest.skip("ANIVAG_TEST_DATABASE_URL is not configured")
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
                series_id=series_id, tenant_id=tenant_id,
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
                    video_id=ids["video_id"], series_id=series_id,
                    episode_order=order, upload_sequence=order,
                    original_filename=f"episode-{order}.mp4",
                    original_source_uri=f"artifact://test/{order}.mp4",
                    content_hash=str(order) * 64, total_bytes=4, duration_seconds=10,
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
                    create_time=now, update_time=now,
                ))
                session.flush()
                session.add(VideoChunk(
                    video_chunk_id=ids["chunk_id"], video_id=ids["video_id"],
                    sequence_no=0, start_seconds=0, end_seconds=10,
                    analysis_window_start_seconds=0, analysis_window_end_seconds=10,
                    chunk_media_ref="artifact://test/chunk.mp4",
                    analysis_window_media_ref="artifact://test/window.mp4",
                    transcript=[], keyframe_uris=[],
                ))
                session.flush()
                session.add(ChunkParseTask(
                    id=ids["chunk_task_id"], video_parse_task_id=ids["task_id"],
                    video_chunk_id=ids["chunk_id"], status="COMPLETE",
                    next_execute_time=now, lease_version=1,
                ))
                session.flush()
                session.add(ChunkParseContent(
                    id=str(uuid4()), chunk_parse_task_id=ids["chunk_task_id"],
                    content={"persons": ["retained"], "timeline": []}, version=1,
                    detail={"scope": [0], "compact": "retained child"},
                ))
                session.add(IntervalParseTask(
                    id=ids["interval_id"], chunk_parse_task_id=ids["chunk_task_id"],
                    video_parse_task_id=ids["task_id"],
                    chunk_parse_task_ids=[ids["chunk_task_id"]],
                    start_sequence_no=0, chunk_count=1, ready_mask=1,
                    status="INDEXING", next_execute_time=now, lease_version=1,
                ))
                session.flush()
                session.add(IntervalParseContent(
                    interval_parse_task_id=ids["interval_id"], content={"retained": True},
                ))
                session.add(UploadSession(
                    upload_session_id=ids["upload_id"], tenant_id=tenant_id,
                    series_id=series_id, episode_order=order, upload_sequence=order,
                    original_filename=f"episode-{order}.mp4", media_type="video/mp4",
                    total_size_bytes=4, preferred_segment_size_bytes=4, total_segments=1,
                    status="COMPLETED", idempotency_key=str(uuid4()),
                    expires_at=now + timedelta(hours=1), completed_at=now,
                    video_id=ids["video_id"], initial_video_parse_task_id=ids["task_id"],
                    video_parse_config=deepcopy(config),
                ))
        yield engine, factory, series_id, tenant_id, now, episodes
    finally:
        event.remove(engine, "checkout", bounded_lock_wait)
        with factory.begin() as session:
            session.execute(delete(Series).where(Series.series_id == series_id))
        engine.dispose()


@pytest.mark.parametrize("takeover", [False, True], ids=["reparse", "pending-takeover"])
@pytest.mark.parametrize("active", [True, False], ids=["active-prefix", "expired-prefix"])
def test_mysql_reparse_prefix_suffix_is_atomic_across_connections(
    mysql_v2_series, takeover, active,
):
    engine, factory, _series_id, tenant_id, now, episodes = mysql_v2_series
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
                    UploadRepository(factory).reparse, first["upload_id"],
                    tenant_id=tenant_id, expected_job_version=1,
                    execution_mode="v2", now=now,
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
                ChunkParseContent.chunk_parse_task_id == item["chunk_task_id"],
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
        repository = VideoParseRepository(factory)
        assert not repository.heartbeat_prefix(stale, lease_duration=timedelta(minutes=1), now=now)
        assert not repository.retry_prefix(stale, error="late", retry_at=now, now=now)
        with pytest.raises(VideoParseLeaseLostError):
            repository.commit_prefix(
                stale, content_updates={"persons": ["late"]},
                video_prefix_compact="late", now=now,
            )


@pytest.mark.parametrize("kind", ["chunk", "refine", "index"])
def test_mysql_retry_fences_worker_claim_after_its_advisory_read(mysql_v2_series, kind):
    engine, factory, _series_id, tenant_id, now, episodes = mysql_v2_series
    target = episodes[0]
    queue = MySqlVideoParseQueue(factory)
    table = "chunk_parse_task" if kind == "chunk" else "interval_parse_task"
    with factory.begin() as session:
        parent = session.get(VideoParseTask, target["task_id"])
        parent.status = "INDEXING" if kind == "index" else "PARSING"
        chunk = session.get(ChunkParseTask, target["chunk_task_id"])
        interval = session.get(IntervalParseTask, target["interval_id"])
        if kind == "chunk":
            chunk.status = "QUEUED"
            chunk.detail = {"dispatch_requested": True}
            interval.status = "WAITING"
            interval.ready_mask = 0
        elif kind == "refine":
            interval.status = "WAITING"
        if kind != "index":
            session.execute(delete(IntervalParseContent).where(
                IntervalParseContent.interval_parse_task_id == target["interval_id"],
            ))

    candidates_read, resume_claim = Event(), Event()
    retry_locked, release_retry, claim_attempted = Event(), Event(), Event()
    connections = {}

    def after_query(connection, _cursor, statement, _parameters, _context, _many):
        sql = " ".join(statement.lower().replace("`", "").split())
        role = current_thread().name
        if f"from {table}" not in sql:
            return
        if role.startswith("mysql-claim-worker") and "limit" in sql and "for update" not in sql:
            if not candidates_read.is_set():
                connections["claim"] = id(connection.connection.driver_connection)
                candidates_read.set()
                assert resume_claim.wait(5), "claim advisory read was not released"
        elif role.startswith("mysql-manual-retry") and "for update" in sql:
            if not retry_locked.is_set():
                connections["retry"] = id(connection.connection.driver_connection)
                retry_locked.set()
                assert release_retry.wait(5), "retry child locks were not released"

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
        "index": queue.claim_interval_index,
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
                # so rejection below requires the actual held child lock.
                with factory.begin() as session:
                    parent = session.get(VideoParseTask, target["task_id"])
                    parent.status = "INDEXING_FAILED" if kind == "index" else "PARSING_FAILED"
                retry = retries.submit(
                    UploadRepository(factory).retry_parse, target["upload_id"],
                    tenant_id=tenant_id, expected_job_version=1, now=now,
                )
                assert retry_locked.wait(5), "retry never acquired the candidate child lock"
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
        session.get(VideoParseTask, target["task_id"]).status = "PARSING_FAILED"
        chunk = session.get(ChunkParseTask, target["chunk_task_id"])
        interval = session.get(IntervalParseTask, target["interval_id"])
        active = chunk if kind == "chunk" else interval
        active.status = "UNDERSTANDING" if kind == "chunk" else "REFINING"
        active.lease_owner = "commit-worker"
        active.lease_expire_time = expires
        if kind == "chunk":
            interval.status = "WAITING"
            interval.ready_mask = 0
        session.execute(delete(IntervalParseContent).where(
            IntervalParseContent.interval_parse_task_id == target["interval_id"],
        ))

    repository = VideoParseRepository(factory)
    worker_locked, resume_worker = Event(), Event()
    retry_chunk_locked = Event()
    connections = {}

    def before_query(_connection, _cursor, statement, _parameters, _context, _many):
        sql = " ".join(statement.lower().replace("`", "").split())
        if (
            current_thread().name.startswith("mysql-commit-retry")
            and "from interval_parse_task" in sql and "for update" in sql
            and not retry_chunk_locked.is_set()
        ):
            # An Interval-first retry will wait for the Worker without owning
            # Chunk locks, so let the Worker finish its actual commit.
            resume_worker.set()

    def after_query(connection, _cursor, statement, _parameters, _context, _many):
        sql = " ".join(statement.lower().replace("`", "").split())
        if "for update" not in sql:
            return
        role = current_thread().name
        if role.startswith("mysql-commit-worker") and "from interval_parse_task" in sql:
            if not worker_locked.is_set():
                connections["worker"] = id(connection.connection.driver_connection)
                worker_locked.set()
                assert resume_worker.wait(5), "retry did not reach a competing child lock"
        elif role.startswith("mysql-commit-retry") and "from chunk_parse_task" in sql:
            connections["retry"] = id(connection.connection.driver_connection)
            retry_chunk_locked.set()
            # A Chunk-first retry now owns the opposing lock. Continue both
            # production methods and require safety, without hiding deadlocks.
            resume_worker.set()

    def commit():
        if kind == "chunk":
            lease = ChunkParseLease(
                chunk_parse_task_id=target["chunk_task_id"], video_parse_task_id=target["task_id"],
                video_chunk_id=target["chunk_id"], worker_id="commit-worker", lease_version=1,
                lease_expire_time=expires, retry_count=0, max_retry_count=2,
            )
            return repository.commit_chunk_result(
                lease, content={"persons": ["worker"], "timeline": []}, now=now,
            )
        lease = IntervalParseLease(
            interval_parse_task_id=target["interval_id"], video_parse_task_id=target["task_id"],
            chunk_parse_task_ids=(target["chunk_task_id"],), start_sequence_no=0, chunk_count=1,
            phase="REFINING", worker_id="commit-worker", lease_version=1,
            lease_expire_time=expires, retry_count=0, max_retry_count=2,
        )
        return repository.commit_interval_refinement(
            lease, **_refinement_inputs(repository, lease, now=now), interval_content={"refined": True},
            member_updates={target["chunk_task_id"]: {"persons": ["worker"]}}, now=now,
        )

    event.listen(engine, "before_cursor_execute", before_query)
    event.listen(engine, "after_cursor_execute", after_query)
    try:
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="mysql-commit-worker") as workers, \
                ThreadPoolExecutor(max_workers=1, thread_name_prefix="mysql-commit-retry") as retries:
            worker = workers.submit(commit)
            try:
                assert worker_locked.wait(5), "worker did not acquire its Interval locks"
                retry = retries.submit(
                    UploadRepository(factory).retry_parse, target["upload_id"],
                    tenant_id=tenant_id, expected_job_version=1, now=now,
                )
                worker.result(timeout=8)
                restored = retry.result(timeout=8)
            finally:
                resume_worker.set()
        assert connections["worker"] != connections["retry"]
        assert restored.video_parse_task_id == target["task_id"]
        assert restored.status == ("PARSING" if kind == "chunk" else "INDEXING")
        with factory() as session:
            content = session.scalar(select(ChunkParseContent).where(
                ChunkParseContent.chunk_parse_task_id == target["chunk_task_id"],
            ))
            assert content.content["persons"] == ["worker"]
            assert content.version == (1 if kind == "chunk" else 2)
            assert session.get(ChunkParseTask, target["chunk_task_id"]).status == "COMPLETE"
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
        parent.status = running
        detail = deepcopy(parent.detail)
        detail["_worker_runtime_v2"]["failure_status"] = failure
        parent.detail = detail
        chunk = session.get(ChunkParseTask, target["chunk_task_id"])
        interval = session.get(IntervalParseTask, target["interval_id"])
        active = chunk if kind == "chunk" else interval
        active.status = {
            "chunk": "UNDERSTANDING", "refine": "REFINING", "index": "INDEXING",
        }[kind]
        active.lease_owner = "draining-worker"
        active.lease_expire_time = expires
        if kind == "chunk":
            interval.status = "WAITING"
            interval.ready_mask = 0
        if kind != "index":
            session.execute(delete(IntervalParseContent).where(
                IntervalParseContent.interval_parse_task_id == target["interval_id"],
            ))

    queue, repository = MySqlVideoParseQueue(factory), VideoParseRepository(factory)
    update_pending, release_commit = Event(), Event()
    table = "chunk_parse_task" if kind == "chunk" else "interval_parse_task"
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
                lease_expire_time=expires, retry_count=0, max_retry_count=2,
            )
            return repository.commit_chunk_result(
                lease, content={"persons": ["drained"], "timeline": []}, now=committed_at,
            )
        lease = IntervalParseLease(
            interval_parse_task_id=target["interval_id"],
            video_parse_task_id=target["task_id"],
            chunk_parse_task_ids=(target["chunk_task_id"],),
            start_sequence_no=0, chunk_count=1,
            phase="INDEXING" if kind == "index" else "REFINING",
            worker_id="draining-worker", lease_version=1,
            lease_expire_time=expires, retry_count=0, max_retry_count=2,
        )
        if kind == "index":
            return repository.complete_interval_indexing(
                lease, {target["chunk_task_id"]: 1}, now=committed_at,
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
                    queue.reap_expired, retry_delay=timedelta(0), now=checked_at,
                ).result(timeout=5)
                assert connections["worker"] != connections["publisher"]
                assert not worker.done(), "result commit was not held during the drain scan"
                with factory() as session:
                    parent = session.get(VideoParseTask, target["task_id"])
                    assert parent.status == running and parent.finish_time is None
                    assert parent.detail["_worker_runtime_v2"]["failure_status"] == failure
                    active = session.get(
                        ChunkParseTask if kind == "chunk" else IntervalParseTask,
                        target["chunk_task_id"] if kind == "chunk" else target["interval_id"],
                    )
                    assert active.lease_owner == "draining-worker"
                    if expired:
                        assert active.lease_expire_time < checked_at
            finally:
                release_commit.set()
            worker.result(timeout=5)
            publishers.submit(
                queue.reap_expired, retry_delay=timedelta(0), now=checked_at,
            ).result(timeout=5)

        with factory() as session:
            parent = session.get(VideoParseTask, target["task_id"])
            assert parent.status == failure and parent.finish_time == checked_at
            chunk = session.get(ChunkParseTask, target["chunk_task_id"])
            interval = session.get(IntervalParseTask, target["interval_id"])
            assert chunk.status == "COMPLETE" and chunk.lease_owner is None
            assert interval.lease_owner is None
            content = session.scalar(select(ChunkParseContent).where(
                ChunkParseContent.chunk_parse_task_id == target["chunk_task_id"],
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
                detail = session.get(IntervalParseContent, target["interval_id"]).detail
                assert detail["indexed_versions"] == {target["chunk_task_id"]: 1}
    finally:
        release_commit.set()
        event.remove(engine, "after_cursor_execute", after_query)


@pytest.mark.skipif(not TEST_DATABASE_URL, reason="ANIVAG_TEST_DATABASE_URL is not configured")
def test_mysql_qdrant_mutation_lock_is_shared_across_connections():
    engine = create_database_engine(
        TEST_DATABASE_URL or "",
        environment=DatabaseEnvironment.TEST,
    )
    holder = QdrantMutationLock(
        engine,
        environment=DatabaseEnvironment.TEST,
        collection_name="video_chunks_lock_test",
    )
    contender = QdrantMutationLock(
        engine,
        environment=DatabaseEnvironment.TEST,
        collection_name="video_chunks_lock_test",
        acquire_timeout_seconds=0,
    )

    try:
        with holder.hold(), ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(_try_qdrant_mutation_lock, contender)
            with pytest.raises(QdrantMutationLockBusyError, match="retry"):
                future.result(timeout=2)
        with contender.hold():
            pass
    finally:
        engine.dispose()


def _try_qdrant_mutation_lock(lock: QdrantMutationLock) -> None:
    with lock.hold():
        pass


@pytest.mark.skipif(not TEST_DATABASE_URL, reason="ANIVAG_TEST_DATABASE_URL is not configured")
def test_mysql_index_sync_hides_video_while_upload_deletion_is_pending(
    prepared_import,
) -> None:
    engine = create_database_engine(
        TEST_DATABASE_URL or "",
        environment=DatabaseEnvironment.TEST,
    )
    create_schema(engine)
    session_factory = create_session_factory(engine)
    prepared = replace(
        prepared_import,
        chunks=tuple(
            replace(
                item,
                text_embedding=PreparedEmbedding(
                    vector=[0.1, 0.2],
                    metadata={"model": "text-v1"},
                ),
                vision_embedding=PreparedEmbedding(
                    vector=[0.3, 0.4],
                    metadata={"model": "vision-v1"},
                ),
            )
            for item in prepared_import.chunks
        ),
    )
    with engine.begin() as connection:
        connection.execute(delete(Series).where(
            Series.series_id == prepared.series_id
        ))

    try:
        VideoRepository(session_factory).import_video_parse(prepared)
        now = unix_time_ms_to_datetime(now_unix_time_ms())
        with session_factory.begin() as session:
            session.add(UploadSession(
                upload_session_id=str(uuid4()),
                tenant_id=prepared.tenant_id,
                series_id=prepared.series_id,
                episode_order=prepared.episode_order,
                upload_sequence=prepared.upload_sequence,
                original_filename=prepared.original_filename,
                media_type="video/mp4",
                total_size_bytes=prepared.total_bytes,
                preferred_segment_size_bytes=prepared.total_bytes,
                total_segments=1,
                content_sha256=prepared.content_hash,
                version=1,
                status=UploadSessionStatus.EXPIRED.value,
                idempotency_key="mysql-pending-delete-crash",
                expires_at=now,
                completed_at=now,
                video_id=prepared.video_id,
                last_error_code=UPLOAD_DELETION_PENDING_ERROR_CODE,
            ))

        index_repository = IndexSyncRepository(session_factory)
        assert index_repository.materialize_all() == 0
        assert index_repository.list_all() == []
    finally:
        with engine.begin() as connection:
            connection.execute(delete(Series).where(
                Series.series_id == prepared.series_id
            ))
        engine.dispose()


@pytest.mark.skipif(not TEST_DATABASE_URL, reason="ANIVAG_TEST_DATABASE_URL is not configured")
def test_mysql_delete_fences_in_flight_upload_and_preserves_order(tmp_path, monkeypatch):
    engine = create_database_engine(TEST_DATABASE_URL or "", environment=DatabaseEnvironment.TEST)
    create_schema(engine)
    session_factory = create_session_factory(engine)
    series = LibraryService(LibraryRepository(session_factory)).create_series(
        tenant_id="mysql-delete", name=f"Upload Delete {uuid4()}",
    )
    store = LocalArtifactStore(tmp_path / "artifacts", environment="test")
    uploads = UploadService(UploadRepository(session_factory), store,
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
            session.execute(delete(Series).where(Series.series_id == series.series_id))
        engine.dispose()


@pytest.mark.skipif(not TEST_DATABASE_URL, reason="ANIVAG_TEST_DATABASE_URL is not configured")
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
        store,
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
    with session_factory.begin() as session:
        session.add(ParseUsageAttempt(
            job_id=completed.job_id,
            attempt_count=1,
            lease_version=1,
            started_at=unix_time_ms_to_datetime(now),
            recording_complete=True,
        ))
        session.add(ParseModelCall(
            call_id=call_id,
            job_id=completed.job_id,
            attempt_count=1,
            lease_version=1,
            tenant_id="mysql-completed-delete",
            series_id=series.series_id,
            video_id=completed.video_id,
            provider="test",
            model="test-model",
            category="parse",
            operation="parse",
            started_at=unix_time_ms_to_datetime(now),
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
            assert session.get(SeriesParseJob, completed.job_id) is None
            assert session.get(ParseUsageAttempt, (completed.job_id, 1, 1)) is None
            assert session.get(ParseModelCall, call_id) is None
    finally:
        with session_factory.begin() as session:
            session.execute(delete(Series).where(Series.series_id == series.series_id))
        engine.dispose()


@pytest.mark.skipif(not TEST_DATABASE_URL, reason="ANIVAG_TEST_DATABASE_URL is not configured")
def test_mysql_enforces_episode_order_under_concurrent_requests(tmp_path) -> None:
    engine = create_database_engine(TEST_DATABASE_URL or "", environment=DatabaseEnvironment.TEST)
    create_schema(engine)
    session_factory = create_session_factory(engine)
    series = LibraryService(LibraryRepository(session_factory)).create_series(
        tenant_id="mysql-order", name=f"Concurrent Upload Order {uuid4()}",
    )
    uploads = UploadService(
        UploadRepository(session_factory),
        LocalArtifactStore(tmp_path / "artifacts", environment="test"),
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
            connection.execute(delete(Series).where(Series.series_id == series.series_id))
        engine.dispose()


@pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="ANIVAG_TEST_DATABASE_URL is not configured",
)
def test_mysql_import_hydrate_and_late_failure_rollback(prepared_import) -> None:
    engine = create_database_engine(
        TEST_DATABASE_URL or "",
        environment=DatabaseEnvironment.TEST,
    )
    create_schema(engine)
    repository = VideoRepository(create_session_factory(engine))
    failed_series_id = str(uuid4())
    failed_video_id = str(uuid4())
    failed_chunks = tuple(
        replace(
            item,
            video_chunk_id=str(uuid4()),
            snapshot_id=str(uuid4()),
        )
        for item in prepared_import.chunks
    )
    failed_import = replace(
        prepared_import,
        series_id=failed_series_id,
        series_name=f"Failed Test Series {failed_series_id}",
        video_id=failed_video_id,
        chunks=failed_chunks,
    )
    prepared_with_vectors = replace(
        prepared_import,
        chunks=tuple(
            replace(
                item,
                text_embedding=PreparedEmbedding(
                    vector=[0.1, 0.2],
                    metadata={"model": "text-v1"},
                ),
                vision_embedding=PreparedEmbedding(
                    vector=[0.3, 0.4],
                    metadata={"model": "vision-v1"},
                ),
            )
            for item in prepared_import.chunks
        ),
    )

    with engine.begin() as connection:
        connection.execute(
            delete(Series).where(
                Series.series_id.in_(
                    [prepared_import.series_id, failed_series_id]
                )
            )
        )

    try:
        start_barrier = Barrier(2)

        def import_concurrently():
            start_barrier.wait()
            return repository.import_video_parse(prepared_with_vectors)

        with ThreadPoolExecutor(max_workers=2) as executor:
            first_future = executor.submit(import_concurrently)
            second_future = executor.submit(import_concurrently)
            first = first_future.result()
            second = second_future.result()
        assert first == second
        assert first.vectors_imported is True

        chunk_ids = first.video_chunk_ids
        with engine.connect() as connection:
            counts = (
                connection.execute(
                    select(func.count()).select_from(Series).where(
                        Series.series_id == prepared_import.series_id
                    )
                ).scalar_one(),
                connection.execute(
                    select(func.count()).select_from(Video).where(
                        Video.video_id == prepared_import.video_id
                    )
                ).scalar_one(),
                connection.execute(
                    select(func.count()).select_from(VideoChunk).where(
                        VideoChunk.video_id == prepared_import.video_id
                    )
                ).scalar_one(),
                connection.execute(
                    select(func.count())
                    .select_from(VideoChunkInfo)
                    .where(
                        VideoChunkInfo.video_chunk_id.in_(chunk_ids)
                    )
                ).scalar_one(),
                connection.execute(
                    select(func.count()).select_from(ContextSnapshot).where(
                        ContextSnapshot.video_id == prepared_import.video_id
                    )
                ).scalar_one(),
                connection.execute(
                    select(func.count())
                    .select_from(VideoChunkEmbedding)
                    .where(
                        VideoChunkEmbedding.video_chunk_id.in_(
                            chunk_ids
                        )
                    )
                ).scalar_one(),
            )
            compact, text_input, text_vector, vision_vector = connection.execute(
                select(
                    VideoChunkInfo.video_compact,
                    VideoChunkEmbedding.text_input,
                    VideoChunkEmbedding.text_vector,
                    VideoChunkEmbedding.vision_vector,
                ).join(
                    VideoChunkEmbedding,
                    VideoChunkEmbedding.video_chunk_id
                    == VideoChunkInfo.video_chunk_id,
                ).where(
                    VideoChunkInfo.video_chunk_id == chunk_ids[0]
                )
            ).one()
        assert counts == (1, 1, 2, 2, 2, 2)
        assert "动作" in compact
        assert text_input == (
            prepared_with_vectors.chunks[0].retrieval_document.text
        )
        assert text_vector == [0.1, 0.2]
        assert vision_vector == [0.3, 0.4]

        select_count = 0

        def count_selects(_connection, _cursor, statement, *_args) -> None:
            nonlocal select_count
            if statement.lstrip().upper().startswith("SELECT"):
                select_count += 1

        event.listen(engine, "before_cursor_execute", count_selects)
        try:
            evidence = repository.hydrate_video_chunks(
                list(reversed(first.video_chunk_ids))
            )
        finally:
            event.remove(engine, "before_cursor_execute", count_selects)
        assert select_count == 1
        assert [item.sequence_no for item in evidence] == [1, 0]
        assert evidence[0].retrieval_text == (
            prepared_with_vectors.chunks[1].retrieval_document.text
        )

        def fail_second_snapshot(_mapper, _connection, target) -> None:
            if target.after_chunk_sequence_no == 1:
                raise RuntimeError("injected MySQL persistence failure")

        event.listen(ContextSnapshot, "before_insert", fail_second_snapshot)
        try:
            with pytest.raises(
                RuntimeError,
                match="injected MySQL persistence failure",
            ):
                repository.import_video_parse(failed_import)
        finally:
            event.remove(
                ContextSnapshot,
                "before_insert",
                fail_second_snapshot,
            )

        with engine.connect() as connection:
            failed_counts = (
                connection.execute(
                    select(func.count()).select_from(Series).where(
                        Series.series_id == failed_series_id
                    )
                ).scalar_one(),
                connection.execute(
                    select(func.count()).select_from(Video).where(
                        Video.video_id == failed_video_id
                    )
                ).scalar_one(),
                connection.execute(
                    select(func.count()).select_from(VideoChunk).where(
                        VideoChunk.video_id == failed_video_id
                    )
                ).scalar_one(),
                connection.execute(
                    select(func.count())
                    .select_from(VideoChunkInfo)
                    .where(
                        VideoChunkInfo.video_chunk_id.in_(
                            [item.video_chunk_id for item in failed_chunks]
                        )
                    )
                ).scalar_one(),
                connection.execute(
                    select(func.count()).select_from(ContextSnapshot).where(
                        ContextSnapshot.video_id == failed_video_id
                    )
                ).scalar_one(),
                connection.execute(
                    select(func.count())
                    .select_from(VideoChunkEmbedding)
                    .where(
                        VideoChunkEmbedding.video_chunk_id.in_(
                            [item.video_chunk_id for item in failed_chunks]
                        )
                    )
                ).scalar_one(),
            )
        assert failed_counts == (0, 0, 0, 0, 0, 0)
    finally:
        with engine.begin() as connection:
            connection.execute(
                delete(Series).where(
                    Series.series_id.in_(
                        [prepared_import.series_id, failed_series_id]
                    )
                )
            )


@pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="ANIVAG_TEST_DATABASE_URL is not configured",
)
def test_mysql_chunked_upload_claim_and_lease_fencing(tmp_path) -> None:
    engine = create_database_engine(
        TEST_DATABASE_URL or "",
        environment=DatabaseEnvironment.TEST,
    )
    create_schema(engine)
    session_factory = create_session_factory(engine)
    unique_name = f"Upload Integration {uuid4()}"
    series = LibraryService(LibraryRepository(session_factory)).create_series(
        tenant_id="mysql-integration",
        name=unique_name,
    )
    artifacts = LocalArtifactStore(tmp_path / "artifacts", environment="test")
    uploads = UploadService(
        UploadRepository(session_factory),
        artifacts,
        config=UploadServiceConfig(
            preferred_segment_size_bytes=4,
            maximum_upload_size_bytes=1024,
            pipeline_version="mysql-upload-v1",
        ),
        probe_duration=lambda _path: 20_000,
    )
    source = b"abcdefghij"
    upload = uploads.create_session(
        tenant_id="mysql-integration",
        series_id=series.series_id,
        episode_order=1,
        original_filename="episode.mp4",
        media_type="video/mp4",
        total_size_bytes=len(source),
        idempotency_key="mysql-upload",
    )
    for number, data in enumerate((b"abcd", b"efgh", b"ij")):
        start = number * 4
        uploads.upload_segment(
            upload.upload_session_id,
            number,
            data,
            tenant_id="mysql-integration",
            checksum_sha256=hashlib.sha256(data).hexdigest(),
            start_byte=start,
            end_byte_exclusive=start + len(data),
            total_size_bytes=len(source),
        )
    completed = uploads.complete(
        upload.upload_session_id,
        tenant_id="mysql-integration",
    )
    second_source = b"klmnopqrst"
    second_upload = uploads.create_session(
        tenant_id="mysql-integration",
        series_id=series.series_id,
        episode_order=2,
        original_filename="episode-2.mp4",
        media_type="video/mp4",
        total_size_bytes=len(second_source),
        idempotency_key="mysql-upload-2",
    )
    for number, data in enumerate((b"klmn", b"opqr", b"st")):
        start = number * 4
        uploads.upload_segment(
            second_upload.upload_session_id,
            number,
            data,
            tenant_id="mysql-integration",
            checksum_sha256=hashlib.sha256(data).hexdigest(),
            start_byte=start,
            end_byte_exclusive=start + len(data),
            total_size_bytes=len(second_source),
        )
    second_completed = uploads.complete(
        second_upload.upload_session_id,
        tenant_id="mysql-integration",
    )
    jobs = ParseJobRepository(session_factory)
    queue = MySqlParseJobQueue(session_factory)
    now = now_unix_time_ms() + timedelta_to_ms(timedelta(seconds=1))
    barrier = Barrier(2)

    def claim(worker_id: str):
        barrier.wait()
        return queue.claim_next(
            worker_id,
            lease_duration=timedelta(minutes=2),
            now=now,
        )

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            claims = [
                executor.submit(claim, "worker-a"),
                executor.submit(claim, "worker-b"),
            ]
            leases = [future.result() for future in claims]
        claimed = [item for item in leases if item is not None]
        assert len(claimed) == 1
        first = claimed[0]
        assert first.job_id == completed.job_id
        checkpoint = jobs.save_checkpoint(
            first,
            VideoChunkCheckpointState(
                job_id=first.job_id,
                sequence_no=0,
                video_chunk_id=str(uuid4()),
                stage="PARSED",
                parse_artifact_uri="artifact://test/jobs/chunk-0000.json",
            ),
            now=now,
        )
        retry_at = now + timedelta_to_ms(timedelta(seconds=10))
        assert queue.retry(
            first,
            error="retry integration",
            retry_at=retry_at,
            duration_ms=0,
            now=now,
        )
        sibling = queue.claim_next(
            "worker-c",
            lease_duration=timedelta(minutes=2),
            now=now + timedelta_to_ms(timedelta(seconds=1)),
        )
        assert sibling is None
        second = queue.claim_next(
            "worker-d",
            lease_duration=timedelta(minutes=2),
            now=retry_at,
        )
        assert second is not None
        assert second.job_id == first.job_id
        assert second.lease_version == first.lease_version + 1
        with pytest.raises(ParseJobLeaseLostError):
            jobs.save_checkpoint(first, checkpoint, now=retry_at)
        assert queue.acknowledge(second, duration_ms=0, now=retry_at)
        sibling = queue.claim_next(
            "worker-e", lease_duration=timedelta(minutes=2), now=retry_at,
        )
        assert sibling is not None and sibling.job_id == second_completed.job_id
    finally:
        with engine.begin() as connection:
            connection.execute(
                delete(Series).where(
                    Series.series_id == series.series_id
                )
            )
        engine.dispose()
