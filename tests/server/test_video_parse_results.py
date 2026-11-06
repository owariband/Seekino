from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import event, select

from agent.video_parse_v2.input import ParseInput
from gateway.config import OpenAIAudioSourceConfig, WhisperXSourceConfig
from server.database import DatabaseEnvironment, create_database_engine, create_schema, create_session_factory
from server.model.task import VideoParseTask
from server.model.parse_content import VideoParseContent
from server.mq.video_parse_queue import MySqlVideoParseQueue
from server.repository import video_parse_content as result_store
from server.service.parse_control import set_paused
from server.service.video_parse import VideoParseRuntimeService
from server.service.video_parse_errors import VideoParseConflictError, VideoParseLeaseLostError
from server.service.video_parse_persistence import VideoParsePersistenceService
from server.service.video_parse_results import build_asr_identity, json_digest
from tests.server.test_video_parse_queue import NOW, _prepare, _whole_video_asr_task


PAYLOAD = {"language": "en", "transcript": [
    {"start_seconds": 0, "end_seconds": 1000, "text": "hello", "speaker_id": None,
     "words": [{"text": "hello", "start_ms": 0, "end_ms": 900, "score": 0.9}]},
]}


def identity_for(repo, task_id):
    inputs = repo.load_task_input(task_id)
    model = ParseInput(series_id=inputs.series_id, video_id=inputs.task.video_id,
                       parse_config=inputs.task.parse_config).parse_config.models.asr
    source = OpenAIAudioSourceConfig(provider="fixture", base_url="https://asr.invalid/v1",
                                    api_key_env="TEST_ASR_KEY")
    return build_asr_identity(inputs, model, source)


def _ready(engine):
    factory = create_session_factory(engine)
    task_id = _whole_video_asr_task(factory)
    queue = MySqlVideoParseQueue(factory, video_concurrency=1)
    repo, lease, _ = _prepare(factory, queue, task_id, chunk_count=2)
    return factory, queue, repo, task_id, lease, identity_for(repo, task_id)


def _committed(engine):
    state = _ready(engine)
    _, _, repo, _, lease, identity = state
    assert repo.load_or_begin_video_asr(lease, identity, now=NOW) is None
    repo.commit_video_asr(lease, identity, PAYLOAD, now=NOW)
    return state


def retry_after_understanding_failure(factory, queue, repo, task_id, lease):
    from server.model.upload import UploadSession
    from server.repository.upload import UploadRepository

    child = queue.claim_chunk("understanding", lease_duration=timedelta(minutes=1), now=NOW)
    assert child is not None
    assert queue.fail_chunk(child, error="Kimi 504", stage="understanding", reason="model_gateway_timeout", now=NOW)
    queue.retry_video(lease, error="coordinator stopped after child failure", now=NOW)
    queue.reap_expired(now=NOW)
    upload_id = str(uuid4())
    inputs = repo.load_task_input(task_id)
    with factory.begin() as session:
        task = session.get(VideoParseTask, task_id)
        assert task.status == "PARSING_FAILED" and task.lease_owner is None
        version = task.lease_version
        # A previous coordinator had preparation failures before topology became ready.
        detail = deepcopy(task.detail)
        detail["_worker_runtime_v2"]["prepare_failures"] = 2
        task.detail = detail
        session.add(UploadSession(
            id=upload_id, tenant_id="mine", series_id=inputs.series_id, episode_order=1, upload_sequence=1,
            filename="episode.mp4", media_type="video/mp4", size_bytes=10, segment_size_bytes=10,
            segment_count=1, status="COMPLETED", idempotency_key=upload_id, expire_time=NOW + timedelta(days=1),
            video_id=task.video_id, initial_task_id=task_id, parse_config=task.parse_config,
        ))
    retried = UploadRepository(factory).retry_parse(
        upload_id, tenant_id="mine", expected_job_version=1, expected_lease_version=version, now=NOW)
    assert retried.video_parse_task_id == task_id and retried.status == "PARSING"
    assert retried.detail["_worker_runtime_v2"].get("prepare_failures", 0) == 0


