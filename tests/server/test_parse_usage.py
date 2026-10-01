from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from engine.parsev1.embed import ChunkEmbedding, EmbedStage
from engine.parsev1.usage import ParseModelCall, parse_chunk_scope
from fastapi import FastAPI
from fastapi.testclient import TestClient
from gateway.embedding import EmbeddingResult
from gateway.usage import ModelCallUsage, TokenUsage, track_model_call
from pydantic import ValidationError
from sqlalchemy import select

from server.api.errors import register_exception_handlers
from server.api.parse_usage import project_parse_usage
from server.config.usage import ModelPricingConfig, PriceTier
from server.database import create_database_engine, create_schema, create_session_factory
from server.model.job import SeriesParseJobRecord, VideoParseTaskRecord
from server.model.dto.jobDTO import (
    ChunkParseLease,
    IntervalParseLease,
    PrefixParseLease,
    VideoParseLease,
)
from server.model.parse_usage import (
    ParseModelCallRecord,
    ParseUsageAttemptRecord,
    VideoParseUsageAttemptRecord,
)
from server.model.video import VideoRecord
from server.mq.mysql_queue import MySqlParseJobQueue
from server.repository.job import ParseJobRepository
from server.repository.library import LibraryRepository
from server.repository.parse_usage import ParseUsageRepository
from server.router.jobs import router
from server.service.job import ParseJobService
from server.service.library import LibraryService
from server.service.parse_usage import ParseUsageService, estimate_cost_cny
from server.service.video_processing import VideoProcessingService
from server.utils.time import (
    datetime_to_unix_time_ms,
    timedelta_to_ms,
    unix_time_ms_to_datetime,
)


def _pricing() -> ModelPricingConfig:
    common = {"provider": "fixture", "source_url": "https://example.com/official-pricing",
              "checked_at": "2026-09-15T00:00:00Z", "effective_from": "2026-09-01T00:00:00Z"}
    return ModelPricingConfig.model_validate({"models": [
        dict(common, model="chat", category="parse", tiers=[{
            "input_cny_per_unit": "2", "output_cny_per_unit": "8", "cached_input_cny_per_unit": "0.04"}]),
        dict(common, model="embed", category="embedding", tiers=[{
            "text_input_cny_per_unit": "0.7", "image_input_cny_per_unit": "1.8"}]),
    ]})


@pytest.fixture
def accounting(tmp_path):
    engine = create_database_engine(f"sqlite+pysqlite:///{tmp_path / 'usage.sqlite'}", environment="test")
    create_schema(engine)
    factory = create_session_factory(engine)
    library = LibraryService(LibraryRepository(factory))
    series = library.create_series(workspace_id="mine", name="Actual Series", description=None, character_bible=[])
    video_id, job_id = str(uuid4()), str(uuid4())
    now = 1_789_411_200_000
    with factory.begin() as session:
        session.add(VideoRecord(
            video_id=video_id, series_id=series.series_id, episode_order=2, upload_sequence=1,
            original_filename="actual-episode.mp4", original_source_uri="artifact://test/actual.mp4",
            content_hash="a" * 64, total_bytes=100, duration_seconds=10,
            upload_status="COMPLETED", parse_status="QUEUED", upload_progress=100, parse_progress=0,
        ))
        session.add(SeriesParseJobRecord(
            job_id=job_id, series_id=series.series_id, video_id=video_id, pipeline_version="fixture",
            status="QUEUED", stage="QUEUED", progress=0, attempt_count=0, max_attempts=3,
            available_at=now, lease_version=0,
        ))
    jobs = ParseJobRepository(factory)
    queue = MySqlParseJobQueue(factory)
    ledger = ParseUsageRepository(factory)
    service = ParseUsageService(ledger, ParseJobService(jobs, library), _pricing())
    try:
        yield SimpleNamespace(
            factory=factory, jobs=jobs, queue=queue, ledger=ledger,
            service=service, job_id=job_id, now=now,
        )
    finally:
        engine.dispose()


def _lease(accounting):
    lease = accounting.queue.claim_next(
        "worker", lease_duration=timedelta(minutes=5), now=accounting.now,
    )
    assert lease is not None
    return lease


def _event(*, model="chat", operation="video_parse", category="parse", sequence_no=0, usage=None, status="completed"):
    return ParseModelCall(ModelCallUsage(
        call_id=str(uuid4()), provider="fixture", model=model, operation=operation,
        started_at=1_789_411_200_000, request_id="real-provider-request", usage=usage, status=status,
    ), sequence_no=sequence_no, category=category)


