from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select, text, inspect

from agent.video_parse_v2.input import ParseConfig
from engine.parsev2.diarization import assign_video_speakers, normalize_diarization
from engine.parsev2.observations import select_asr_window
from scripts.migrate_video_parse_content import migrate
from server.database import DatabaseEnvironment, create_database_engine, create_schema, create_session_factory
from server.model.parse_content import VideoParseContent
from server.model.task import VideoParseTask
from server.mq.video_parse_queue import MySqlVideoParseQueue
from server.repository import video_parse_content as result_store
from server.service.video_parse import VideoParseRuntimeService
from server.service.video_parse_errors import VideoParseConflictError, VideoParseLeaseLostError
from server.service.video_parse_persistence import VideoParsePersistenceService
from tests.server.test_video_parse_results import NOW, PAYLOAD, _committed, identity_for
from tests.server.test_video_parse_queue import _prepare


BINDING = {"source": "voices", "model": "/installed/community-1", "request_timeout_seconds": 600.0}
RAW = {"segments": [{"start": 0.0, "end": 1.0, "speaker": "SPEAKER_00"}],
       "exclusive_segments": [{"start": 0.0, "end": 1.0, "speaker": "SPEAKER_00"}]}
DIARIZATION = normalize_diarization(RAW, duration_ms=20000)


def test_provider_native_labels_keep_full_video_catalog_across_chunk_windows():
    asr = {"transcript": [
        {"start_seconds": 0, "end_seconds": 1000, "text": "first", "speaker_id": "SPEAKER_00"},
        {"start_seconds": 1000, "end_seconds": 2000, "text": "second", "speaker_id": "SPEAKER_01"},
    ]}
    runtime = object.__new__(VideoParseRuntimeService)
    runtime._persistence = SimpleNamespace(load_or_begin_video_asr=lambda *args: deepcopy(asr))
    runtime._video_asr_identity = lambda *_: {}
    runtime._video_transcripts = {}
    runtime._require_video_active = lambda _: None
    lease = SimpleNamespace(video_parse_task_id="native-voices", lease_version=1)
    asyncio.run(runtime._prepare_video_asr(lease, None, None))
    result = runtime._video_transcripts[("native-voices", 1)]
    selected = select_asr_window(result, start_ms=1000, end_ms=2000)
    assert selected["speaker_scope"] == "video"
    assert selected["speaker_scope_id"] == "native-voices"
    assert selected["speaker_ids"] == ["SPEAKER_00", "SPEAKER_01"]
    assert selected["transcript"][0]["speaker_id"] == "SPEAKER_01"
    assert "speaker_scope" not in asr


def enable_diarization(factory, task_id, identity):
    with factory.begin() as session:
        task = session.get(VideoParseTask, task_id)
        config = deepcopy(task.parse_config)
        config["models"]["diarization"] = BINDING
        task.parse_config = ParseConfig.model_validate(config).model_dump(mode="json")
    return {**identity, "model_binding": BINDING, "producer": {"weights": "verified-test-weights"}}


def test_assignment_keeps_cross_chunk_scope_and_rejects_mixed_or_absent_voices():
    first = {"start": 0.0, "end": 0.5, "speaker": "a"}
    second = {"start": 0.5, "end": 1.5, "speaker": "b"}
    grouping = normalize_diarization({"segments": [first, second], "exclusive_segments": [first, second]}, duration_ms=2000)
    original = {"transcript": [
        {"start_seconds": 0, "end_seconds": 1000, "text": "mixed", "speaker_id": None},
        {"start_seconds": 1000, "end_seconds": 1500, "text": "second", "speaker_id": None},
        {"start_seconds": 1600, "end_seconds": 1800, "text": "no speech", "speaker_id": None},
    ]}
    value = assign_video_speakers(original, grouping, scope_id="one-video-task")
    assert [cue["speaker_id"] for cue in value["transcript"]] == [None, "b", None]
    assert all(cue["speaker_id"] is None for cue in original["transcript"])
    selected = select_asr_window(value, start_ms=1000, end_ms=2000)
    assert selected["speaker_scope"] == "video" and selected["speaker_scope_id"] == "one-video-task"
    selected["speaker_ids"].append("cannot-mutate-shared-result")
    assert value["speaker_ids"] == ["a", "b"]


