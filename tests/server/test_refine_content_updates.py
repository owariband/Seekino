from __future__ import annotations

import asyncio
from contextvars import ContextVar
from threading import Event

import pytest

from agent.video_parse_v2.refine.input import RefineInputChanged
from gateway.usage import ModelCallCancelledError, track_model_call
from server.service.video_parse import VideoParseRuntimeService
from server.service.video_parse_refine_updates import RefineContentUpdates, RefineSubscription
from server.utils.execution import BlockingCallCancelled, ExecutionCalls, execution_calls
from tests.agent.video_parse_v2.test_context_canvas import _voice_snapshot
from tests.server.test_video_parse_model_calls import _Repository, _manager


def test_subscription_filters_coalesces_and_handles_snapshot_read_race():
    async def run():
        bus = RefineContentUpdates()
        with bus.subscribe("video", "interval", ("a", "b")) as sub:
            bus.publish("other-video", "other", {"a": 9})
            bus.publish("video", "interval", {"a": 9})
            bus.publish("video", "other", {"z": 9})
            bus.publish("video", "other", {})
            assert not sub.changes_since({"a": 1, "b": 1})
            bus.publish("video", "other", {"a": 2})
            assert sub.changes_since({"a": 1}) == {"a": 2}
            waiter = asyncio.create_task(sub.wait({"a": 2, "b": 1}))
            await asyncio.sleep(0)
            assert not waiter.done()  # Snapshot loading already saw this update.
            bus.publish("video", "other", {"a": 2})
            await asyncio.sleep(0)
            assert not waiter.done()
            bus.publish("video", "other", {"a": 3, "b": 2})
            bus.publish("video", "other", {"a": 4})
            bus.publish("video", "other", {"a": 3})
            assert await asyncio.wait_for(waiter, 1) == {"a": 4, "b": 2}
        bus.publish("video", "other", {"a": 5})
        assert sub.changes_since({"a": 4, "b": 2}) == {}
    asyncio.run(run())


def _runtime():
    snapshot = _voice_snapshot()
    runtime = object.__new__(VideoParseRuntimeService)
    repo = _Repository()
    manager = _manager(repo)
    sub = RefineSubscription(snapshot["video_parse_task_id"], snapshot["interval_parse_task_id"], ("member",))
    runtime._model_manager = ContextVar("test-refine-manager", default=manager)
    runtime._refine_subscription = ContextVar("test-refine-subscription", default=sub)
    return runtime, snapshot, sub, manager, repo


async def _until(event):
    async with asyncio.timeout(2):
        while not event.is_set():
            await asyncio.sleep(.001)


def test_update_before_admission_does_not_start_a_provider_request():
    async def run():
        runtime, snapshot, sub, manager, repo = _runtime()
        sub.publish({"member": snapshot["member_versions"]["member"] + 1})
        with pytest.raises(RefineInputChanged):
            await runtime.generate_interval_refinement(snapshot, prompt="test", remaining_seconds=1)
        assert not repo.reserved
        manager.raise_if_cancelled()
    asyncio.run(run())


def test_superseded_request_drains_before_refresh_and_execution_stays_usable():
    async def run():
        runtime, snapshot, sub, manager, repo = _runtime()
        entered, closed, cleanup = Event(), Event(), Event()
        calls = []

        def provider():
            with track_model_call(provider="fixture", model="refine", operation="generate") as call:
                calls.append(call)
                if len(calls) == 1:
                    call.add_cancel_callback(closed.set)
                    entered.set()
                    assert cleanup.wait(3)
                return "complete candidate"

        async def generate(*args, **kwargs):
            return await runtime._run_blocking(provider)

        runtime._generate_text_structured = generate
        request = asyncio.create_task(runtime.generate_interval_refinement(
            snapshot, prompt="old", remaining_seconds=2,
        ))
        try:
            await _until(entered)
            sub.publish({"member": snapshot["member_versions"]["member"] + 1})
            await _until(closed)
            assert not request.done() and len(repo.reserved) == 1 and not repo.released
            manager.raise_if_cancelled()
            cleanup.set()
            with pytest.raises(RefineInputChanged):
                await request
            assert len(repo.released) == 1
            snapshot["member_versions"]["member"] += 1
            assert await runtime.generate_interval_refinement(
                snapshot, prompt="latest", remaining_seconds=2,
            ) == "complete candidate"
            assert len(repo.reserved) == len(repo.released) == 2
            assert calls[0].cancelled and not calls[1].cancelled
        finally:
            cleanup.set()
            await asyncio.gather(request, return_exceptions=True)
    asyncio.run(run())


def test_complete_response_wins_simultaneous_update_for_transactional_merge():
    async def run():
        runtime, snapshot, sub, _, _ = _runtime()

        async def generate(*args, **kwargs):
            sub.publish({"member": snapshot["member_versions"]["member"] + 1})
            return "already complete candidate"

        runtime._generate_text_structured = generate
        assert await runtime.generate_interval_refinement(
            snapshot, prompt="test", remaining_seconds=1,
        ) == "already complete candidate"
    asyncio.run(run())