def test_decimal_cache_and_reasoning_are_subsets():
    usage = TokenUsage(1000, 100, 1100, 200, 40)
    tier = _pricing().models[0].tiers[0]
    assert estimate_cost_cny(usage, tier, category="parse", operation="video_parse", unit=1_000_000) == Decimal("0.002408")


def test_audio_and_cached_audio_are_not_charged_twice():
    tier = PriceTier(input_cny_per_unit="0.6", cached_input_cny_per_unit="0.12",
                     audio_input_cny_per_unit="9", cached_audio_input_cny_per_unit="1.8", output_cny_per_unit="3.6")
    usage = TokenUsage(1000, 100, 1100, 200, None, audio_input_tokens=300, cached_audio_input_tokens=50)
    # 550 ordinary + 150 cached ordinary + 250 audio + 50 cached audio + 100 output.
    assert estimate_cost_cny(usage, tier, category="parse", operation="asr", unit=1_000_000) == Decimal("0.003048")
    assert estimate_cost_cny(replace(usage, audio_input_tokens=None, cached_audio_input_tokens=None), tier,
                             category="parse", operation="asr", unit=1_000_000) is None


@pytest.mark.parametrize("operation,text,image,expected", [
    ("text_embedding", None, None, "0.00007"),
    ("text_embedding", 100, None, "0.00007"),
    ("video_embedding", None, 100, "0.00018"),
    ("video_embedding", 20, 80, "0.000158"),
    ("video_embedding", None, None, None),
])
def test_embedding_uses_actual_modalities(operation, text, image, expected):
    usage = TokenUsage(100, 0, 100, None, None, text_input_tokens=text, image_input_tokens=image)
    cost = estimate_cost_cny(usage, _pricing().models[1].tiers[0], category="embedding", operation=operation, unit=1_000_000)
    assert cost == (Decimal(expected) if expected is not None else None)


def test_unknown_cache_does_not_erase_actual_core_tokens(accounting):
    lease = _lease(accounting)
    usage = TokenUsage(100, 20, 120, None, None)
    with accounting.service.capture(lease):
        accounting.service.record(lease, _event(usage=usage))
    public = project_parse_usage(accounting.service.get(lease.job_id, workspace_id="mine"))
    assert public.total_tokens == 120 and public.missing_usage_count == 0
    assert public.items[0].input_tokens == 100 and public.items[0].output_tokens == 20
    assert public.items[0].cached_input_tokens is None and public.items[0].reasoning_tokens is None
    assert public.estimated_cost_cny is None and public.unpriced_call_count == 1


def test_call_id_idempotence_retains_original_price_snapshot(accounting):
    lease = _lease(accounting)
    event = _event(usage=TokenUsage(1, 0, 1, 0, 0))
    first = accounting.service.record(lease, event)
    repriced = ParseUsageService(accounting.ledger, accounting.service._jobs, ModelPricingConfig())
    repeated = repriced.record(lease, event)
    assert repeated == first
    assert repeated.estimated_cost_cny == Decimal("0.000002")
    assert repeated.price_snapshot["tier"]["input_cny_per_unit"] == "2"
    assert len(accounting.ledger.list_calls(lease.job_id, workspace_id="mine")) == 1
    assert accounting.ledger.list_calls(lease.job_id, workspace_id="foreign") == []


def test_parse_usage_persists_aware_utc_and_keeps_unix_ms_at_the_event_boundary(
    accounting,
):
    lease = _lease(accounting)
    event = _event(usage=TokenUsage(1, 0, 1, 0, 0))
    with accounting.service.capture(lease):
        state = accounting.service.record(lease, event)

    assert isinstance(state.started_at, datetime)
    assert state.started_at.utcoffset() == timedelta(0)
    assert datetime_to_unix_time_ms(state.started_at) == event.call.started_at
    with accounting.factory() as session:
        attempt = session.get(
            ParseUsageAttemptRecord,
            (lease.job_id, lease.attempt_count, lease.lease_version),
        )
        call = session.get(ParseModelCallRecord, event.call.call_id)
        assert attempt is not None and call is not None
        for value in (
            attempt.started_at,
            call.started_at,
            call.created_at,
            call.updated_at,
        ):
            assert isinstance(value, datetime)
            assert value.utcoffset() == timedelta(0)


