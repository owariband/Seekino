from __future__ import annotations

import asyncio
from dataclasses import replace
import json
from pathlib import Path
from threading import Event
from time import monotonic
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent.chat.common.errors import AgentContextBudgetError, AgentModelInvocationError
from agent.chat.realtime_understanding.runtime import (
    RealtimeUnderstandingRuntime, select_chunk_window,
)
from agent.chat.realtime_understanding.state import (
    ChunkEvidence, ChunkTarget, ContextSummary, FrameEvidence, RealtimeContext,
    RealtimeRequest, RealtimeResult, VisualEvidence,
)
from engine.parsev2.schemas import VideoParseResult


def _targets(video_id="video", *, duration_ms=260_000):
    return tuple(
        ChunkTarget(
            f"{video_id}-chunk-{index}", f"{video_id}-task-{index}", index,
            start_ms, min(start_ms + 60_000, duration_ms),
        )
        for index, start_ms in enumerate(range(0, duration_ms, 60_000))
    )


def _request(*, video_id="video", anchor_ms=90_000, question="老师刚才讲的概念是什么？"):
    return RealtimeRequest(
        tenant_id="workspace", video_id=video_id, anchor_ms=anchor_ms,
        question=question, deadline=monotonic() + 10, check_control=lambda: None,
    )


def _context(request):
    return RealtimeContext(
        request=request, series_id="series",
        video_parse_task_id=f"{request.video_id}-parse", task_version=2,
        duration_ms=260_000,
        targets=select_chunk_window(
            _targets(request.video_id), request.anchor_ms, duration_ms=260_000,
        ),
        source_uri=f"artifact://{request.video_id}.mp4",
    )


def _evidence(target):
    return ChunkEvidence(
        target=target,
        result=VideoParseResult.model_validate({
            "persons": [],
            "timeline": [{
                "event_type": "ACTION",
                "start_seconds": target.start_ms + 1000,
                "end_seconds": target.start_ms + 2000,
                "description": f"lecture evidence {target.chunk_id}",
                "participants": [], "visible_persons": [],
            }],
        }),
        content_version=3,
    )


def _host():
    return SimpleNamespace(
        prepare_context=AsyncMock(side_effect=_context),
        ensure_chunk=AsyncMock(side_effect=lambda context, target: _evidence(target)),
        observe_frames=AsyncMock(return_value=VisualEvidence()),
        compose_context=AsyncMock(return_value=ContextSummary(summary="整理后的课堂上下文。")),
        refine=AsyncMock(side_effect=AssertionError("must not refine")),
        index=AsyncMock(side_effect=AssertionError("must not index")),
    )


@pytest.mark.parametrize("anchor_ms,sequences", [
    (0, (0, 1)),
    (59_999, (0, 1)),
    (60_000, (0, 1, 2)),
    (120_000, (1, 2, 3)),
    (259_999, (3, 4)),
    (260_000, (3, 4)),
])
def test_fixed_chunk_window_uses_half_open_bounds_and_clips_video_ends(anchor_ms, sequences):
    selected = select_chunk_window(
        tuple(reversed(_targets())), anchor_ms, duration_ms=260_000,
    )
    assert tuple(target.sequence_no for target in selected) == sequences


@pytest.mark.parametrize("duration_ms", [20_000, 80_000])
def test_short_video_reuses_all_available_chunks_at_last_playback_endpoint(duration_ms):
    chunks = _targets(duration_ms=duration_ms)
    assert select_chunk_window(chunks, duration_ms, duration_ms=duration_ms) == chunks


@pytest.mark.parametrize("anchor_ms", [-1, 260_001, True, 60_000.5])
def test_invalid_playback_position_does_not_choose_a_chunk(anchor_ms):
    with pytest.raises(ValueError, match="outside"):
        select_chunk_window(_targets(), anchor_ms, duration_ms=260_000)


