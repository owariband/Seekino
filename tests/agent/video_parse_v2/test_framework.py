from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from agent.video_parse_v2 import ParseAgent
from agent.video_parse_v2.listener import ChunkIntervalListener
from agent.video_parse_v2.scheduler import ChunkScheduler
from agent.video_parse_v2.understanding import ChunkUnderstandingWorkflow
from agent.video_parse_v2.input import ParseInput
from agent.video_parse_v2.prefix import PrefixContextAgent
from agent.video_parse_v2.refine import IntervalRefine
from agent.video_parse_v2.host import (
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
from engine.parsev2.validation import (
    ChunkResultUpdate,
    IntervalRefinementProposal,
    PrefixRefinementProposal,
)


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
            "execution": {
                "max_concurrent_model_calls": 8, "max_retry_count": 0,
                "retry_initial_delay_seconds": 1, "retry_max_delay_seconds": 30,
                "max_model_calls": 100,
            },
        },
    })


def _result(description: str = "爱音走进房间") -> VideoParseResult:
    return VideoParseResult.model_validate({
        "persons": ["爱音"],
        "timeline": [{
            "event_type": "ACTION",
            "start_seconds": 1,
            "end_seconds": 3,
            "description": description,
            "participants": ["爱音"],
            "visible_persons": ["爱音"],
        }],
    })


def test_default_entry_stops_at_unconfigured_runtime_boundary() -> None:
    with pytest.raises(NotImplementedError, match="load_video_run"):
        asyncio.run(ParseAgent().run("video-task-1"))