def test_v2_usage_links_directly_to_video_task_without_fake_v1_job(accounting):
    with accounting.factory() as session:
        legacy = session.get(SeriesParseJobRecord, accounting.job_id)
        video_id, series_id = legacy.video_id, legacy.series_id
    task_id = str(uuid4())
    now = unix_time_ms_to_datetime(accounting.now)
    with accounting.factory.begin() as session:
        session.add(VideoParseTaskRecord(
            id=task_id,
            video_id=video_id,
            pipeline_version="video-parse-v2",
            status="PREPARING",
            parse_config={"execution": {
                "max_retry_count": 2,
                "max_model_calls": 10,
                "max_concurrent_model_calls": 2,
            }},
            chunk_count=1,
            version=1,
            next_execute_time=now,
            attempt_count=1,
            max_attempts=3,
            lease_owner="v2-worker",
            lease_version=1,
            lease_expire_time=now + timedelta(minutes=5),
            heartbeat_time=now,
            start_time=now,
            processing_duration_ms=0,
            processing_timing_complete=True,
            is_test=True,
            is_deleted=False,
            detail={"_worker_runtime_v2": {"accepted": True}},
            create_time=now,
            update_time=now,
        ))
    lease = VideoParseLease(
        video_parse_task_id=task_id,
        video_id=video_id,
        worker_id="v2-worker",
        lease_version=1,
        lease_expire_time=now + timedelta(minutes=5),
        attempt_count=1,
        max_attempts=3,
        parse_config={},
    )
    assert accounting.ledger.start_video_attempt(lease, now=now)
    state = accounting.ledger.record_video_call(
        lease,
        _event(usage=TokenUsage(3, 1, 4, 0, 0)),
        workspace_id="mine",
        series_id=series_id,
        video_id=video_id,
        estimated_cost_cny=Decimal("0.1"),
        price_snapshot={"kind": "fixture"},
    )
    accounting.ledger.finish_video_attempt(lease, recording_complete=True)
    assert state.job_id is None
    assert state.video_parse_task_id == task_id
    assert (state.execution_kind, state.execution_task_id) == (
        "video", task_id
    )
    assert accounting.ledger.list_video_calls(
        task_id, workspace_id="mine"
    ) == [state]
    assert accounting.ledger.list_calls(
        accounting.job_id, workspace_id="mine"
    ) == []

    prefix_lease = PrefixParseLease(
        video_parse_task_id=task_id,
        chunk_parse_task_id=str(uuid4()),
        video_chunk_id=str(uuid4()),
        sequence_no=0,
        worker_id="prefix-worker",
        lease_version=7,
        lease_expire_time=now + timedelta(minutes=5),
        retry_count=2,
        content_version=1,
        content={},
        content_detail={},
        series_prefix_compact="",
        video_prefix_compact="",
    )
    assert accounting.ledger.start_video_attempt(prefix_lease, now=now)
    prefix_state = accounting.ledger.record_video_call(
        prefix_lease,
        _event(usage=TokenUsage(2, 0, 2, 0, 0)),
        workspace_id="mine",
        series_id=series_id,
        video_id=video_id,
        estimated_cost_cny=None,
        price_snapshot=None,
    )
    accounting.ledger.finish_video_attempt(
        prefix_lease, recording_complete=True
    )
    assert prefix_state.attempt_count == 3
    assert prefix_state.lease_version == 7
    assert (prefix_state.execution_kind, prefix_state.execution_task_id) == (
        "prefix", prefix_lease.chunk_parse_task_id
    )


