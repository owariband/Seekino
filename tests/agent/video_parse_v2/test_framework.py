from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from agent.common.chunk_understanding import understand_chunk
from agent.video_parse_v2 import ParseAgent
from agent.video_parse_v2.timelineEvent_canvas import CanvasCompletion
from agent.video_parse_v2.prompts import build_canvas_understanding_prompt
from agent.video_parse_v2.scheduler import ChunkScheduler, claimable_chunk_sequences
from agent.video_parse_v2.understanding import ChunkUnderstandingWorkflow
from agent.video_parse_v2.input import ParseInput
from agent.video_parse_v2.prefix import PrefixContextAgent
from agent.video_parse_v2.refine import IntervalRefine
from agent.video_parse_v2.state import (
    ChunkExecution,
    ChunkScheduleEntry,
    ChunkScheduleSnapshot,
    ChunkTaskStatus,
    LeaseToken,
    VideoChunk,
    VideoRunSnapshot,
    VideoTaskStatus,
)
from engine.parsev2.schemas import VideoParseResult
from engine.parsev2.observations import OCRResult


def _lease() -> LeaseToken:
    return LeaseToken(
        owner="worker-1",
        version=3,
        expires_at=datetime.now(UTC) + timedelta(minutes=1),
    )


def _inputs(**switches: bool) -> ParseInput:
    compact = switches.get("compact_enabled", False)
    ocr = switches.get("ocr_enabled", False)
    return ParseInput.model_validate({
        "series_id": "series-1",
        "video_id": "video-1",
        "parse_config": {
            "chunk_duration": 20,
            "interval_duration": 60,
            "batch_max_size": 10,
            "dispatch_interval_seconds": 0,
            "models": {
                "asr": {
                    "source": "openai-main", "model": "asr-model",
                    "request_timeout_seconds": 60, "max_output_tokens": 2048,
                    "mode": "lite", "fps": 1,
                },
                "understanding": {
                    "source": "ark-main", "model": "vision-model",
                    "request_timeout_seconds": 120, "max_output_tokens": 8192,
                },
                "ocr": ({
                    "source": "ark-main", "model": "ocr-model",
                    "request_timeout_seconds": 60, "max_output_tokens": 2048,
                } if ocr else None),
                "prefix_context": ({
                    "source": "ark-main", "model": "prefix-model",
                    "request_timeout_seconds": 120, "max_output_tokens": 4096,
                } if compact else None),
                "embedding": {
                    "source": "ark-embedding", "model": "embed-model",
                    "dimensions": 1024, "video_fps": 1,
                    "request_timeout_seconds": 60,
                },
            },
            "visual_input": {
                "mode": "ordered_frames", "fps": 1,
                "max_frames_per_chunk": 30, "max_height": 720,
            },
            "ocr_enabled": ocr,
            "understanding_enabled": switches.get("understanding_enabled", True),
            "compact_enabled": compact,
            "prefix_context": ({
                "series_compact_max_chars": 4000,
                "video_compact_max_chars": 2000,
                "max_tool_calls": 1,
            } if compact else None),
            "indexing": {"sparse_enabled": False, "contract_fingerprint": "0" * 64},
            "execution": {
                "parse_concurrency": 10,
                "refine_concurrency": 10,
                "index_concurrency": 10,
                "parse_execution_timeout_seconds": 1800,
                "refine_execution_timeout_seconds": 1800,
                "index_execution_timeout_seconds": 1800, "max_retry_count": 0,
                "retry_initial_delay_seconds": 1, "retry_max_delay_seconds": 30,
                "max_model_calls": 100,
            },
        },
    })


def _result(
    description: str = "爱音走进房间", *, offset_ms: int = 0
) -> VideoParseResult:
    return VideoParseResult.model_validate({
        "persons": ["爱音"],
        "timeline": [{
            "event_type": "ACTION",
            "start_seconds": offset_ms + 1000,
            "end_seconds": offset_ms + 3000,
            "description": description,
            "participants": ["爱音"],
            "visible_persons": ["爱音"],
        }],
    })