@pytest.mark.parametrize("termination", ["deadline", "cancel"])
def test_uncooperative_provider_retains_ownership_after_deadline_or_cancel(termination):
    async def run():
        runtime, snapshot, sub, manager, repo = _runtime()
        entered, cleanup = Event(), Event()
        calls = ExecutionCalls()
        token = execution_calls.set(calls)

        def provider():
            with track_model_call(provider="fixture", model="refine", operation="generate"):
                entered.set()
                assert cleanup.wait(3)  # Intentionally no transport-close callback.
                return "late response"

        async def generate(*args, **kwargs):
            return await runtime._run_blocking(provider)

        runtime._generate_text_structured = generate
        request = asyncio.create_task(runtime.generate_interval_refinement(
            snapshot, prompt="test", remaining_seconds=.15 if termination == "deadline" else 2,
        ))
        try:
            await _until(entered)
            sub.publish({"member": snapshot["member_versions"]["member"] + 1})
            if termination == "cancel":
                await asyncio.sleep(.02)
                request.cancel()
            with pytest.raises(BlockingCallCancelled) as caught:
                await request
            assert calls.pending and not caught.value.completion.done()
            assert len(repo.reserved) == 1 and not repo.released
            with pytest.raises(ModelCallCancelledError):
                manager.raise_if_cancelled()
            cleanup.set()
            await asyncio.wait_for(asyncio.wrap_future(caught.value.completion), 2)
            assert not calls.pending and len(repo.released) == 1
        finally:
            cleanup.set()
            execution_calls.reset(token)
            await asyncio.gather(request, return_exceptions=True)
    asyncio.run(run())


def test_execution_cancel_is_not_misclassified_as_content_refresh():
    async def run():
        runtime, snapshot, sub, manager, _ = _runtime()
        entered, closed = Event(), Event()

        def provider():
            with track_model_call(provider="fixture", model="refine", operation="generate") as call:
                call.add_cancel_callback(closed.set)
                entered.set()
                assert closed.wait(3)

        async def generate(*args, **kwargs):
            return await runtime._run_blocking(provider)

        runtime._generate_text_structured = generate
        request = asyncio.create_task(runtime.generate_interval_refinement(
            snapshot, prompt="test", remaining_seconds=2,
        ))
        await _until(entered)
        manager.cancel()
        sub.publish({"member": snapshot["member_versions"]["member"] + 1})
        with pytest.raises(ModelCallCancelledError):
            await request
    asyncio.run(run())


@pytest.mark.parametrize("outcome", ["changed", "noop", "conflict", "rollback"])
def test_runtime_notifies_only_after_successful_commit(outcome):
    from types import SimpleNamespace
    from agent.video_parse_v2.timelineEvent_canvas import ParseContextCanvas
    from engine.parsev2.schemas import VideoParseResult
    from engine.parsev2.validation import parse_content_fingerprint
    from server.model.dto.parseContentDTO import IntervalRefinementCommit
    from server.service.video_parse_errors import VideoParseSnapshotConflictError

    async def run():
        runtime, snapshot, _, _, _ = _runtime()
        runtime._refine_updates = RefineContentUpdates()
        snapshot["member_contents"] = {key: VideoParseResult.model_validate(value)
                                       for key, value in snapshot["member_contents"].items()}
        snapshot["member_fingerprints"] = {key: parse_content_fingerprint(value.model_dump(mode="json"))
                                           for key, value in snapshot["member_contents"].items()}
        runtime._require_interval_token = lambda *args, **kwargs: SimpleNamespace(lease_version=1)
        entered, finish = Event(), Event()

        def commit(*args, **kwargs):
            entered.set()
            assert finish.wait(3)
            if outcome == "conflict":
                raise VideoParseSnapshotConflictError("conflict", reason="timeline_structure_changed")
            if outcome == "rollback":
                raise RuntimeError("transaction rolled back")
            return IntervalRefinementCommit((), {"member": 2} if outcome == "changed" else {})

        runtime._persistence = SimpleNamespace(commit_interval_refinement=commit)
        with runtime._refine_updates.subscribe(snapshot["video_parse_task_id"], "neighbor", ("member",)) as sub:
            task = asyncio.create_task(runtime.commit_interval_refinement(
                snapshot, ParseContextCanvas.from_refine(snapshot).interval_result(),
            ))
            try:
                await _until(entered)
                assert sub.changes_since({"member": 1}) == {}
                finish.set()
                if outcome in {"conflict", "rollback"}:
                    with pytest.raises(RefineInputChanged if outcome == "conflict" else RuntimeError):
                        await task
                else:
                    assert await task
                assert sub.changes_since({"member": 1}) == ({"member": 2} if outcome == "changed" else {})
            finally:
                finish.set()
                await asyncio.gather(task, return_exceptions=True)
    asyncio.run(run())


def test_worker_subscribes_before_snapshot_reads_and_unsubscribes_on_failure():
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    async def run():
        runtime, snapshot, _, _, _ = _runtime()
        runtime._refine_updates = RefineContentUpdates()
        runtime._interval_lease = ContextVar("test-interval-lease", default=None)
        runtime._refine_subscription = ContextVar("test-subscription", default=None)
        observed = []

        def load(task_id):
            observed.append(runtime._refine_subscription.get())
            return object()

        @asynccontextmanager
        async def stage(*args, **kwargs):
            yield

        async def invoke(interval_id):
            runtime._refine_updates.publish("video", "neighbor", {"member": 2})
            assert observed[0].changes_since({"member": 1}) == {"member": 2}
            raise RuntimeError("agent failed")

        runtime._persistence = SimpleNamespace(load_task_input=load)
        runtime._model_stage = stage
        runtime.agent = SimpleNamespace(interval_refine=SimpleNamespace(InvokeRefine=invoke))
        lease = SimpleNamespace(video_parse_task_id="video", interval_parse_task_id="interval",
                                chunk_parse_task_ids=("member",))
        with pytest.raises(RuntimeError, match="agent failed"):
            await runtime.refine(lease)
        assert runtime._refine_subscription.get() is None and runtime._interval_lease.get() is None
        runtime._refine_updates.publish("video", "neighbor", {"member": 3})
        assert observed[0].changes_since({"member": 2}) == {}
    asyncio.run(run())