def test_v2_usage_attempt_identity_includes_execution_kind_and_task(accounting):
    with accounting.factory() as session:
        legacy = session.get(SeriesParseJobRecord, accounting.job_id)
        video_id = legacy.video_id
    task_id = str(uuid4())
    now = unix_time_ms_to_datetime(accounting.now)
    with accounting.factory.begin() as session:
        session.add(VideoParseTaskRecord(
            id=task_id,
            video_id=video_id,
            pipeline_version="video-parse-v2",
            status="PREPARING",
            parse_config={"execution": {
                "max_retry_count": 2,
                "max_model_calls": 10,
                "max_concurrent_model_calls": 2,
            }},
            chunk_count=2,
            version=1,
            next_execute_time=now,
            attempt_count=1,
            max_attempts=3,
            lease_owner="v2-worker",
            lease_version=1,
            lease_expire_time=now + timedelta(minutes=5),
            heartbeat_time=now,
            start_time=now,
            processing_duration_ms=0,
            processing_timing_complete=True,
            is_test=True,
            is_deleted=False,
            detail={"_worker_runtime_v2": {"accepted": True}},
            create_time=now,
            update_time=now,
        ))

    chunk_a_id, chunk_b_id, interval_id = (
        str(uuid4()), str(uuid4()), str(uuid4())
    )
    common = {
        "video_parse_task_id": task_id,
        "worker_id": "worker",
        "lease_version": 1,
        "lease_expire_time": now + timedelta(minutes=5),
        "retry_count": 0,
        "max_retry_count": 2,
    }
    chunk_a = ChunkParseLease(
        chunk_parse_task_id=chunk_a_id,
        video_chunk_id=str(uuid4()),
        **common,
    )
    chunk_b = ChunkParseLease(
        chunk_parse_task_id=chunk_b_id,
        video_chunk_id=str(uuid4()),
        **common,
    )
    interval = IntervalParseLease(
        interval_parse_task_id=interval_id,
        chunk_parse_task_ids=(chunk_a_id, chunk_b_id),
        start_sequence_no=0,
        chunk_count=2,
        phase="REFINING",
        **common,
    )
    prefix = PrefixParseLease(
        video_parse_task_id=task_id,
        chunk_parse_task_id=chunk_a_id,
        video_chunk_id=str(uuid4()),
        sequence_no=0,
        worker_id="worker",
        lease_version=1,
        lease_expire_time=now + timedelta(minutes=5),
        retry_count=0,
        content_version=1,
        content={},
        content_detail={},
        series_prefix_compact="",
        video_prefix_compact="",
    )
    leases = (chunk_a, chunk_b, interval, prefix)
    assert [
        accounting.ledger.start_video_attempt(lease, now=now)
        for lease in leases
    ] == [True, True, True, True]
    assert not accounting.ledger.start_video_attempt(chunk_a, now=now)

    with accounting.factory() as session:
        attempts = list(session.scalars(
            select(VideoParseUsageAttemptRecord).where(
                VideoParseUsageAttemptRecord.video_parse_task_id == task_id
            )
        ))
    assert len(attempts) == 4
    assert {
        (item.execution_kind, item.execution_task_id)
        for item in attempts
    } == {
        ("chunk", chunk_a_id),
        ("chunk", chunk_b_id),
        ("interval", interval_id),
        ("prefix", chunk_a_id),
    }


def test_failed_and_stale_calls_are_retained_across_all_attempts(accounting):
    first = _lease(accounting)
    with accounting.service.capture(first):
        accounting.service.record(first, _event(usage=TokenUsage(100, 20, 120, 0, 0), status="failed"))
    retry_at = accounting.now + timedelta_to_ms(timedelta(seconds=2))
    assert accounting.queue.retry(
        first, error="fixture", retry_at=retry_at, duration_ms=0, now=retry_at,
    )
    second = accounting.queue.claim_next(
        "replacement", lease_duration=timedelta(minutes=5), now=retry_at,
    )
    assert second is not None and second.attempt_count == 2
    before = accounting.jobs.get(second.job_id)
    accounting.service.record(first, _event(usage=TokenUsage(50, 0, 50, 0, 0), sequence_no=None))
    with accounting.service.capture(second):
        accounting.service.record(second, _event(model="embed", category="embedding", operation="video_embedding",
                                                 usage=TokenUsage(10, 0, 10, None, None, image_input_tokens=10)))
    assert accounting.jobs.get(second.job_id) == before
    calls = accounting.ledger.list_calls(first.job_id, workspace_id="mine")
    assert {call.attempt_count for call in calls} == {1, 2}
    assert any(call.lease_version == first.lease_version for call in calls)
    assert any(call.status == "failed" for call in calls)
    assert any(call.video_chunk_id is None for call in calls)
    public = project_parse_usage(accounting.service.get(first.job_id, workspace_id="mine"))
    assert public.coverage == "recorded" and public.call_count == 3 and public.total_tokens == 180
    assert public.estimated_cost_cny == "0.000478"
    assert public.series_name == "Actual Series" and public.original_filename == "actual-episode.mp4"