@pytest.mark.parametrize("recovery", ["coordinator_failure", "downstream_failure", "restart", "pause_resume"])
def test_completed_asr_survives_runtime_and_database_reconnection_without_new_transcription(tmp_path, monkeypatch, recovery):
    url = f"sqlite+pysqlite:///{tmp_path / 'results-test.sqlite'}"
    engine = create_database_engine(url, environment=DatabaseEnvironment.TEST)
    create_schema(engine)
    factory, queue, repo, task_id, lease, identity = _ready(engine)
    clock = [NOW]
    monkeypatch.setattr("server.service.video_parse_persistence.now_utc", lambda: clock[0])
    calls, loads, extracts = [], [], []

    def make_runtime(repo, queue, *, fail):
        runtime = object.__new__(VideoParseRuntimeService)
        runtime._video_transcripts = {}
        runtime._video_lease = ContextVar("result_lease", default=None)
        runtime._model_manager = ContextVar("result_manager", default=None)
        runtime._persistence = repo
        runtime._parse_input = lambda inputs: ParseInput(
            series_id=inputs.series_id, video_id=inputs.task.video_id, parse_config=inputs.task.parse_config)
        runtime._video_asr_identity = lambda *_: identity
        runtime._artifact_store = SimpleNamespace(resolve=lambda uri: tmp_path / "source.mp4")

        def active(current):
            if not queue.heartbeat_video(current, lease_duration=timedelta(minutes=5), now=clock[0]):
                raise VideoParseLeaseLostError("stale lease")
        runtime._require_video_active = active

        @asynccontextmanager
        async def stage(*args, **kwargs):
            yield
        runtime._model_stage = stage

        @contextmanager
        def audio(*args, **kwargs):
            extracts.append(True)
            yield tmp_path / "full.wav"

        @contextmanager
        def media(*args, **kwargs):
            assert runtime._video_transcripts
            yield
        runtime._media_service = SimpleNamespace(prepare_speaker_audio=audio, prepare_episode=media)

        def transcribe(path):
            calls.append(path)
            return {"language": "en", "segments": [
                {"start": 0.0, "end": 1.0, "text": "hello", "words": [
                    {"word": "hello", "start": 0.0, "end": 0.9, "score": 0.9}]}]}

        def transport(_):
            loads.append(True)
            return SimpleNamespace(input_mode="audio", transcribe=transcribe, close=lambda: None)
        runtime._transcription_model = transport

        async def invoke(_):
            value = next(iter(runtime._video_transcripts.values()))
            assert value == PAYLOAD
            if fail:
                raise RuntimeError("downstream failed after ASR")
        runtime.agent = SimpleNamespace(InvokeVideoParse=invoke)
        return runtime

    try:
        first = make_runtime(repo, queue, fail=True)
        with pytest.raises(RuntimeError, match="downstream failed"):
            asyncio.run(first.prepare(lease))
        assert first._video_transcripts == {}
        if recovery == "downstream_failure":
            retry_after_understanding_failure(factory, queue, repo, task_id, lease)
        elif recovery == "coordinator_failure":
            queue.retry_video(lease, error="coordinator failed", now=NOW)
            clock[0] = NOW + timedelta(minutes=10)
        elif recovery == "restart":
            clock[0] = NOW + timedelta(minutes=10)
            assert queue.reap_expired_videos(now=clock[0]) == 1
        else:
            with factory.begin() as session:
                set_paused(session, task_id, tenant_id="mine", paused=True, expected_version=1,
                           expected_lease_version=lease.lease_version, now=NOW)
                current_version = session.get(VideoParseTask, task_id).lease_version
                set_paused(session, task_id, tenant_id="mine", paused=False, expected_version=1,
                           expected_lease_version=current_version, now=NOW)
        # Discard the original runtime and all database connections, then recover from disk.
        del first
        engine.dispose()
        engine = create_database_engine(url, environment=DatabaseEnvironment.TEST)
        factory = create_session_factory(engine)
        repo = VideoParsePersistenceService(factory)
        queue = MySqlVideoParseQueue(factory, video_concurrency=1)
        resumed = queue.claim_video("resumed", lease_duration=timedelta(minutes=5), now=clock[0])
        assert resumed is not None and resumed.lease_version > lease.lease_version
        second = make_runtime(repo, queue, fail=False)
        asyncio.run(second.prepare(resumed))
        assert second._video_transcripts == {}
        assert len(calls) == len(loads) == len(extracts) == 1
        with pytest.raises(VideoParseLeaseLostError):
            repo.commit_video_asr(lease, identity, PAYLOAD, now=clock[0])
        with factory() as session:
            stored = result_store.get_metadata(session, task_id)
            assert stored.detail["asr"]["committed_lease_version"] == lease.lease_version
            assert result_store.load_result(session, task_id, "asr") == PAYLOAD
    finally:
        engine.dispose()


