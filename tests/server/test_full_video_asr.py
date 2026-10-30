from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent.video_parse_v2.input import ParseInput
from agent.video_parse_v2.state import ChunkExecution, LeaseToken
from engine.parsev2.observations import select_asr_window
from server.model.dto.jobDTO import VideoParseLease
from server.service.video_parse import VideoParseRuntimeService
from tests.server.test_video_parse_worker import _parse_config


def test_selection_keeps_boundary_context_and_never_mutates_shared_words():
    transcript = {"language": "en", "transcript": [
        {"start_seconds": 110000, "end_seconds": 120000, "text": "before"},
        {"start_seconds": 117000, "end_seconds": 124000, "text": "whole sentence",
         "words": [{"text": "whole", "start_ms": 117000, "end_ms": 120500, "score": 0.8}]},
        {"start_seconds": 180000, "end_seconds": 181000, "text": "after"},
    ]}
    selected = select_asr_window(transcript, start_ms=120000, end_ms=180000)
    assert selected == {"language": "en", "transcript": [transcript["transcript"][1]]}
    selected["transcript"][0]["words"][0]["start_ms"] = 0
    assert transcript["transcript"][1]["words"][0]["start_ms"] == 117000
    assert select_asr_window(transcript, start_ms=0, end_ms=100000) == {"language": "en", "transcript": []}


@pytest.mark.parametrize("outcome", ["success", "provider_failure", "lease_lost"])
@pytest.mark.parametrize("chunk_count", [2, 10])
def test_one_video_transcription_feeds_concurrent_chunks_and_is_rebuilt_on_resume(tmp_path, outcome, chunk_count):
    config = _parse_config()
    config["models"]["asr"]["mode"] = "lite"
    config["models"]["asr"]["scope"] = "video"
    config["visual_input"]["mode"] = "video"
    config["execution"]["retry_initial_delay_seconds"] = 0.001
    config["execution"]["retry_max_delay_seconds"] = 0.001
    inputs = ParseInput(series_id="series", video_id="video", parse_config=config)
    task_id, video_id = str(uuid4()), str(uuid4())
    lease = VideoParseLease(task_id, video_id, "worker", 1, datetime.now(UTC) + timedelta(minutes=5), 1, 3, {})
    task_input = SimpleNamespace(task=SimpleNamespace(status="PARSING", lease_version=1),
                                 original_source_uri="source.mp4", inference_proxy_uri=None,
                                 duration_seconds=chunk_count * 60000)
    runtime = object.__new__(VideoParseRuntimeService)
    runtime._video_transcripts = {}
    runtime._video_lease = ContextVar("video_asr_lease", default=None)
    runtime._model_manager = ContextVar("video_asr_manager", default=None)
    runtime._persistence = SimpleNamespace(load_task_input=lambda _: task_input,
                                          finish_video_asr_preparation=lambda _: None)
    runtime._parse_input = lambda _: inputs
    runtime._artifact_store = SimpleNamespace(resolve=lambda uri: tmp_path / uri)
    runtime._require_video_active = lambda _: None
    calls, stages, snapshots = [], [], []

    @asynccontextmanager
    async def stage(*args, **kwargs):
        stages.append(kwargs["stage"])
        yield

    runtime._model_stage = stage

    def transcribe(path):
        calls.append(path)
        assert path.exists()
        if outcome == "provider_failure":
            raise RuntimeError("provider failed")
        if outcome == "lease_lost":
            runtime._require_video_active = lambda _: (_ for _ in ()).throw(RuntimeError("lease lost"))
        return {"language": "en", "segments": [
            {"start": 58, "end": 62, "text": "crossing", "words": [
                {"word": "crossing", "start": 58, "end": 62, "score": 0.9}]},
        ]}

    runtime._transcription_model = lambda _: SimpleNamespace(input_mode="audio", transcribe=transcribe, close=lambda: None)

    @contextmanager
    def audio(*args, **kwargs):
        assert kwargs["start_ms"] == 0 and kwargs["end_ms"] == chunk_count * 60000
        path = tmp_path / "full.wav"
        path.write_bytes(b"audio")
        try:
            yield path
        finally:
            path.unlink()

    @contextmanager
    def media(*args, **kwargs):
        assert (task_id, 1) in runtime._video_transcripts
        yield

    runtime._media_service = SimpleNamespace(prepare_speaker_audio=audio, prepare_episode=media)

    async def prepared(_):
        return SimpleNamespace()

    runtime._ensure_chunk_media = prepared
    runtime._require_chunk_execution = lambda _: SimpleNamespace(state=SimpleNamespace(task_input=task_input))

    async def parse(_):
        executions = [ChunkExecution(
            chunk_parse_task_id=str(uuid4()), video_parse_task_id=task_id, sequence_no=index,
            chunk_start_seconds=index * 60000, chunk_end_seconds=(index + 1) * 60000,
            allowed_start_seconds=index * 60000, allowed_end_seconds=(index + 1) * 60000,
            inputs=inputs, lease=LeaseToken("worker", 1, lease.lease_expire_time),
        ) for index in range(chunk_count)]
        results = await asyncio.gather(*(runtime.transcribe_chunk(execution) for execution in executions))
        assert results[0] == results[1]
        assert results[0]["transcript"][0]["start_seconds"] == 58000
        assert results[1]["transcript"][0]["words"][0]["start_ms"] == 58000
        assert all(result == {"language": "en", "transcript": []} for result in results[2:])
        snapshots.append(results)

        # Exercise the current dev subgraph, including its node-local retry,
        # after all ten chunks have entered Understanding together.
        from agent.video_parse_v2.understanding.workflow import ChunkUnderstandingWorkflow
        from engine.parsev2.schemas import VideoParseResult

        by_id = {execution.chunk_parse_task_id: execution for execution in executions}
        attempts, committed = {}, []
        ready = asyncio.Event()

        async def load(task_id):
            return by_id[task_id]

        async def understand(execution, *, asr_result, ocr_result, prompt):
            index = execution.sequence_no
            attempts[index] = attempts.get(index, 0) + 1
            assert asr_result == results[index]
            if len(attempts) == chunk_count:
                ready.set()
            await asyncio.wait_for(ready.wait(), 2)
            if index == 0 and attempts[index] == 1:
                raise TimeoutError("transient understanding failure")
            return VideoParseResult(persons=[], timeline=[])

        async def commit(execution, result, **kwargs):
            committed.append(execution.sequence_no)

        runtime.load_chunk_execution = load
        runtime.understand_chunk = understand
        runtime.commit_chunk_result = commit
        runtime.cleanup_chunk_execution = prepared
        runtime.notify_chunk_committed = prepared
        workflow = ChunkUnderstandingWorkflow(runtime=runtime)
        await asyncio.gather(*(workflow.InvokeUnderstanding(task_id) for task_id in by_id))
        assert sorted(committed) == list(range(chunk_count))
        assert attempts == {index: 2 if index == 0 else 1 for index in range(chunk_count)}
        assert len(calls) == len(snapshots)  # Neither Chunk reads nor retries rerun ASR.

    runtime.agent = SimpleNamespace(InvokeVideoParse=parse)
    if outcome == "success":
        asyncio.run(runtime.prepare(lease))
        assert len(calls) == 1 and len(snapshots) == 1
        assert runtime._video_transcripts == {}
        asyncio.run(runtime.prepare(lease))
        assert len(calls) == 2 and snapshots[0] == snapshots[1]
    else:
        with pytest.raises(RuntimeError, match="provider failed|lease lost"):
            asyncio.run(runtime.prepare(lease))
        assert not snapshots
    assert runtime._video_transcripts == {} and runtime._video_lease.get() is None
    assert not (tmp_path / "full.wav").exists()
    assert set(stages) == {"asr"}