def test_missing_usage_preserves_known_subtotals_and_price_gaps_stay_explicit(accounting):
    lease = _lease(accounting)
    with accounting.service.capture(lease):
        accounting.service.record(lease, _event(status="failed"))
        accounting.service.record(lease, _event(model="unknown", usage=TokenUsage(2, 1, 3, 0, 0)))
    state = accounting.service.get(lease.job_id, workspace_id="mine")
    assert state.coverage == "recorded"
    assert state.call_count == 2 and state.missing_usage_count == state.unpriced_call_count == 1
    assert state.total_tokens == 3 and state.estimated_cost_cny is None
    assert next(item for item in state.items if item.model == "unknown").total_tokens == 3
    calls = accounting.ledger.list_calls(lease.job_id, workspace_id="mine")
    assert all(call.price_snapshot is None for call in calls)


def test_failed_call_without_usage_does_not_hide_same_models_known_subtotal(accounting):
    lease = _lease(accounting)
    with accounting.service.capture(lease):
        accounting.service.record(lease, _event(status="failed"))
        accounting.service.record(
            lease,
            _event(usage=TokenUsage(100, 20, 120, 0, 0)),
        )
    state = accounting.service.get(lease.job_id, workspace_id="mine")
    assert state.missing_usage_count == 1
    assert state.total_tokens == state.items[0].total_tokens == 120
    assert state.estimated_cost_cny == state.items[0].estimated_cost_cny == Decimal("0.00036")


def test_historical_unknown_is_distinct_from_observed_zero_calls(accounting):
    before = accounting.service.get(accounting.job_id, workspace_id="mine")
    assert before.coverage == "not_started" and before.estimated_cost_cny is None
    lease = _lease(accounting)
    historical = accounting.service.get(lease.job_id, workspace_id="mine")
    assert historical.coverage == "unknown" and historical.total_tokens is None
    with accounting.service.capture(lease):
        pass
    observed = accounting.service.get(lease.job_id, workspace_id="mine")
    assert observed.coverage == "recorded" and observed.total_tokens == 0 and observed.estimated_cost_cny == 0


def test_last_call_finishing_after_snapshot_does_not_claim_a_complete_prefix(accounting, monkeypatch):
    lease = _lease(accounting)
    accounting.ledger.start_attempt(
        lease,
        now=unix_time_ms_to_datetime(accounting.now),
    )
    accounting.service.record(lease, _event(usage=TokenUsage(100, 0, 100, 0, 0)))
    snapshot = accounting.ledger.snapshot
    def finishes_after_read(*args, **kwargs):
        state = snapshot(*args, **kwargs)
        accounting.service.record(lease, _event(usage=TokenUsage(50, 0, 50, 0, 0)))
        accounting.ledger.finish_attempt(lease, recording_complete=True)
        return state
    monkeypatch.setattr(accounting.ledger, "snapshot", finishes_after_read)
    prefix = accounting.service.get(lease.job_id, workspace_id="mine")
    assert prefix.call_count == 1 and prefix.coverage == "partial"
    assert prefix.total_tokens == 100 and prefix.estimated_cost_cny == Decimal("0.0002")
    monkeypatch.setattr(accounting.ledger, "snapshot", snapshot)
    final = accounting.service.get(lease.job_id, workspace_id="mine")
    assert final.coverage == "recorded" and final.call_count == 2 and final.total_tokens == 150


def test_new_attempt_after_metadata_read_is_included_in_coverage(accounting, monkeypatch):
    first = _lease(accounting)
    with accounting.service.capture(first):
        accounting.service.record(first, _event(usage=TokenUsage(100, 0, 100, 0, 0)))
    get_job = accounting.service._jobs.get
    def claims_after_metadata(*args, **kwargs):
        detail = get_job(*args, **kwargs)
        retry_at = accounting.now + timedelta_to_ms(timedelta(seconds=2))
        assert accounting.queue.retry(
            first, error="fixture", retry_at=retry_at, duration_ms=0, now=retry_at,
        )
        second = accounting.queue.claim_next(
            "next", lease_duration=timedelta(minutes=5), now=retry_at,
        )
        assert second is not None and second.attempt_count == 2
        return detail
    monkeypatch.setattr(accounting.service._jobs, "get", claims_after_metadata)
    state = accounting.service.get(first.job_id, workspace_id="mine")
    assert state.coverage == "partial" and state.total_tokens == 100
    assert state.estimated_cost_cny == Decimal("0.0002")