def test_entry_requires_its_actual_runtime_dependency() -> None:
    with pytest.raises(TypeError, match="runtime"):
        ParseAgent()


def test_main_graph_prepares_fixed_topology_then_completes() -> None:
    calls: list[object] = []
    chunks = (
        VideoChunk("chunk-0", 0, 0, 20_000),
        VideoChunk("chunk-1", 1, 20_000, 35_000),
    )

    class Runtime:
        async def load_video_run(self, task_id):
            calls.append("load")
            return VideoRunSnapshot(
                task_id, VideoTaskStatus.PREPARING, _inputs(), 35_000, _lease(), ()
            )

        async def ensure_video_chunks(self, task_id, lease, plans):
            calls.append(("chunks", lease.version, tuple(plans)))
            return chunks

        async def ensure_parse_tasks(self, task_id, lease, loaded, intervals):
            calls.append(("tasks", tuple(intervals)))

        async def activate_video_task(self, task_id, lease):
            calls.append("activate")

        async def load_chunk_schedule(self, task_id):
            calls.append("schedule")
            return ChunkScheduleSnapshot(
                tasks=tuple(
                    ChunkScheduleEntry(
                        f"task-{index}", index, ChunkTaskStatus.COMPLETE, phase=3, understanding_committed=True
                    )
                    for index in range(2)
                ),
                wait_timeout_seconds=0,
            )

        async def advance_video_task(self, task_id, lease):
            calls.append(("advance", lease.version))
            return VideoTaskStatus.COMPLETE

    state = asyncio.run(ParseAgent(runtime=Runtime()).run("video-task-1"))
    assert state["status"] == VideoTaskStatus.COMPLETE
    assert state["video_chunks"] == chunks
    assert calls[0] == "load"
    assert calls[2][0] == "tasks"
    intervals = calls[2][1]
    assert len(intervals) == 1
    assert intervals[0].member_sequence_nos == (0, 1)


def test_index_only_resume_never_prepares_or_schedules_chunks() -> None:
    runtime = SimpleNamespace(
        load_video_run=AsyncMock(return_value=VideoRunSnapshot(
            "video-task-1", VideoTaskStatus.INDEXING, _inputs(), 20_000, _lease(),
            (VideoChunk("chunk-0", 0, 0, 20_000),),
        )),
        advance_video_task=AsyncMock(return_value=VideoTaskStatus.COMPLETE),
        ensure_video_chunks=AsyncMock(side_effect=AssertionError("must not prepare")),
        load_chunk_schedule=AsyncMock(side_effect=AssertionError("must not parse")),
    )
    state = asyncio.run(ParseAgent(runtime=runtime).run("video-task-1"))
    assert state["status"] == VideoTaskStatus.COMPLETE
    runtime.advance_video_task.assert_awaited_once()
    runtime.ensure_video_chunks.assert_not_awaited()
    runtime.load_chunk_schedule.assert_not_awaited()


def test_scheduler_opens_next_batch_only_at_ninety_percent() -> None:
    # An Index failure cannot erase committed understanding or hold the next batch.
    first_eight = tuple(
        (index, 3 if index < 8 else 1,
         ChunkTaskStatus.FAILED if index < 8 else ChunkTaskStatus.QUEUED, index < 8)
        for index in range(20)
    )
    assert claimable_chunk_sequences(first_eight, 10) == {8, 9}
    first_nine = tuple(
        (index, 2 if index < 9 else 1,
         ChunkTaskStatus.WAITING if index < 9 else ChunkTaskStatus.QUEUED, index < 9)
        for index in range(20)
    )
    assert claimable_chunk_sequences(first_nine, 10) == set(range(9, 20))
    active = tuple(
        (index, phase, ChunkTaskStatus.RUNNING if 9 <= index < 19 else status, committed)
        for index, phase, status, committed in first_nine
    )
    # Batch policy opens work; the transactional claim enforces phase capacity.
    assert claimable_chunk_sequences(active, 10) == {19}