@pytest.mark.parametrize("failure", [False, True], ids=["completed-before-restart", "diarization-failed-before-restart"])
def test_restart_does_not_repeat_completed_transcription(tmp_path, monkeypatch, failure):
    url = f"sqlite+pysqlite:///{tmp_path / 'diarization-test.sqlite'}"
    engine = create_database_engine(url, environment=DatabaseEnvironment.TEST)
    create_schema(engine)
    factory, queue, repo, task_id, lease, asr_identity = _committed(engine)
    diarization_identity = enable_diarization(factory, task_id, asr_identity)
    monkeypatch.setattr("server.service.video_parse_persistence.now_utc", lambda: NOW)
    calls = []

    def runtime_for(repository):
        runtime = object.__new__(VideoParseRuntimeService)
        runtime._persistence = repository
        runtime._video_transcripts = {}
        runtime._model_manager = ContextVar("diarization-test-manager", default=None)
        runtime._video_asr_identity = lambda *_: asr_identity
        runtime._video_diarization_identity = lambda *_: diarization_identity
        runtime._require_video_active = lambda _: None
        runtime._transcription_model = lambda _: pytest.fail("completed ASR must not load a transcription model")
        runtime._artifact_store = SimpleNamespace(resolve=lambda _: tmp_path / "source.mp4")

        @asynccontextmanager
        async def stage(*_, **kwargs):
            yield

        @contextmanager
        def audio(*_, **kwargs):
            yield tmp_path / "full.wav"

        def diarize(_):
            calls.append(True)
            if failure and len(calls) == 1:
                raise RuntimeError("voice model interrupted")
            return RAW

        runtime._model_stage = stage
        runtime._media_service = SimpleNamespace(prepare_speaker_audio=audio)
        runtime._diarization_model = lambda _: SimpleNamespace(diarize=diarize, close=lambda: None)
        return runtime

    first = runtime_for(repo)
    if failure:
        with pytest.raises(RuntimeError, match="interrupted"):
            asyncio.run(first._prepare_video_asr(lease, repo.load_task_input(task_id), None, BINDING))
        with pytest.raises(VideoParseConflictError, match="diarization"):
            repo.finish_video_asr_preparation(lease, now=NOW)
    else:
        asyncio.run(first._prepare_video_asr(lease, repo.load_task_input(task_id), None, BINDING))
    assert queue.release_execution(lease, now=NOW)
    del first
    engine.dispose()
    engine = create_database_engine(url, environment=DatabaseEnvironment.TEST)
    try:
        factory = create_session_factory(engine)
        repo = VideoParsePersistenceService(factory)
        queue = MySqlVideoParseQueue(factory, video_concurrency=1)
        resumed = queue.claim_video("new-process", lease_duration=timedelta(minutes=5), now=NOW)
        assert resumed is not None
        second = runtime_for(repo)
        asyncio.run(second._prepare_video_asr(resumed, repo.load_task_input(task_id), None, BINDING))
        result = second._video_transcripts[(task_id, resumed.lease_version)]
        assert result["transcript"][0]["speaker_id"] == "SPEAKER_00"
        assert result["speaker_scope_id"] == task_id
        assert result["transcript"][0]["words"] == PAYLOAD["transcript"][0]["words"]
        assert len(calls) == (2 if failure else 1)
        repo.finish_video_asr_preparation(resumed, now=NOW)
        with pytest.raises(VideoParseLeaseLostError):
            repo.commit_video_diarization(lease, diarization_identity, DIARIZATION, now=NOW)
        with factory() as session:
            assert result_store.load_result(session, task_id, "asr") == PAYLOAD
    finally:
        engine.dispose()


@pytest.mark.parametrize("damage", ["checksum", "bounds", "missing"])
def test_bad_diarization_revokes_only_its_own_completion(repository, damage):
    factory, _, repo, task_id, lease, identity = _committed(repository[0])
    voice_identity = enable_diarization(factory, task_id, identity)
    assert repo.load_or_begin_video_diarization(lease, voice_identity, now=NOW) is None
    repo.commit_video_diarization(lease, voice_identity, DIARIZATION, now=NOW)
    with factory.begin() as session:
        row = session.scalar(select(VideoParseContent).where(VideoParseContent.video_task_id == task_id))
        value = deepcopy(row.diarization)
        if damage == "checksum": value["turns"][0]["speaker_id"] = "tampered"
        elif damage == "bounds": value["exclusive_turns"][0]["end_ms"] = 999999
        else: value = None
        row.diarization = value
    assert repo.load_or_begin_video_diarization(lease, voice_identity, now=NOW) is None
    assert repo.load_or_begin_video_asr(lease, identity, now=NOW) == PAYLOAD
    with pytest.raises(VideoParseConflictError, match="diarization"):
        repo.finish_video_asr_preparation(lease, now=NOW)


@pytest.mark.parametrize("mismatch", [False, True])
def test_new_parse_version_reuses_only_identical_asr(repository, mismatch):
    factory, queue, repo, task_id, lease, identity = _committed(repository[0])
    assert queue.release_execution(lease, now=NOW)
    new_id = str(uuid4())
    with factory.begin() as session:
        old = session.get(VideoParseTask, task_id)
        old.status = "PARSING_FAILED"
        old.detail = {"_worker_runtime_v2": {"accepted": False, "capacity_acquired": False}}
        session.add(VideoParseTask(
            id=new_id, video_id=old.video_id, pipeline_version="v2", status="PENDING",
            parse_config=deepcopy(old.parse_config), chunk_count=old.chunk_count, version=old.version+1,
            next_execute_time=NOW, max_attempts=3, is_test=True,
            detail={"_worker_runtime_v2": {"accepted": True}},
        ))
    repo, current, _ = _prepare(factory, queue, new_id, chunk_count=2)
    new_identity = identity_for(repo, new_id)
    if mismatch: new_identity["producer"] = {"different": True}
    reused = repo.load_or_begin_video_asr(current, new_identity, now=NOW)
    assert reused == (None if mismatch else PAYLOAD)
    with factory() as session:
        assert result_store.load_result(session, task_id, "asr") == PAYLOAD
        if not mismatch:
            detail = result_store.get_metadata(session, new_id).detail["asr"]
            assert detail["reused_from_task_id"] == task_id
            assert detail["committed_lease_version"] == current.lease_version


def test_additive_column_migration_preserves_existing_asr(repository):
    factory, _, _, task_id, _, _ = _committed(repository[0])
    with repository[0].begin() as connection:
        connection.execute(text("ALTER TABLE video_parse_content DROP COLUMN diarization"))
    assert migrate(repository[0])["add_diarization_column"] is True
    assert "diarization" not in {c["name"] for c in inspect(repository[0]).get_columns("video_parse_content")}
    migrate(repository[0], apply=True, writers_stopped=True)
    assert migrate(repository[0])["add_diarization_column"] is False
    with factory() as session:
        assert result_store.load_result(session, task_id, "asr") == PAYLOAD