@pytest.mark.parametrize("changed", ["media_sha256", "schema_version", "model_binding", "producer"])
def test_changed_input_or_producer_cannot_reuse_or_replace_asr(repository, changed):
    factory, _, repo, task_id, lease, identity = _committed(repository[0])
    different = deepcopy(identity)
    different[changed] = "different"
    with pytest.raises(VideoParseConflictError):
        repo.load_or_begin_video_asr(lease, different, now=NOW)
    with pytest.raises(VideoParseConflictError):
        repo.commit_video_asr(lease, different, PAYLOAD, now=NOW)
    with factory() as session:
        assert result_store.load_result(session, task_id, "asr") == PAYLOAD


@pytest.mark.parametrize("damage", ["checksum", "schema", "time_bounds", "missing_body"])
def test_corrupt_asr_is_rebuilt_without_erasing_summary(repository, damage):
    factory, _, repo, task_id, lease, identity = _committed(repository[0])
    with factory.begin() as session:
        row = session.scalar(select(VideoParseContent).where(VideoParseContent.video_task_id == task_id))
        body = deepcopy(row.asr)
        if damage == "checksum":
            body["transcript"][0]["text"] = "tampered"
        elif damage == "schema":
            body = {"segments": []}
        elif damage == "time_bounds":
            body["transcript"][0]["end_seconds"] = 9999999
        else:
            body = None
        row.asr = body
        row.display = {"description": "preserved"}
        row.detail = {**row.detail, "display": {"audit": "preserved"}}
    assert repo.load_or_begin_video_asr(lease, identity, now=NOW) is None
    # A missing/corrupt body must also revoke the durable completion receipt.
    with pytest.raises(VideoParseConflictError, match="no committed result"):
        repo.finish_video_asr_preparation(lease, now=NOW)
    repo.commit_video_asr(lease, identity, PAYLOAD, now=NOW)
    repo.finish_video_asr_preparation(lease, now=NOW)
    with factory() as session:
        assert result_store.load_result(session, task_id, "asr") == PAYLOAD
        assert result_store.load_result(session, task_id, "display") == {"description": "preserved"}
        assert result_store.get_metadata(session, task_id).detail["display"] == {"audit": "preserved"}


def test_no_completion_before_atomic_asr_commit_and_idempotent_recovery_does_not_rewrite_json(repository):
    factory, queue, repo, task_id, lease, identity = _ready(repository[0])
    assert repo.load_or_begin_video_asr(lease, identity, now=NOW) is None
    with pytest.raises(VideoParseConflictError, match="no committed result"):
        repo.finish_video_asr_preparation(lease, now=NOW)
    with pytest.raises(ValueError):
        repo.commit_video_asr(lease, identity, {"transcript": [{"start_seconds": -1}]}, now=NOW)
    repo.commit_video_asr(lease, identity, PAYLOAD, now=NOW)
    with factory() as session:
        version = result_store.get_metadata(session, task_id).version
    statements = []
    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement.lower())
    event.listen(repository[0], "before_cursor_execute", capture)
    try:
        assert repo.load_or_begin_video_asr(lease, identity, now=NOW) == PAYLOAD
        repo.commit_video_asr(lease, identity, PAYLOAD, now=NOW)
        assert not any("update video_parse_content" in sql for sql in statements)
        statements.clear()
        assert queue.heartbeat_video(lease, lease_duration=timedelta(minutes=5), now=NOW)
        repo.load_task_input(task_id)
        assert not any("video_parse_content" in sql for sql in statements)
    finally:
        event.remove(repository[0], "before_cursor_execute", capture)
    with factory() as session:
        assert result_store.get_metadata(session, task_id).version == version