def test_scheduler_observes_progress_without_a_dispatch_permission() -> None:
    runtime = SimpleNamespace(
        load_chunk_schedule=AsyncMock(side_effect=[
            ChunkScheduleSnapshot((ChunkScheduleEntry("chunk", 0, ChunkTaskStatus.QUEUED),), 0),
            ChunkScheduleSnapshot((ChunkScheduleEntry("chunk", 0, ChunkTaskStatus.WAITING, phase=2, understanding_committed=True),), 0),
        ]),
        wait_for_video_change=AsyncMock(),
    )
    state = {"video_parse_task_id": "video-task"}
    assert asyncio.run(ChunkScheduler(runtime=runtime).run(state)) == state
    runtime.wait_for_video_change.assert_awaited_once_with("video-task", timeout_seconds=0)


@pytest.mark.parametrize("offset_ms", [0, 60_000], ids=["first-chunk", "second-chunk"])
def test_chunk_workflow_commits_before_notifying_and_cleans_media(offset_ms) -> None:
    sequence_no = int(offset_ms != 0)
    task_id = f"chunk-task-{sequence_no}"
    execution = ChunkExecution(
        task_id, "video-task-1", sequence_no, offset_ms, offset_ms + 20_000,
        offset_ms, offset_ms + 20_000, _inputs(ocr_enabled=True), _lease(),
    )
    asr_result = {"transcript": [{
        "start_seconds": offset_ms + 1000,
        "end_seconds": offset_ms + 3000,
        "text": "hello",
    }]}
    calls: list[str] = []

    class Runtime:
        async def load_chunk_execution(self, task_id):
            return execution

        async def prepare_chunk_visual(self, loaded):
            calls.append("visual")

        async def transcribe_chunk(self, loaded):
            calls.append("asr")
            return asr_result

        async def read_chunk_text(self, loaded):
            calls.append("ocr")
            return OCRResult.model_validate({"lines": [{
                "start_seconds": offset_ms + 1000,
                "end_seconds": offset_ms + 3000,
                "text": "screen text",
            }]})

        async def understand_chunk(self, loaded, **kwargs):
            canvas = kwargs["canvas"]
            assert build_canvas_understanding_prompt(execution, canvas=canvas) == kwargs["prompt"]
            assert kwargs["ocr_result"].lines[0].text == "screen text"
            assert kwargs["asr_result"] is asr_result
            evidence = canvas.get("evidence", task_id)
            assert evidence["ocr"]["lines"][0]["text"] == "screen text"
            assert evidence["asr"] == asr_result
            assert f"[{offset_ms}, {offset_ms + 20_000})" in kwargs["prompt"]
            assert "integer source-video milliseconds" in kwargs["prompt"]
            assert evidence["asr"]["transcript"][0]["start_seconds"] == offset_ms + 1000
            assert evidence["asr"]["transcript"][0]["end_seconds"] == offset_ms + 3000
            time_contract = kwargs["prompt"].lower()
            assert "already normalized" in time_contract
            assert "legacy" in time_contract
            assert "do not multiply or divide" in time_contract
            assert "1000" in time_contract and "offset" in time_contract
            calls.append("understand")
            canvas.write(role="understanding", batch_id="initial", expected_revision=canvas.revision, operations=[
                {"op": "persons", "member_ref": task_id, "action": "add", "values": ["爱音"]},
                {"op": "add_event", "member_ref": task_id, "key": "initial-event", "event": _result(offset_ms=offset_ms).timeline[0]},
            ])
            return canvas.result(task_id)

        async def commit_chunk_result(self, loaded, result, *, evidence):
            assert loaded is execution
            assert result == _result(offset_ms=offset_ms)
            assert result.timeline[0].start_seconds == offset_ms + 1000
            assert result.timeline[0].end_seconds == offset_ms + 3000
            assert evidence["asr"] == asr_result
            calls.append("commit")

        async def notify_chunk_committed(self, loaded):
            calls.append("notify")

        async def cleanup_chunk_execution(self, loaded):
            assert loaded.lease.version == 3
            calls.append("cleanup")

    runtime = Runtime()
    asyncio.run(ChunkUnderstandingWorkflow(runtime=runtime).run(
        task_id
    ))
    assert calls[-3:] == ["commit", "notify", "cleanup"]