def test_chunk_and_vision_branches_run_concurrently_and_compose_joins_once():
    async def run():
        host = _host()
        chunks_started = asyncio.Event()
        chunks_finished = asyncio.Event()
        visual_started = asyncio.Event()
        release_chunks = asyncio.Event()
        release_visual = asyncio.Event()
        started = finished = 0

        async def ensure(context, target):
            nonlocal started, finished
            started += 1
            if started == len(context.targets):
                chunks_started.set()
            await release_chunks.wait()
            finished += 1
            if finished == len(context.targets):
                chunks_finished.set()
            return _evidence(target)

        async def observe(context, *, prompt):
            visual_started.set()
            await release_visual.wait()
            return VisualEvidence(summary="暂停画面显示 λ 和分母中的长度。")

        host.ensure_chunk.side_effect = ensure
        host.observe_frames.side_effect = observe
        runtime = RealtimeUnderstandingRuntime(host=host)
        task = asyncio.create_task(runtime.run(_request()))
        await asyncio.wait_for(asyncio.gather(
            chunks_started.wait(), visual_started.wait(),
        ), timeout=1)
        host.compose_context.assert_not_awaited()
        release_chunks.set()
        await asyncio.wait_for(chunks_finished.wait(), timeout=1)
        host.compose_context.assert_not_awaited()
        release_visual.set()
        result = await asyncio.wait_for(task, timeout=1)
        host.compose_context.assert_awaited_once()
        prompt = host.compose_context.await_args.kwargs["prompt"]
        facts = json.loads(prompt.split("Evidence: ", 1)[1])
        assert facts["visual_summary"] == result.visual.summary
        assert {item["chunk_id"] for item in facts["chunks"]} == {
            item.target.chunk_id for item in result.chunks
        }
        assert all(item["content"]["timeline"] for item in facts["chunks"])
        host.refine.assert_not_awaited()
        host.index.assert_not_awaited()

    asyncio.run(run())


def test_visual_no_op_still_returns_and_completes_the_join():
    host = _host()
    result = asyncio.run(RealtimeUnderstandingRuntime(host=host).run(_request()))
    host.observe_frames.assert_awaited_once()
    host.compose_context.assert_awaited_once()
    assert result.visual == VisualEvidence()
    assert result.as_dict()["partial"] is False


def test_chunk_work_is_concurrent_within_the_configured_bound():
    async def run():
        host = _host()
        first_batch_started = asyncio.Event()
        release_batch = asyncio.Event()
        active = maximum_active = started = 0

        async def ensure(context, target):
            nonlocal active, maximum_active, started
            active += 1
            started += 1
            maximum_active = max(maximum_active, active)
            if active == 2:
                first_batch_started.set()
            try:
                await release_batch.wait()
                return _evidence(target)
            finally:
                active -= 1

        host.ensure_chunk.side_effect = ensure
        runtime = RealtimeUnderstandingRuntime(host=host, maximum_concurrent_chunks=2)
        task = asyncio.create_task(runtime.run(_request()))
        await asyncio.wait_for(first_batch_started.wait(), timeout=1)
        assert started == active == 2
        release_batch.set()
        result = await asyncio.wait_for(task, timeout=1)
        assert maximum_active == 2
        assert len(result.chunks) == started == 3

    asyncio.run(run())


@pytest.mark.parametrize("failed_path", ["chunk", "visual", "both"])
def test_partial_failure_preserves_missing_coverage_and_available_evidence(failed_path):
    host = _host()

    async def ensure(context, target):
        if failed_path in {"chunk", "both"} and target.sequence_no == 1:
            raise RuntimeError("one chunk is unavailable")
        return _evidence(target)

    host.ensure_chunk.side_effect = ensure
    if failed_path in {"visual", "both"}:
        host.observe_frames.side_effect = RuntimeError("frames are unavailable")
    result = asyncio.run(RealtimeUnderstandingRuntime(host=host).run(_request()))
    payload = result.as_dict()
    assert payload["partial"] is True
    assert [item["available"] for item in payload["chunks"]] == (
        [True, False, True] if failed_path in {"chunk", "both"} else [True] * 3
    )
    if failed_path in {"chunk", "both"}:
        assert payload["chunks"][1]["error"] == "RuntimeError"
        assert payload["chunks"][1]["evidence"] == []
    assert payload["visual_error"] == (
        "RuntimeError" if failed_path in {"visual", "both"} else None
    )
    host.compose_context.assert_awaited_once()
    host.refine.assert_not_awaited()
    host.index.assert_not_awaited()


