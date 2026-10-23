from __future__ import annotations

from datetime import timedelta
from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

import pytest

from gateway.ark.embedding import DoubaoMultimodalEmbedder
from gateway.usage import ModelCallCancelledError, ModelCallTracker
from server.model.dto.jobDTO import ModelCallReservation, VideoParseLease
from server.service.video_parse_model_calls import (
    ModelCallAdmissionError,
    VideoParseModelCallManager,
)
from server.utils.time import now_utc


def test_summary_model_calls_continue_after_parse_completion_and_preserve_runtime(repository, monkeypatch):
    from server.constants.parse import RUNTIME_DETAIL_KEY
    from server.model.job import VideoParseTask
    from tests.server.test_video_summarize_persistence import AT, _claim, _ready
    from tests.server.test_video_parse_repository import _finish_ready_indexes

    factory, queue, persistence, task_id, video_lease = _ready(repository)
    _finish_ready_indexes(queue, persistence, now=AT)
    queue.release_execution(video_lease, now=AT)
    with factory.begin() as session:
        task = session.get(VideoParseTask, task_id)
        assert task.status == "COMPLETE"
        config = deepcopy(task.parse_config)
        config["execution"]["max_model_calls"] = 1
        task.parse_config = config
        detail = deepcopy(task.detail)
        detail[RUNTIME_DETAIL_KEY]["model_calls"]["used_call_ids"] = ["understanding", "refine"]
        detail[RUNTIME_DETAIL_KEY]["other_phase"] = {"value": "before"}
        task.detail = detail
    lease = _claim(persistence)
    monkeypatch.setattr("server.service.video_parse_persistence.now_utc", lambda: AT)
    usage = []
    manager = VideoParseModelCallManager(
        persistence, execution_lease=lease, lease_duration=timedelta(minutes=5), usage_observer=usage.append,
    )
    with manager.capture():
        from gateway.usage import track_model_call
        with track_model_call(provider="fixture", model="summary", operation="generate") as call:
            with factory.begin() as session:
                task = session.get(VideoParseTask, task_id)
                detail = deepcopy(task.detail)
                runtime = detail[RUNTIME_DETAIL_KEY]
                slot = runtime["model_calls"]["active"][call.call_id]
                assert slot["execution_kind"] == "summarize"
                assert slot["execution_task_id"] == task_id
                runtime["other_phase"] = {"value": "concurrent-update"}
                task.detail = detail
            assert manager.heartbeat()
    with factory() as session:
        task = session.get(VideoParseTask, task_id)
        runtime = task.detail[RUNTIME_DETAIL_KEY]
        assert task.status == "COMPLETE"
        assert runtime["summarize"]["status"] == "RUNNING"
        assert runtime["other_phase"] == {"value": "concurrent-update"}
        assert runtime["model_calls"]["active"] == {}
        assert set(runtime["model_calls"]["used_call_ids"]) == {"understanding", "refine", call.call_id}
    assert len(usage) == 1


@pytest.mark.parametrize("replacement", ["summary_lease", "parse_task"])
def test_summary_call_admission_fences_replaced_execution(repository, monkeypatch, replacement):
    from gateway.usage import track_model_call
    from server.constants.parse import RUNTIME_DETAIL_KEY
    from server.model.job import VideoParseTask
    from tests.server.test_video_summarize_persistence import AT, _claim, _ready

    factory, _queue, persistence, task_id, _video_lease = _ready(repository)
    lease = _claim(persistence)
    if replacement == "summary_lease":
        assert persistence.release_summary(lease, now=AT)
        assert _claim(persistence).lease_version > lease.lease_version
    else:
        with factory.begin() as session:
            task = session.get(VideoParseTask, task_id)
            session.add(VideoParseTask(
                id=str(uuid4()), video_id=task.video_id, pipeline_version=task.pipeline_version,
                status="PENDING", parse_config=task.parse_config, chunk_count=task.chunk_count,
                version=2, next_execute_time=AT, max_attempts=3,
                detail={RUNTIME_DETAIL_KEY: {"accepted": True}},
            ))
    monkeypatch.setattr("server.service.video_parse_persistence.now_utc", lambda: AT)
    manager = VideoParseModelCallManager(
        persistence, execution_lease=lease, lease_duration=timedelta(minutes=5),
    )
    with manager.capture(), pytest.raises(ModelCallAdmissionError):
        with track_model_call(provider="fixture", model="summary", operation="generate"):
            pytest.fail("a stale summary must be rejected before the provider starts")
    with factory() as session:
        runtime = session.get(VideoParseTask, task_id).detail[RUNTIME_DETAIL_KEY]
        assert runtime["model_calls"]["active"] == {}