@pytest.mark.parametrize("ocr_enabled", [False, True])
@pytest.mark.parametrize("offset_ms", [0, 60_000], ids=["first-chunk", "second-chunk"])
def test_chunk_workflow_builds_engine_events_without_understanding(
    ocr_enabled, offset_ms,
) -> None:
    sequence_no = int(offset_ms != 0)
    task_id = f"chunk-task-{sequence_no}"
    execution = ChunkExecution(
        task_id, "video-task-1", sequence_no, offset_ms, offset_ms + 20_000,
        offset_ms, offset_ms + 20_000,
        _inputs(understanding_enabled=False, ocr_enabled=ocr_enabled), _lease(),
    )
    runtime = SimpleNamespace(
        load_chunk_execution=AsyncMock(return_value=execution),
        prepare_chunk_visual=AsyncMock(),
        transcribe_chunk=AsyncMock(return_value={"transcript": [{
            "start_seconds": offset_ms + 1000,
            "end_seconds": offset_ms + 3000,
            "text": " hello ",
        }]}),
        read_chunk_text=AsyncMock(return_value=OCRResult.model_validate({"lines": [{
            "start_seconds": offset_ms + 500,
            "end_seconds": offset_ms + 1500,
            "text": " sign ",
        }]})),
        understand_chunk=AsyncMock(side_effect=AssertionError("must not understand")),
        commit_chunk_result=AsyncMock(),
        notify_chunk_committed=AsyncMock(),
        cleanup_chunk_execution=AsyncMock(),
    )
    asyncio.run(ChunkUnderstandingWorkflow(runtime=runtime).run(task_id))
    result = runtime.commit_chunk_result.await_args.args[1]
    assert result.persons == []
    assert [event.description for event in result.timeline] == (
        ["sign", "hello"] if ocr_enabled else ["hello"]
    )
    assert [(event.start_seconds, event.end_seconds) for event in result.timeline] == (
        [(offset_ms + 500, offset_ms + 1500), (offset_ms + 1000, offset_ms + 3000)]
        if ocr_enabled else [(offset_ms + 1000, offset_ms + 3000)]
    )
    runtime.understand_chunk.assert_not_awaited()
    assert runtime.read_chunk_text.await_count == int(ocr_enabled)
    runtime.notify_chunk_committed.assert_awaited_once_with(execution)
    runtime.cleanup_chunk_execution.assert_awaited_once_with(execution)


@pytest.mark.parametrize("failure", ["validation", "commit"])
def test_chunk_workflow_failure_never_notifies_and_always_cleans_media(failure) -> None:
    execution = ChunkExecution(
        "chunk-task-0", "video-task-1", 0, 0, 20_000, 0, 20_000,
        _inputs(understanding_enabled=False), _lease(),
    )
    runtime = SimpleNamespace(
        load_chunk_execution=AsyncMock(return_value=execution),
        prepare_chunk_visual=AsyncMock(),
        transcribe_chunk=AsyncMock(return_value={"transcript": [{
            "start_seconds": 1000,
            "end_seconds": 20_001 if failure == "validation" else 3000,
            "text": "hello",
        }]}),
        commit_chunk_result=AsyncMock(side_effect=RuntimeError("commit failed")),
        notify_chunk_committed=AsyncMock(),
        cleanup_chunk_execution=AsyncMock(),
    )
    with pytest.raises(ValueError if failure == "validation" else RuntimeError):
        asyncio.run(ChunkUnderstandingWorkflow(runtime=runtime).run("chunk-task-0"))
    assert runtime.commit_chunk_result.await_count == int(failure == "commit")
    runtime.notify_chunk_committed.assert_not_awaited()
    runtime.cleanup_chunk_execution.assert_awaited_once_with(execution)