@pytest.mark.parametrize("unavailable", ["failed", "empty"])
def test_no_facts_does_not_generate_a_model_summary(unavailable):
    host = _host()
    if unavailable == "failed":
        host.ensure_chunk.side_effect = RuntimeError("no chunk evidence")
    else:
        host.ensure_chunk.side_effect = lambda context, target: ChunkEvidence(
            target=target, result=VideoParseResult(persons=[], timeline=[]),
            content_version=3,
        )
    result = asyncio.run(RealtimeUnderstandingRuntime(host=host).run(_request()))
    host.compose_context.assert_not_awaited()
    assert "无法核验" in result.summary
    assert all(not item.result or not item.result.timeline for item in result.chunks)


def test_frame_caption_is_usable_evidence_without_chunk_results_or_visual_summary():
    host = _host()
    frame = FrameEvidence(
        timestamp_ms=90_000, chunk_id="video-chunk-1",
        artifact_uri="artifact://video/paused-frame.png",
        image_path=Path("/tmp/paused-frame.png"), caption="公式中 λ 位于分子。",
    )
    host.ensure_chunk.side_effect = RuntimeError("chunk evidence unavailable")
    host.observe_frames.return_value = VisualEvidence(frames=(frame,))
    result = asyncio.run(RealtimeUnderstandingRuntime(host=host).run(_request()))
    host.compose_context.assert_awaited_once()
    prompt = host.compose_context.await_args.kwargs["prompt"]
    facts = json.loads(prompt.split("Evidence: ", 1)[1])
    assert facts["frames"] == [{
        "timestamp_ms": 90_000, "frame_ref": frame.artifact_uri, "caption": frame.caption,
    }]
    assert result.as_dict()["frames"][0]["frame_ref"] == frame.artifact_uri
    assert result.as_dict()["partial"] is True


def test_context_overflow_propagates_without_calling_the_summary_model():
    host = _host()
    host.observe_frames.return_value = VisualEvidence(summary="可见事实。" * 1000)
    with pytest.raises(AgentContextBudgetError, match="context budget"):
        asyncio.run(RealtimeUnderstandingRuntime(
            host=host, max_prompt_chars=1024,
        ).run(_request()))
    host.compose_context.assert_not_awaited()


def test_selected_evidence_keeps_the_active_event_and_marks_future_context():
    context = _context(_request(anchor_ms=90_000))
    target = context.targets[1]
    evidence = _evidence(target)
    active = evidence.result.timeline[0].model_copy(update={
        "start_seconds": 60_000, "end_seconds": 100_000,
        "description": "active lecture explanation",
    })
    future = active.model_copy(update={
        "start_seconds": 91_000, "end_seconds": 92_000,
        "description": "later example",
    })
    parsed = evidence.result.model_copy(update={"timeline": [active, future]})
    result = RealtimeResult(
        context, (replace(evidence, result=parsed),), VisualEvidence(), "context",
    )
    selected = result.as_dict(events_per_chunk=1)["chunks"][0]["evidence"]
    assert selected[0]["description"] == "active lecture explanation"
    assert selected[0]["after_anchor"] is False
    all_events = result.as_dict(events_per_chunk=2)["chunks"][0]["evidence"]
    assert all_events[1]["after_anchor"] is True
    assert "after_anchor" not in parsed.timeline[1].model_dump()


def test_one_runtime_keeps_concurrent_request_state_isolated():
    async def run():
        host = _host()
        both_started = asyncio.Event()
        seen_videos: set[str] = set()

        async def observe(context, *, prompt):
            video_id = context.request.video_id
            seen_videos.add(video_id)
            if len(seen_videos) == 2:
                both_started.set()
            await both_started.wait()
            return VisualEvidence(summary=f"observations for {video_id}")

        async def compose(context, *, prompt):
            facts = json.loads(prompt.split("Evidence: ", 1)[1])
            assert facts["question"] == context.request.question
            assert facts["visual_summary"] == f"observations for {context.request.video_id}"
            assert all(
                item["chunk_id"].startswith(context.request.video_id + "-")
                for item in facts["chunks"]
            )
            return ContextSummary(summary=context.request.question)

        host.observe_frames.side_effect = observe
        host.compose_context.side_effect = compose
        runtime = RealtimeUnderstandingRuntime(host=host)
        requests = (
            _request(video_id="video-a", anchor_ms=60_000, question="第一个视频的问题"),
            _request(video_id="video-b", anchor_ms=260_000, question="第二个视频的问题"),
        )
        results = await asyncio.wait_for(asyncio.gather(
            *(runtime.run(request) for request in requests),
        ), timeout=1)
        assert [result.summary for result in results] == [r.question for r in requests]
        assert [result.context.request.anchor_ms for result in results] == [60_000, 260_000]
        assert [len(result.chunks) for result in results] == [3, 2]
        assert host.compose_context.await_count == 2

    asyncio.run(run())