def test_main_graph_prepares_fixed_topology_then_completes() -> None:
    calls: list[object] = []
    chunks = (
        VideoChunk("chunk-0", 0, 0, 20),
        VideoChunk("chunk-1", 1, 20, 35),
    )

    class Runtime:
        async def load_video_run(self, task_id):
            calls.append("load")
            return VideoRunSnapshot(
                task_id, VideoTaskStatus.PREPARING, _inputs(), 35, _lease(), ()
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
                        f"task-{index}", index, ChunkTaskStatus.COMPLETE
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
            "video-task-1", VideoTaskStatus.INDEXING, _inputs(), 20, _lease(),
            (VideoChunk("chunk-0", 0, 0, 20),),
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
    runtime = SimpleNamespace(dispatch_chunk=AsyncMock(return_value=True))
    scheduler = ChunkScheduler(runtime=runtime)
    state = {
        "video_parse_task_id": "video-task-1",
        "inputs": _inputs(),
        "lease": _lease(),
    }

    first_eight = tuple(
        ChunkScheduleEntry(
            f"task-{index}",
            index,
            ChunkTaskStatus.COMPLETE if index < 8 else ChunkTaskStatus.QUEUED,
            dispatchable=True,
        )
        for index in range(20)
    )
    asyncio.run(scheduler._dispatch_ready_chunks(
        state,
        ChunkScheduleSnapshot(first_eight, wait_timeout_seconds=1),
    ))
    assert all(
        call.args[0] in {"task-8", "task-9"}
        for call in runtime.dispatch_chunk.await_args_list
    )
    assert all(call.args[1].version == 3 for call in runtime.dispatch_chunk.await_args_list)

    runtime.dispatch_chunk.reset_mock()
    first_nine = tuple(
        ChunkScheduleEntry(
            f"task-{index}",
            index,
            ChunkTaskStatus.COMPLETE if index < 9 else ChunkTaskStatus.QUEUED,
            dispatchable=True,
        )
        for index in range(20)
    )
    asyncio.run(scheduler._dispatch_ready_chunks(
        state,
        ChunkScheduleSnapshot(first_nine, wait_timeout_seconds=1),
    ))
    assert "task-10" in [call.args[0] for call in runtime.dispatch_chunk.await_args_list]


def test_chunk_workflow_commits_mask_in_transaction_and_cleans_media() -> None:
    execution = ChunkExecution(
        "chunk-task-0", "video-task-1", 0, 0, 20, 0, 20, _inputs(ocr_enabled=True), _lease()
    )
    calls: list[str] = []

    class Runtime:
        async def load_chunk_execution(self, task_id):
            return execution

        async def prepare_chunk_visual(self, loaded):
            calls.append("visual")

        async def transcribe_chunk(self, loaded):
            calls.append("asr")
            return {"text": "transcript"}

        async def read_chunk_text(self, loaded):
            calls.append("ocr")
            return ["screen text"]

        async def understand_chunk(self, loaded, **kwargs):
            assert kwargs["ocr_result"] == ["screen text"]
            assert "[0, 20)" in kwargs["prompt"]
            calls.append("understand")
            return _result()

        async def commit_chunk_result(self, loaded, result, update_mask):
            calls.append("commit-start")
            update_mask(loaded.chunk_parse_task_id)
            calls.append("commit-end")

        def update_interval_mask(self, task_id):
            assert calls[-1] == "commit-start"
            calls.append("mask")

        async def notify_chunk_committed(self, loaded):
            calls.append("notify")

        async def cleanup_chunk_execution(self, loaded):
            assert loaded.lease.version == 3
            calls.append("cleanup")

    runtime = Runtime()
    listener = ChunkIntervalListener(runtime=runtime)
    asyncio.run(ChunkUnderstandingWorkflow(runtime=runtime, listener=listener).run(
        "chunk-task-0"
    ))
    assert calls[-5:] == ["commit-start", "mask", "commit-end", "notify", "cleanup"]


def test_listener_rechecks_durable_readiness_before_queueing() -> None:
    runtime = SimpleNamespace(
        interval_is_ready=AsyncMock(side_effect=[False, True]),
        enqueue_interval_refine=AsyncMock(),
        next_prefix_chunk=AsyncMock(side_effect=[None, "chunk-task-0"]),
        enqueue_prefix_context=AsyncMock(),
    )
    listener = ChunkIntervalListener(runtime=runtime)

    async def exercise() -> None:
        await listener.run_interval_refine_if_ready("interval-1")
        await listener.run_prefix_context_if_ready("series-1")
        await listener.run_interval_refine_if_ready("interval-1")
        await listener.run_prefix_context_if_ready("series-1")

    asyncio.run(exercise())
    runtime.enqueue_interval_refine.assert_awaited_once_with("interval-1")
    runtime.enqueue_prefix_context.assert_awaited_once_with("chunk-task-0")


def test_refine_prompt_carries_scope_and_commit_receives_only_actual_fields() -> None:
    snapshot = {
        "interval_parse_task_id": "interval-1",
        "video_parse_task_id": "video-task-1",
        "inputs": _inputs(),
        "lease": _lease(),
        "start_sequence_no": 0,
        "chunk_count": 1,
        "member_sequence_nos": {"chunk-task-0": 0},
        "member_bounds": {"chunk-task-0": (0, 20)},
        "member_contents": {"chunk-task-0": _result()},
        "member_versions": {"chunk-task-0": 2},
        "member_scopes": {"chunk-task-0": [0, 1]},
    }
    runtime = SimpleNamespace(
        load_refine_input=AsyncMock(return_value=snapshot),
        generate_interval_refinement=AsyncMock(return_value=IntervalRefinementProposal(
            updates=[ChunkResultUpdate(
                sequence_no=0,
                persons=["爱音"],
                timeline=_result("爱音推门走进房间").timeline,
            )]
        )),
        commit_interval_refinement=AsyncMock(),
        notify_interval_committed=AsyncMock(),
        next_prefix_chunk=AsyncMock(return_value=None),
    )
    listener = ChunkIntervalListener(runtime=runtime)
    asyncio.run(IntervalRefine(runtime=runtime, listener=listener).run("interval-1"))

    prompt = runtime.generate_interval_refinement.await_args.kwargs["prompt"]
    assert '"scope": [0, 1]' in prompt
    validated = runtime.commit_interval_refinement.await_args.args[1]
    assert validated.updates[0].changed_fields == {"timeline"}
    runtime.notify_interval_committed.assert_awaited_once_with(snapshot)


def test_prefix_is_optional_and_validates_bound_target_before_commit() -> None:
    disabled = {
        "chunk_parse_task_id": "chunk-task-0",
        "video_parse_task_id": "video-task-1",
        "inputs": _inputs(),
        "lease": _lease(),
        "sequence_no": 0,
        "allowed_start_seconds": 0,
        "allowed_end_seconds": 20,
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
        listener=ChunkIntervalListener(runtime=runtime),
    ).run("chunk-task-0"))
    runtime.generate_prefix_refinement.assert_not_awaited()

    enabled = {**disabled, "inputs": _inputs(compact_enabled=True)}
    runtime = SimpleNamespace(
        load_prefix_context=AsyncMock(return_value=enabled),
        generate_prefix_refinement=AsyncMock(return_value=PrefixRefinementProposal(
            series_compact="prior episodes",
            video_compact="prior chunks",
            update=ChunkResultUpdate(sequence_no=0, persons=["爱音", "乐奈"]),
        )),
        commit_prefix_refinement=AsyncMock(),
        notify_prefix_committed=AsyncMock(),
        next_prefix_chunk=AsyncMock(return_value=None),
    )
    asyncio.run(PrefixContextAgent(
        runtime=runtime,
        listener=ChunkIntervalListener(runtime=runtime),
    ).run("chunk-task-0"))
    committed = runtime.commit_prefix_refinement.await_args.args[1]
    assert committed.update is not None
    assert committed.update.sequence_no == 0