def test_shared_chunk_understanding_prepares_asr_and_visual_concurrently() -> None:
    async def run():
        visual_started = asyncio.Event()
        asr_started = asyncio.Event()
        asr_result = {"transcript": []}
        ocr_result = OCRResult.model_validate({"lines": []})

        async def prepare_visual():
            visual_started.set()
            await asr_started.wait()

        async def transcribe():
            asr_started.set()
            await visual_started.wait()
            return asr_result

        read_text = AsyncMock(return_value=ocr_result)
        understand = AsyncMock(return_value=_result(offset_ms=60_000))
        result = await asyncio.wait_for(understand_chunk(
            start_ms=60_000,
            end_ms=80_000,
            ocr_enabled=True,
            understanding_enabled=True,
            prepare_visual=prepare_visual,
            transcribe=transcribe,
            read_text=read_text,
            understand=understand,
        ), timeout=1)
        assert result == _result(offset_ms=60_000)
        read_text.assert_awaited_once()
        understand.assert_awaited_once()
        assert understand.await_args.kwargs["asr_result"] is asr_result
        assert understand.await_args.kwargs["ocr_result"] is ocr_result

    asyncio.run(run())


@pytest.mark.parametrize("failure", ["visual", "asr", "cancel"])
def test_chunk_preparation_failure_finishes_both_branches_before_cleanup(failure) -> None:
    async def run():
        execution = ChunkExecution(
            "chunk-task-0", "video-task-1", 0, 0, 20_000, 0, 20_000,
            _inputs(), _lease(),
        )
        visual_started = asyncio.Event()
        asr_started = asyncio.Event()
        finished: set[str] = set()
        original_error = RuntimeError("preparation failed")

        async def prepare_visual(loaded):
            visual_started.set()
            try:
                await asr_started.wait()
                if failure == "visual":
                    raise original_error
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                finished.add("visual")

        async def transcribe(loaded):
            asr_started.set()
            try:
                await visual_started.wait()
                if failure == "asr":
                    raise original_error
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                finished.add("asr")

        async def cleanup(loaded):
            assert loaded is execution
            assert finished == {"visual", "asr"}

        runtime = SimpleNamespace(
            load_chunk_execution=AsyncMock(return_value=execution),
            prepare_chunk_visual=prepare_visual,
            transcribe_chunk=transcribe,
            understand_chunk=AsyncMock(),
            commit_chunk_result=AsyncMock(),
            notify_chunk_committed=AsyncMock(),
            cleanup_chunk_execution=AsyncMock(side_effect=cleanup),
        )
        task = asyncio.create_task(
            ChunkUnderstandingWorkflow(runtime=runtime).run("chunk-task-0")
        )
        await asyncio.wait_for(asyncio.gather(
            visual_started.wait(), asr_started.wait(),
        ), timeout=1)
        if failure == "cancel":
            task.cancel()
        expected_error = asyncio.CancelledError if failure == "cancel" else RuntimeError
        with pytest.raises(expected_error) as captured:
            await asyncio.wait_for(task, timeout=1)
        if failure != "cancel":
            assert captured.value is original_error
        runtime.understand_chunk.assert_not_awaited()
        runtime.commit_chunk_result.assert_not_awaited()
        runtime.notify_chunk_committed.assert_not_awaited()
        runtime.cleanup_chunk_execution.assert_awaited_once_with(execution)

    asyncio.run(run())