def test_weight_and_runtime_configuration_identity_changes_without_loading_models(tmp_path):
    weights, alignment = tmp_path / "model", tmp_path / "align"
    weights.mkdir()
    alignment.mkdir()
    binary = weights / "model.bin"
    binary.write_bytes(b"first")
    (alignment / "model.bin").write_bytes(b"align")
    inputs = SimpleNamespace(content_hash="a" * 64, duration_seconds=20000, total_bytes=10)
    model = SimpleNamespace(model=str(weights), model_dump=lambda **_: {"model": str(weights)})
    source = WhisperXSourceConfig(language="en", alignment_models={"en": str(alignment)})
    first = build_asr_identity(inputs, model, source)
    assert first == build_asr_identity(inputs, model, source)
    assert json_digest(first) != json_digest(build_asr_identity(
        inputs, model, source.model_copy(update={"batch_size": 1})))
    binary.write_bytes(b"other")  # Same path and size, different weights.
    assert first["producer"]["weights"] != build_asr_identity(inputs, model, source)["producer"]["weights"]


def test_new_python_process_loads_committed_asr_without_provider(tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path

    url = f"sqlite+pysqlite:///{tmp_path / 'process-test.sqlite'}"
    engine = create_database_engine(url, environment=DatabaseEnvironment.TEST)
    create_schema(engine)
    _, queue, _, task_id, lease, _ = _committed(engine)
    assert queue.release_execution(lease, now=NOW)
    engine.dispose()
    # No runtime, provider, transcript cache, or SQL connection crosses this boundary.
    code = """
import asyncio
import sys
from datetime import timedelta
from server.database import DatabaseEnvironment, create_database_engine, create_session_factory
from server.mq.video_parse_queue import MySqlVideoParseQueue
from server.service.video_parse import VideoParseRuntimeService
from server.service import video_parse_persistence as persistence
from tests.server.test_video_parse_results import NOW, PAYLOAD, identity_for
engine = create_database_engine(sys.argv[1], environment=DatabaseEnvironment.TEST)
try:
    factory = create_session_factory(engine)
    repo = persistence.VideoParsePersistenceService(factory)
    persistence.now_utc = lambda: NOW
    queue = MySqlVideoParseQueue(factory, video_concurrency=1)
    lease = queue.claim_video('new-process', lease_duration=timedelta(minutes=5), now=NOW)
    assert lease and lease.video_parse_task_id == sys.argv[2]
    runtime = object.__new__(VideoParseRuntimeService)
    runtime._persistence = repo
    runtime._video_transcripts = {}
    runtime._video_asr_identity = lambda *_: identity_for(repo, sys.argv[2])
    def active(lease):
        assert queue.heartbeat_video(lease, lease_duration=timedelta(minutes=5), now=NOW)
    runtime._require_video_active = active
    def forbidden(*args, **kwargs):
        raise AssertionError('completed ASR must not enter inference or construct a provider')
    runtime._model_stage = runtime._transcription_model = forbidden
    asyncio.run(runtime._prepare_video_asr(lease, repo.load_task_input(sys.argv[2]), None))
    assert runtime._video_transcripts[(sys.argv[2], lease.lease_version)] == PAYLOAD
    print('recovered_without_provider')
finally:
    engine.dispose()
"""
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run([sys.executable, "-c", code, url, task_id], cwd=root,
                            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": str(root)},
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert "recovered_without_provider" in result.stdout


def test_successful_asr_preparation_resets_consecutive_failure_budget(repository):
    factory, queue, repo, task_id, lease, identity = _committed(repository[0])
    with factory.begin() as session:
        task = session.get(VideoParseTask, task_id)
        detail = deepcopy(task.detail)
        detail["_worker_runtime_v2"]["prepare_failures"] = 2
        task.detail = detail
    repo.finish_video_asr_preparation(lease, now=NOW)
    with factory() as session:
        task = session.get(VideoParseTask, task_id)
        assert task.detail["_worker_runtime_v2"].get("prepare_failures", 0) == 0
    assert repo.load_or_begin_video_asr(lease, identity, now=NOW) == PAYLOAD
