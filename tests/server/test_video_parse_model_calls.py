from __future__ import annotations

from datetime import timedelta
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
from server.repository.video_parse import ModelCallConcurrencyBusyError
from server.utils.time import now_utc


class _Repository:
    def __init__(self):
        self.allow = True
        self.busy_count = 0
        self.reserved = []
        self.renewed = []
        self.released = []

    def reserve_model_call(
        self, execution_lease, call_id, *, lease_duration, now=None,
    ):
        video_parse_task_id = execution_lease.video_parse_task_id
        worker_id = execution_lease.worker_id
        self.reserved.append((video_parse_task_id, call_id, worker_id))
        if self.busy_count:
            self.busy_count -= 1
            raise ModelCallConcurrencyBusyError("busy")
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
            max_concurrent_calls=2,
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


def test_transient_concurrency_contention_waits_before_request():
    repository = _Repository()
    repository.busy_count = 1
    manager = _manager(repository, concurrency_poll_seconds=0.001)
    embeddings = _Embeddings()
    embedder = DoubaoMultimodalEmbedder(
        "embedding", client=SimpleNamespace(multimodal_embeddings=embeddings)
    )

    with manager.capture():
        embedder.embed_text("query")

    assert len(repository.reserved) == 2
    assert embeddings.calls == 1