def _inputs():
    return {
        "tenant_id": "workspace", "user_message": "解释当前公式，再关联刚才的定义。",
        "extra_context": {"playback": {"videoId": "video", "positionSeconds": 90_123}},
    }


def test_kick_freezes_milliseconds_and_the_complete_user_question():
    host = _host()
    inputs = _inputs()

    async def prepare(request):
        inputs["extra_context"]["playback"].update(videoId="new-video", positionSeconds=1)
        inputs["user_message"] = "a different question"
        return _context(request)

    host.prepare_context.side_effect = prepare
    result = RealtimeUnderstandingRuntime(host=host).kick(inputs)
    assert result.context.request.video_id == "video"
    assert result.context.request.anchor_ms == result.as_dict()["anchor_ms"] == 90_123
    assert result.context.request.question == "解释当前公式，再关联刚才的定义。"
    assert result.context.targets[1].sequence_no == 1


def test_pre_cancelled_kick_does_not_start_evidence_work():
    host = _host()
    cancellation = Event()
    cancellation.set()
    with pytest.raises(AgentModelInvocationError, match="cancelled"):
        RealtimeUnderstandingRuntime(host=host).kick(_inputs(), cancellation=cancellation)
    host.prepare_context.assert_not_awaited()
    host.compose_context.assert_not_awaited()


def test_expired_zero_deadline_does_not_start_evidence_work():
    host = _host()
    with pytest.raises(AgentModelInvocationError, match="deadline"):
        RealtimeUnderstandingRuntime(host=host).kick(_inputs(), deadline=0)
    host.prepare_context.assert_not_awaited()


@pytest.mark.parametrize("failed_path", ["chunk", "visual"])
@pytest.mark.parametrize("reason", ["cancelled", "deadline exceeded"])
def test_control_failure_is_not_reported_as_partial_evidence(failed_path, reason):
    host = _host()
    stopped = Event()

    def check():
        if stopped.is_set():
            raise AgentModelInvocationError(f"Agent execution {reason}")

    async def fail(*args, **kwargs):
        stopped.set()
        raise RuntimeError("dependency failed after control stopped")

    if failed_path == "chunk":
        host.ensure_chunk.side_effect = fail
    else:
        host.observe_frames.side_effect = fail
    request = replace(_request(), check_control=check)
    with pytest.raises(AgentModelInvocationError, match=reason):
        asyncio.run(RealtimeUnderstandingRuntime(host=host).run(request))
    host.compose_context.assert_not_awaited()


@pytest.mark.parametrize("stop", ["cancel", "timeout"])
def test_pending_branches_stop_without_composing_when_execution_ends(stop):
    async def run():
        host = _host()
        chunk_started = asyncio.Event()
        visual_started = asyncio.Event()
        stopped: set[str] = set()

        async def ensure(context, target):
            chunk_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.add(target.chunk_id)

        async def observe(context, *, prompt):
            visual_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.add("visual")

        host.ensure_chunk.side_effect = ensure
        host.observe_frames.side_effect = observe
        request = _request()
        if stop == "timeout":
            request = replace(request, deadline=monotonic() + 0.1)
        task = asyncio.create_task(RealtimeUnderstandingRuntime(host=host).run(request))
        await asyncio.wait_for(asyncio.gather(
            chunk_started.wait(), visual_started.wait(),
        ), timeout=1)
        if stop == "cancel":
            task.cancel()
        with pytest.raises(asyncio.CancelledError if stop == "cancel" else TimeoutError):
            await asyncio.wait_for(task, timeout=1)
        assert stopped == {"visual", *(target.chunk_id for target in _context(request).targets)}
        host.compose_context.assert_not_awaited()

    asyncio.run(run())