def test_recording_failure_preserves_model_result_and_cannot_be_hidden_by_replay(accounting, monkeypatch):
    lease = _lease(accounting)
    original = accounting.ledger.record_call
    def unavailable(*args, **kwargs):
        raise RuntimeError("fixture unavailable")
    monkeypatch.setattr(accounting.ledger, "record_call", unavailable)
    with accounting.service.capture(lease):
        with track_model_call(provider="fixture", model="chat", operation="video_parse") as tracker:
            tracker.usage = TokenUsage(100, 0, 100, 0, 0)
            result = "provider result remains successful"
    assert result == "provider result remains successful"
    assert accounting.service.get(lease.job_id, workspace_id="mine").coverage == "partial"
    monkeypatch.setattr(accounting.ledger, "record_call", original)
    with accounting.service.capture(lease):
        accounting.service.record(lease, _event(usage=TokenUsage(2, 0, 2, 0, 0)))
    state = accounting.service.get(lease.job_id, workspace_id="mine")
    assert state.coverage == "partial" and state.estimated_cost_cny == Decimal("0.000004")


def test_get_usage_route_requires_workspace_and_enforces_ownership(accounting):
    app = FastAPI()
    register_exception_handlers(app)
    app.state.services = SimpleNamespace(parse_usage=accounting.service)
    app.include_router(router)
    with TestClient(app) as client:
        assert client.get("/api/jobs/usage/get", params={"jobId": accounting.job_id}).status_code == 422
        assert client.get("/api/jobs/usage/get", params={"jobId": accounting.job_id, "workspaceId": "foreign"}).status_code == 404
        response = client.get("/api/jobs/usage/get", params={"jobId": accounting.job_id, "workspaceId": "mine"})
    assert response.status_code == 200 and response.json()["coverage"] == "not_started"
    assert response.json()["currency"] == "CNY" and response.json()["estimatedCostCny"] is None


def test_processing_capture_covers_failure_and_only_real_embedding_cache_misses(accounting, tmp_path, monkeypatch):
    lease = _lease(accounting)
    processor = VideoProcessingService(
        None,
        None,
        None,
        None,
        object(),
        parse_usage=accounting.service,
    )

    class EmbeddingAgent:
        def embed_text(self, _text):
            raise AssertionError("text embedding is not expected")

        def embed_video(self, _video_path):
            with track_model_call(
                provider="fixture",
                model="embed",
                operation="video_embedding",
            ) as tracker:
                tracker.usage = TokenUsage(
                    100,
                    0,
                    100,
                    None,
                    None,
                    image_input_tokens=100,
                )
                return EmbeddingResult(
                    model="embed",
                    modality="video",
                    vector=[1, 2, 3],
                )

    class EmbeddingCache:
        def __init__(self):
            self.value: ChunkEmbedding | None = None

        def ensure_active(self):
            pass

        def load_embedding(self, **_kwargs):
            return self.value

        def save_embedding(self, embedding):
            self.value = embedding

    embed_stage = EmbedStage(EmbeddingAgent(), dimensions=3)
    embedding_cache = EmbeddingCache()

    def process(_lease):
        with parse_chunk_scope(0):
            with track_model_call(provider="fixture", model="chat", operation="video_parse") as tracker:
                tracker.usage = TokenUsage(100, 10, 110, 0, 0)
        for _ in range(2):
            embed_stage._embed_video(
                2,
                tmp_path / "chunk.mp4",
                "a" * 64,
                persistence=embedding_cache,
            )
        with parse_chunk_scope(3):
            with track_model_call(provider="fixture", model="chat", operation="timeline_refine"):
                raise RuntimeError("actual model failure")
    monkeypatch.setattr(processor, "_process", process)
    with pytest.raises(RuntimeError, match="actual model failure"):
        processor.process(lease)
    calls = accounting.ledger.list_calls(lease.job_id, workspace_id="mine")
    assert len(calls) == 3
    assert {(call.sequence_no, call.category, call.status) for call in calls} == {
        (0, "parse", "completed"), (2, "embedding", "completed"), (3, "parse", "failed")}


def test_pricing_rejects_float_rates_and_overlapping_tiers():
    with pytest.raises(ValidationError, match="quoted decimal"):
        PriceTier(input_cny_per_unit=0.7)
    price = _pricing().model_dump(mode="json")
    price["models"][0]["tiers"] = [{"max_input_tokens": 100}, {"min_input_tokens": 50}]
    with pytest.raises(ValidationError, match="non-overlapping"):
        ModelPricingConfig.model_validate(price)