def test_historical_scope_stays_chunk_and_video_scope_requires_understanding():
    config = _parse_config()
    inputs = ParseInput(series_id="s", video_id="v", parse_config=config)
    assert inputs.parse_config.models.asr.scope == "chunk"
    assert "scope" not in inputs.parse_config.models.asr.model_dump()
    config["models"]["asr"]["scope"] = "video"
    config["models"]["asr"]["mode"] = "lite"
    config["understanding_enabled"] = False
    with pytest.raises(ValueError, match="requires Understanding"):
        ParseInput(series_id="s", video_id="v", parse_config=config)


def test_cancelled_asr_retains_audio_and_provider_until_native_execution_exits(tmp_path):
    from threading import Event
    from server.utils.execution import BlockingCallCancelled, ExecutionCalls, execution_calls

    started, release = Event(), Event()
    closed = []
    audio_path = tmp_path / "full.wav"
    runtime = object.__new__(VideoParseRuntimeService)
    runtime._model_manager = ContextVar("asr_cancel_manager", default=None)
    runtime._video_transcripts = {}
    runtime._artifact_store = SimpleNamespace(resolve=lambda uri: tmp_path / uri)
    runtime._require_video_active = lambda _: None

    @asynccontextmanager
    async def stage(*args, **kwargs):
        token = execution_calls.set(ExecutionCalls())
        try:
            yield
        finally:
            execution_calls.reset(token)

    @contextmanager
    def audio(*args, **kwargs):
        audio_path.write_bytes(b"audio")
        try:
            yield audio_path
        finally:
            audio_path.unlink()

    def transcribe(path):
        started.set()
        assert release.wait(5)
        assert path.exists() and not closed
        return {"segments": []}

    runtime._model_stage = stage
    runtime._media_service = SimpleNamespace(prepare_speaker_audio=audio)
    runtime._transcription_model = lambda _: SimpleNamespace(
        input_mode="audio", transcribe=transcribe, close=lambda: closed.append(True))
    identity = str(uuid4())
    lease = SimpleNamespace(video_parse_task_id=identity, lease_version=1)
    source = SimpleNamespace(original_source_uri="original.mp4", duration_seconds=60000)

    async def cancel():
        pending = asyncio.create_task(runtime._prepare_video_asr(lease, source, None))
        assert await asyncio.to_thread(started.wait, 2)
        pending.cancel()
        with pytest.raises(BlockingCallCancelled) as caught:
            await pending
        assert audio_path.exists() and not closed
        assert runtime._video_transcripts == {}
        release.set()
        await asyncio.wrap_future(caught.value.completion)
        assert not audio_path.exists() and closed == [True]

    try:
        asyncio.run(cancel())
    finally:
        release.set()