@pytest.mark.parametrize("offset_ms", [0, 60_000], ids=["first-chunk", "second-chunk"])
def test_refine_prompt_carries_scope_and_commit_receives_only_actual_fields(offset_ms) -> None:
    sequence_no = int(offset_ms != 0)
    task_id = f"chunk-task-{sequence_no}"
    snapshot = {
        "interval_parse_task_id": "interval-1",
        "video_parse_task_id": "video-task-1",
        "inputs": _inputs(),
        "lease": _lease(),
        "start_sequence_no": sequence_no,
        "chunk_count": 1,
        "member_sequence_nos": {task_id: sequence_no},
        "member_bounds": {task_id: (offset_ms, offset_ms + 20_000)},
        "member_contents": {task_id: _result(offset_ms=offset_ms)},
        "member_versions": {task_id: 2},
        "member_scopes": {task_id: [0, 1]},
    }
    async def refine(_snapshot, *, canvas, prompt):
        reference = canvas.items("events")[0][0]
        canvas.write(role="refine", batch_id="refine", expected_revision=canvas.revision, operations=[
            {"op": "persons", "member_ref": task_id, "action": "set", "values": ["爱音"]},
            {"op": "patch_event", "event_ref": reference, "fields": {"description": "爱音推门走进房间"}},
        ])
        return CanvasCompletion(complete=True)

    runtime = SimpleNamespace(
        load_refine_input=AsyncMock(return_value=snapshot),
        generate_interval_refinement=AsyncMock(side_effect=refine),
        commit_interval_refinement=AsyncMock(),
        notify_interval_committed=AsyncMock(),
    )
    asyncio.run(IntervalRefine(runtime=runtime).run("interval-1"))

    prompt = runtime.generate_interval_refinement.await_args.kwargs["prompt"]
    assert '"scope": [0, 1]' in prompt
    assert f'"start_ms": {offset_ms}' in prompt
    assert f'"end_ms": {offset_ms + 20_000}' in prompt
    time_contract = prompt.lower()
    assert "absolute integer source-video milliseconds" in time_contract
    assert "legacy" in time_contract
    assert "do not multiply or divide" in time_contract
    assert "1000" in time_contract and "offset" in time_contract
    assert "preserve" in time_contract and "time bounds" in time_contract
    validated = runtime.commit_interval_refinement.await_args.args[1]
    assert validated.updates[0].changed_fields == {"timeline"}
    assert validated.updates[0].timeline[0].start_seconds == offset_ms + 1000
    assert validated.updates[0].timeline[0].end_seconds == offset_ms + 3000
    assert snapshot["member_contents"][task_id] == _result(offset_ms=offset_ms)
    runtime.notify_interval_committed.assert_awaited_once_with(snapshot)


def test_prefix_is_optional_and_validates_bound_target_before_commit() -> None:
    disabled = {
        "chunk_parse_task_id": "chunk-task-0",
        "video_parse_task_id": "video-task-1",
        "inputs": _inputs(),
        "lease": _lease(),
        "sequence_no": 0,
        "allowed_start_seconds": 0,
        "allowed_end_seconds": 20_000,
        "scope": [0],
        "content": _result(),
        "content_version": 3,
        "series_prefix_compact": "",
        "video_prefix_compact": "",
    }
    runtime = SimpleNamespace(
        load_prefix_context=AsyncMock(return_value=disabled),
        generate_prefix_refinement=AsyncMock(),
    )
    asyncio.run(PrefixContextAgent(
        runtime=runtime,
    ).run("chunk-task-0"))
    runtime.generate_prefix_refinement.assert_not_awaited()

    enabled = {**disabled, "inputs": _inputs(compact_enabled=True)}
    async def prefix(_context, *, canvas, prompt):
        canvas.write(role="prefix", batch_id="prefix", expected_revision=canvas.revision, operations=[
            {"op": "persons", "member_ref": "chunk-task-0", "action": "add", "values": ["乐奈"]},
            {"op": "set_compact", "series_compact": "prior episodes", "video_compact": "prior chunks"},
        ])
        return CanvasCompletion(complete=True)

    runtime = SimpleNamespace(
        load_prefix_context=AsyncMock(return_value=enabled),
        generate_prefix_refinement=AsyncMock(side_effect=prefix),
        commit_prefix_refinement=AsyncMock(),
        notify_prefix_committed=AsyncMock(),
    )
    asyncio.run(PrefixContextAgent(
        runtime=runtime,
    ).run("chunk-task-0"))
    committed = runtime.commit_prefix_refinement.await_args.args[1]
    assert committed.update is not None
    assert committed.update.sequence_no == 0