class _Repository:
    def __init__(self):
        self.allow = True
        self.reserved = []
        self.renewed = []
        self.released = []

    def reserve_model_call(
        self, execution_lease, call_id, *, lease_duration, now=None,
    ):
        video_parse_task_id = execution_lease.video_parse_task_id
        worker_id = execution_lease.worker_id
        self.reserved.append((video_parse_task_id, call_id, worker_id))
        if not self.allow:
            return None
        return ModelCallReservation(
            video_parse_task_id=video_parse_task_id,
            call_id=call_id,
            worker_id=worker_id,
            first_reservation=True,
            used_calls=1,
            max_calls=10,
            active_calls=1,
            lease_expire_time=now_utc() + lease_duration,
        )

    def renew_model_call(
        self, execution_lease, call_id, *, lease_duration, now=None,
    ):
        self.renewed.append((execution_lease, call_id))
        return self.allow

    def release_model_call(
        self, video_parse_task_id, call_id, worker_id, *, now=None,
    ):
        self.released.append((video_parse_task_id, call_id, worker_id))
        return True


class _Embeddings:
    def __init__(self):
        self.calls = 0

    def create(self, **request):
        self.calls += 1
        return SimpleNamespace(
            model=request["model"],
            data=SimpleNamespace(embedding=[1.0, 0.0]),
            usage=SimpleNamespace(prompt_tokens=1, total_tokens=1),
        )


def _manager(repository, **options):
    video_parse_task_id = str(uuid4())
    return VideoParseModelCallManager(
        repository,
        execution_lease=VideoParseLease(
            video_parse_task_id=video_parse_task_id,
            video_id=str(uuid4()),
            worker_id="worker-1",
            lease_version=1,
            lease_expire_time=now_utc() + timedelta(seconds=30),
            attempt_count=1,
            max_attempts=3,
            parse_config={},
        ),
        lease_duration=timedelta(seconds=30),
        **options,
    )


def test_manager_uses_gateway_call_id_for_reserve_usage_and_release():
    repository = _Repository()
    usage = []
    manager = _manager(repository, usage_observer=usage.append)
    embeddings = _Embeddings()
    embedder = DoubaoMultimodalEmbedder(
        "embedding", client=SimpleNamespace(multimodal_embeddings=embeddings)
    )

    with manager.capture():
        embedder.embed_text("query")

    assert embeddings.calls == 1
    assert len(repository.reserved) == len(repository.released) == len(usage) == 1
    call_id = usage[0].call_id
    assert repository.reserved[0][1] == repository.released[0][1] == call_id


def test_denial_happens_before_provider_request_and_emits_no_usage():
    repository = _Repository()
    repository.allow = False
    usage = []
    manager = _manager(repository, usage_observer=usage.append)
    embeddings = _Embeddings()
    embedder = DoubaoMultimodalEmbedder(
        "embedding", client=SimpleNamespace(multimodal_embeddings=embeddings)
    )

    with manager.capture(), pytest.raises(ModelCallAdmissionError):
        embedder.embed_text("query")

    assert embeddings.calls == 0
    assert repository.released == []
    assert usage == []


def test_cancel_prevents_new_requests_and_checks_worker_lease_first():
    repository = _Repository()
    checked = []
    manager = _manager(repository, check_active=lambda: checked.append(True))
    manager.cancel()
    embedder = DoubaoMultimodalEmbedder(
        "embedding", client=SimpleNamespace(multimodal_embeddings=_Embeddings())
    )

    with manager.capture(), pytest.raises(ModelCallCancelledError):
        embedder.embed_text("query")

    assert checked == []
    assert repository.reserved == []


def test_heartbeat_with_no_active_requests_only_checks_execution_lease():
    repository = _Repository()
    checked = []
    manager = _manager(repository, check_active=lambda: checked.append(True))

    assert manager.heartbeat()
    assert checked == [True]
    assert repository.reserved == []
    assert repository.renewed == []


def test_heartbeat_cancels_active_transports_when_reservation_is_lost():
    repository = _Repository()
    manager = _manager(repository)
    call = ModelCallTracker(
        provider="ark", model="embedding", operation="text_embedding"
    )
    closed = []
    call.add_cancel_callback(lambda: closed.append(True))
    manager.acquire(call)
    repository.allow = False

    assert manager.heartbeat() is False
    assert call.cancelled
    assert closed == [True]
    with pytest.raises(ModelCallCancelledError):
        manager.acquire(ModelCallTracker(
            provider="ark", model="embedding", operation="text_embedding"
        ))
