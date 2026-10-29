from __future__ import annotations

import asyncio
from copy import deepcopy
from contextvars import ContextVar
from dataclasses import replace
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import event, select
from sqlalchemy.orm.attributes import flag_modified

from engine.parsev2.schemas import VideoParseResult
from engine.parsev2.validation import parse_content_fingerprint
from engine.rag.contracts import VideoChunkRetrievalIdentity
from server.config.parse_profile import default_video_parse_v2_config
from server.database import create_session_factory
from server.model.dto.jobDTO import PrefixParseLease
from server.model.job import (
    ChunkParseContent,
    ChunkParseTask,
    IntervalParseContent,
    IntervalParseTask,
    VideoParseTask,
)
from server.model.video import Series, VideoChunk, Video
from server.mq.video_parse_queue import MySqlVideoParseQueue
from server.service.video_parse_persistence import (
    INDEX_EMBEDDINGS_DETAIL_KEY,
    ModelCallLeaseConflictError,
    VideoParseConflictError,
    VideoParseLeaseLostError,
    VideoParsePersistenceService,
    VideoParseSnapshotConflictError,
    VideoParseTaskNotFoundError,
    invalidate_series_prefixes_from,
)
from server.error.retrieval import SeriesNotReadyError, VideoChunkNotFoundError
from server.service.video_parse import VideoParseRuntimeService
from server.utils.time import datetime_to_unix_time_ms


NOW = datetime(2026, 9, 29, 2, 0, tzinfo=UTC)


def _refinement_inputs(repo, lease, *, now=None):
    snapshot = repo.load_interval_members(lease, now=now)
    return {
        "input_versions": {member.chunk_parse_task_id: member.version for member in snapshot.members},
        "input_fingerprints": {member.chunk_parse_task_id: parse_content_fingerprint(member.content) for member in snapshot.members},
        "input_contents": {member.chunk_parse_task_id: deepcopy(member.content) for member in snapshot.members},
    }


def _finish_index(repo, lease, versions=None, *, now):
    """Stand in for the host's verified ES publication in repository tests."""
    snapshot = repo.load_chunk_indexing_snapshot(lease, now=now)
    chunk = snapshot.chunk
    repo.save_index_embeddings(
        lease, chunk.chunk_parse_task_id, chunk.content_version, {},
        receipt={"parse_task_id": lease.video_parse_task_id,
                 "video_chunk_id": chunk.video_chunk_id,
                 "content_version": chunk.content_version,
                 "index_generation": snapshot.target_index_generation}, now=now,
    )
    return repo.complete_chunk_indexing(
        lease, versions.get(chunk.chunk_parse_task_id, chunk.content_version) if versions else chunk.content_version,
        snapshot.target_index_generation, now=now,
    )


def _finish_ready_indexes(queue, repo, *, now):
    while (lease := queue.claim_chunk_index("index", lease_duration=timedelta(minutes=5), now=now)):
        _finish_index(repo, lease, now=now)


def _fixture_event(value, *, sequence_no=0):
    return {"event_type": "ACTION", "start_seconds": sequence_no * 10_000 + 1000,
            "end_seconds": sequence_no * 10_000 + 4000, "description": str(value),
            "participants": [], "visible_persons": []}


def _event_target(*, max_frames=3):
    from engine.rag.encoding import EncoderContract
    from engine.rag.indexing.events import EventIndexingContract
    from engine.rag.vision.event_frames import EventFrameContract

    encoder = EncoderContract("test", "embedding", "v1", 2)
    contract = EventIndexingContract(semantic_encoder=encoder, vision_encoder=encoder,
                                    capture=EventFrameContract(max_height=720, max_frames=max_frames))
    return {"index_name": "seekino_video_events_test_v1", "contract_id": contract.contract_id,
            "contract": contract.as_dict()}


def _event_index_fixture(repository, *, empty=False):
    factory, queue, repo, task_id, video_lease, prepared = _prepare(repository, chunk_count=1)
    _commit_all_chunks(factory, queue, repo, video_lease, prepared, count=1)
    if empty:
        with factory.begin() as session:
            content = session.scalar(select(ChunkParseContent))
            content.content = {"persons": [], "timeline": []}
    at = NOW + timedelta(minutes=1)
    refine = queue.claim_interval_refine("refine", lease_duration=timedelta(minutes=5), now=at)
    repo.commit_interval_refinement(
        refine, **_refinement_inputs(repo, refine, now=at),
        interval_content={"summary": "done"}, member_updates={}, now=at,
    )
    lease = queue.claim_chunk_index("index", lease_duration=timedelta(minutes=5), now=at)
    assert lease is not None
    return factory, queue, repo, task_id, lease, at, video_lease


def _event_receipt(snapshot, target):
    from engine.rag.indexing.events import EventIndexingContract, build_event_index_receipt, plan_event_index

    contract = EventIndexingContract.model_validate(target["contract"])
    chunk = snapshot.chunk
    plans = plan_event_index(snapshot.video_parse_task_id, chunk.video_chunk_id, chunk.content_version,
                             VideoParseResult.model_validate(chunk.content), chunk.start_seconds,
                             chunk.end_seconds, contract)
    return {"parse_task_id": snapshot.video_parse_task_id, "video_chunk_id": chunk.video_chunk_id,
            "content_version": chunk.content_version, "index_generation": snapshot.target_index_generation,
            "events": build_event_index_receipt(target["index_name"], contract, plans).as_dict()}


def test_event_target_freezes_once_and_rejects_stale_lease(repository):
    factory, _queue, repo, task_id, lease, at, _video_lease = _event_index_fixture(repository)
    original = repo.load_task_input(task_id).task.parse_config
    target = _event_target()
    assert repo.bind_event_index_target(lease, target, now=at) == target
    assert repo.bind_event_index_target(lease, _event_target(max_frames=1), now=at) == target
    with pytest.raises(VideoParseLeaseLostError):
        repo.bind_event_index_target(replace(lease, lease_version=lease.lease_version + 1), target, now=at)
    with factory() as session:
        assert session.get(VideoParseTask, task_id).detail["_event_index_target_v1"] == target
    assert repo.load_task_input(task_id).task.parse_config == original
    with pytest.raises(VideoParseConflictError, match="target changed"):
        repo.load_chunk_indexing_snapshot(lease, event_target=_event_target(max_frames=1), now=at)


def test_index_completion_returns_persisted_task_timestamps(repository):
    factory, queue, repo, task_id, lease, at, video_lease = _event_index_fixture(repository)
    assert queue.release_execution(video_lease, now=at)
    completed_at = at + timedelta(seconds=1)

    state = _finish_index(repo, lease, now=completed_at)

    with factory() as session:
        task = session.get(VideoParseTask, task_id)
        assert state.status == task.status == "COMPLETE"
        assert state.create_time == task.create_date
        assert state.update_time == task.update_date == completed_at


@pytest.mark.parametrize("empty", [False, True])
def test_event_receipt_required_for_completion_and_preserves_other_cache(repository, empty):
    factory, _queue, repo, task_id, lease, at, video_lease = _event_index_fixture(repository, empty=empty)
    target = repo.bind_event_index_target(lease, _event_target(), now=at)
    snapshot = repo.load_chunk_indexing_snapshot(lease, event_target=target, now=at)
    chunk = snapshot.chunk
    receipt = _event_receipt(snapshot, target)
    basic = {key: value for key, value in receipt.items() if key != "events"}
    repo.save_index_embeddings(lease, chunk.chunk_parse_task_id, chunk.content_version,
                               {"semantic": {"keep": True}, "events": {"first": {"vector": [1, 0]}}},
                               receipt=basic, event_target=target, now=at)
    with pytest.raises(VideoParseConflictError, match="publication is incomplete"):
        repo.complete_chunk_indexing(lease, chunk.content_version, snapshot.target_index_generation,
                                     event_target=target, now=at)
    with pytest.raises(VideoParseConflictError, match="target changed"):
        repo.save_index_embeddings(lease, chunk.chunk_parse_task_id, chunk.content_version, {},
                                   receipt=receipt, now=at)
    repo.save_index_embeddings(lease, chunk.chunk_parse_task_id, chunk.content_version,
                               {"events": {"second": {"vector": [0, 1]}}}, receipt=receipt,
                               event_target=target, now=at)
    repo.complete_chunk_indexing(lease, chunk.content_version, snapshot.target_index_generation,
                                 event_target=target, now=at)
    repo.aggregate_task(video_lease, now=at)
    with factory() as session:
        content = session.scalar(select(ChunkParseContent))
        cache = content.detail[INDEX_EMBEDDINGS_DETAIL_KEY]
        assert set(cache["representations"]["events"]) == {"first", "second"}
        assert cache["representations"]["semantic"] == {"keep": True}
        assert cache["receipt"]["events"]["event_count"] == (0 if empty else 1)
    status = repo.event_index_status(task_id)
    assert status["task_complete"] and status["covered_chunks"] == status["total_chunks"] == 1


def test_event_coverage_pairs_version_and_rebuild_preserves_source(repository):
    factory, _queue, repo, task_id, lease, at, video_lease = _event_index_fixture(repository)
    target = repo.bind_event_index_target(lease, _event_target(), now=at)
    snapshot = repo.load_chunk_indexing_snapshot(lease, event_target=target, now=at)
    chunk = snapshot.chunk
    receipt = _event_receipt(snapshot, target)
    repo.save_index_embeddings(lease, chunk.chunk_parse_task_id, chunk.content_version, {},
                               receipt=receipt, event_target=target, now=at)
    repo.complete_chunk_indexing(lease, chunk.content_version, snapshot.target_index_generation,
                                 event_target=target, now=at)
    repo.aggregate_task(video_lease, now=at)
    scope = repo.resolve_retrieval_scope(tenant_id=chunk.tenant_id, series_ids=[chunk.series_id])
    options = {"index_name": target["index_name"], "contract_id": target["contract_id"]}
    assert repo.validate_event_coverage(scope, **options) == ((task_id, chunk.video_chunk_id, chunk.content_version),)
    assert repo.validate_event_coverage(scope, video_chunk_ids=[chunk.video_chunk_id], **options)
    with pytest.raises(VideoParseConflictError, match="outside"):
        repo.validate_event_coverage(scope, video_chunk_ids=[str(uuid4())], **options)
    with pytest.raises(VideoParseConflictError, match="not covered"):
        repo.validate_event_coverage(scope, **{**options, "contract_id": "other"})
    replacement = _event_target(max_frames=1)
    assert repo.request_index_rebuild(task_id, event_target=replacement, now=at) == (chunk.chunk_parse_task_id,)
    with factory() as session:
        content = session.scalar(select(ChunkParseContent))
        parent = session.get(VideoParseTask, task_id)
        assert content.version == chunk.content_version and content.content == chunk.content
        assert parent.target_index_generation == snapshot.target_index_generation
        assert parent.detail["_event_index_target_v1"] == replacement
    with pytest.raises(VideoParseConflictError, match="COMPLETE"):
        repo.validate_event_coverage(scope, **options)


def test_legacy_complete_is_not_zero_event_coverage(repository):
    factory, repo, _video, identity, _chunk_id = _seed_retrieval_chunk(repository)
    task_id = identity.parse_task_id
    with factory() as session:
        task = session.get(VideoParseTask, task_id)
        video = session.get(Video, task.video_id)
        series = session.get(Series, video.series_id)
        tenant_id, series_id = series.tenant_id, series.id
    scope = repo.resolve_retrieval_scope(tenant_id=tenant_id, series_ids=[series_id])
    target = _event_target()
    with pytest.raises(VideoParseConflictError, match="not covered"):
        repo.validate_event_coverage(scope, index_name=target["index_name"], contract_id=target["contract_id"])
    assert repo.event_index_status(task_id)["covered_chunks"] == 0


def test_video_asr_timing_is_fenced_and_survives_chunk_work(repository):
    factory, queue, repo, task_id, lease, _ = _prepare(repository, chunk_count=1)
    started = datetime_to_unix_time_ms(NOW)
    assert repo.record_execution_timing(lease, started_at_ms=started, elapsed_ms=1000, now=NOW)
    assert not repo.record_execution_timing(replace(lease, worker_id="stale"), started_at_ms=started,
                                          elapsed_ms=2000, now=NOW)
    assert not repo.record_execution_timing(replace(lease, lease_version=lease.lease_version + 1),
                                          started_at_ms=started, elapsed_ms=2000, now=NOW)
    assert repo.record_execution_timing(lease, started_at_ms=started, elapsed_ms=1500,
                                      finished=True, outcome="completed", now=NOW)
    assert queue.claim_chunk("chunk", lease_duration=timedelta(minutes=1), now=NOW) is not None
    with factory() as session:
        entry = session.get(VideoParseTask, task_id).detail["_worker_runtime_v2"]["execution_timing"]["entries"][
            f"asr:{task_id}:{lease.lease_version}"]
        assert entry["execution_kind"] == "video" and entry["elapsed_ms"] == 1500 and entry["finished"]


def test_execution_timing_is_monotonic_frozen_and_preserves_runtime(repository):
    factory, queue, repo, task_id, video_lease, prepared = _prepare(repository, chunk_count=1)
    lease = queue.claim_chunk("timed", lease_duration=timedelta(minutes=5), now=NOW)
    started = datetime_to_unix_time_ms(NOW)
    with factory.begin() as session:
        parent = session.get(VideoParseTask, task_id)
        parent.detail = {**parent.detail, "unrelated": {"keep": True}}
        parent.detail["_worker_runtime_v2"]["prefix"]["session_extension"] = {"keep": "prefix"}
        parent.detail["_worker_runtime_v2"]["model_calls"]["session_extension"] = ["keep"]
        flag_modified(parent, "detail")
        original = json.loads(json.dumps(parent.detail))
    for elapsed in (0, 1200, 1200, 800):
        assert repo.record_execution_timing(
            lease, started_at_ms=started, elapsed_ms=elapsed, now=NOW,
        )
    assert not repo.record_execution_timing(
        lease, started_at_ms=started + 1, elapsed_ms=2000, now=NOW,
    )
    assert not repo.record_execution_timing(
        replace(lease, worker_id="another-worker"),
        started_at_ms=started, elapsed_ms=2000, now=NOW,
    )
    assert repo.record_execution_timing(
        lease, started_at_ms=started, elapsed_ms=1400,
        finished=True, outcome="failed", now=NOW,
    )
    assert repo.record_execution_timing(
        lease, started_at_ms=started, elapsed_ms=9900, now=NOW,
    )
    with factory() as session:
        parent = session.get(VideoParseTask, task_id)
        timing = parent.detail["_worker_runtime_v2"]["execution_timing"]
        assert timing == {"version": 1, "entries": {
            f"understand:{lease.chunk_parse_task_id}:{lease.lease_version}": {
                "phase": "understand", "execution_kind": "chunk",
                "execution_task_id": lease.chunk_parse_task_id,
                "lease_version": lease.lease_version, "worker_id": lease.worker_id,
                "started_at_ms": started, "elapsed_ms": 1400,
                "finished": True, "outcome": "failed",
            },
        }}
        remaining = json.loads(json.dumps(parent.detail))
        del remaining["_worker_runtime_v2"]["execution_timing"]
        assert remaining == original
        assert parent.processing_duration_ms == 0


def test_execution_timing_rejects_expired_and_replaced_lease_samples(repository):
    factory, queue, repo, task_id, video_lease, prepared = _prepare(repository, chunk_count=1)
    first = queue.claim_chunk("first", lease_duration=timedelta(seconds=10), now=NOW)
    started = datetime_to_unix_time_ms(NOW)
    assert repo.record_execution_timing(first, started_at_ms=started, elapsed_ms=5000, now=NOW)
    assert not repo.record_execution_timing(
        first, started_at_ms=started, elapsed_ms=12000,
        finished=True, outcome="cancelled", now=NOW + timedelta(seconds=12),
    )
    assert queue.reap_expired_chunks(
        now=NOW + timedelta(seconds=12),
    ) == 1
    resumed_at = NOW + timedelta(seconds=12 + first.retry_initial_delay_seconds)
    second = queue.claim_chunk("second", lease_duration=timedelta(minutes=5), now=resumed_at)
    assert second is not None and second.lease_version > first.lease_version
    assert repo.record_execution_timing(
        second, started_at_ms=datetime_to_unix_time_ms(resumed_at), elapsed_ms=300, now=resumed_at,
    )
    assert not repo.record_execution_timing(
        first, started_at_ms=started, elapsed_ms=13000,
        finished=True, outcome="completed", now=NOW + timedelta(seconds=13),
    )
    with factory() as session:
        entries = session.get(VideoParseTask, task_id).detail["_worker_runtime_v2"]["execution_timing"]["entries"]
        assert entries[f"understand:{first.chunk_parse_task_id}:{first.lease_version}"]["elapsed_ms"] == 5000
        assert entries[f"understand:{second.chunk_parse_task_id}:{second.lease_version}"]["elapsed_ms"] == 300


def test_execution_timing_closes_only_an_observed_successful_chunk(repository):
    factory, queue, repo, task_id, video_lease, prepared = _prepare(repository, chunk_count=2)
    first = _commit_all_chunks(factory, queue, repo, video_lease, prepared, count=1)[0]
    started = datetime_to_unix_time_ms(NOW)
    assert not repo.record_execution_timing(
        first, started_at_ms=started, elapsed_ms=1000,
        finished=True, outcome="completed", now=NOW + timedelta(seconds=3),
    )
    second = queue.claim_chunk("second", lease_duration=timedelta(minutes=5), now=NOW + timedelta(seconds=3))
    assert second is not None
    assert repo.record_execution_timing(second, started_at_ms=started, elapsed_ms=0, now=NOW + timedelta(seconds=3))
    repo.commit_chunk_result(second, content={"persons": [], "timeline": []}, now=NOW + timedelta(seconds=4))
    assert not repo.record_execution_timing(second, started_at_ms=started, elapsed_ms=4000, now=NOW + timedelta(seconds=4))
    assert repo.record_execution_timing(
        second, started_at_ms=started, elapsed_ms=4000,
        finished=True, outcome="completed", now=NOW + timedelta(seconds=4),
    )
    with factory() as session:
        entry = session.get(VideoParseTask, task_id).detail["_worker_runtime_v2"]["execution_timing"]["entries"][
            f"understand:{second.chunk_parse_task_id}:{second.lease_version}"
        ]
        assert entry["finished"] and entry["elapsed_ms"] == 4000


def test_execution_timing_refine_final_survives_the_next_index_lease(repository):
    factory, queue, repo, task_id, video_lease, prepared = _prepare(repository, chunk_count=2)
    _commit_all_chunks(factory, queue, repo, video_lease, prepared, count=2)
    at = NOW + timedelta(minutes=1)
    refine = queue.claim_interval_refine("refiner", lease_duration=timedelta(minutes=5), now=at)
    started = datetime_to_unix_time_ms(at)
    assert repo.record_execution_timing(refine, started_at_ms=started, elapsed_ms=100, now=at)
    repo.commit_interval_refinement(
        refine, **_refinement_inputs(repo, refine, now=at),
        interval_content={"updates": []}, member_updates={}, now=at,
    )
    index = queue.claim_chunk_index("indexer", lease_duration=timedelta(minutes=5), now=at)
    assert index is not None and index.lease_version > refine.lease_version
    assert not repo.record_execution_timing(refine, started_at_ms=started, elapsed_ms=200, now=at)
    assert repo.record_execution_timing(
        refine, started_at_ms=started, elapsed_ms=300,
        finished=True, outcome="completed", now=at,
    )
    assert not repo.record_execution_timing(
        replace(refine, worker_id="stale-other-worker"), started_at_ms=started,
        elapsed_ms=9900, finished=True, outcome="completed", now=at,
    )
    assert not repo.record_execution_timing(
        replace(refine, lease_version=refine.lease_version + 20), started_at_ms=started,
        elapsed_ms=9900, finished=True, outcome="completed", now=at,
    )
    assert repo.record_execution_timing(index, started_at_ms=started, elapsed_ms=100, now=at)
    with factory() as session:
        entries = session.get(VideoParseTask, task_id).detail["_worker_runtime_v2"]["execution_timing"]["entries"]
        assert set(entries) == {
            f"refine:{refine.interval_parse_task_id}:{refine.lease_version}",
            f"index:{index.chunk_parse_task_id}:{index.lease_version}",
        }
        assert entries[f"refine:{refine.interval_parse_task_id}:{refine.lease_version}"]["elapsed_ms"] == 300
        current = session.get(ChunkParseTask, index.chunk_parse_task_id)
        assert current.lease_version == index.lease_version and current.lease_owner == "indexer"


@pytest.mark.parametrize("chunk_count", [2, 4])
@pytest.mark.parametrize("observed", [False, True])
def test_index_timing_closes_only_observed_full_or_partial_publication(repository, chunk_count, observed):
    factory, queue, repo, task_id, video_lease, prepared = _prepare(repository, chunk_count=chunk_count)
    parse_leases = _commit_all_chunks(factory, queue, repo, video_lease, prepared, count=chunk_count)
    at = NOW + timedelta(minutes=1)
    refine = queue.claim_interval_refine("refiner", lease_duration=timedelta(minutes=5), now=at)
    repo.commit_interval_refinement(refine, **_refinement_inputs(repo, refine, now=at),
                                   interval_content={"updates": []}, member_updates={}, now=at)
    index = queue.claim_chunk_index("indexer", lease_duration=timedelta(minutes=5), now=at)
    assert index.chunk_parse_task_id == prepared.chunk_parse_task_ids[0]
    started = datetime_to_unix_time_ms(at)
    if observed:
        assert repo.record_execution_timing(index, started_at_ms=started, elapsed_ms=100, now=at)
    _finish_index(repo, index, now=at)
    assert queue.is_execution_lease_committed(index)
    assert queue.is_execution_lease_committed(parse_leases[0])
    with factory() as session:
        content = session.scalar(select(ChunkParseContent).where(ChunkParseContent.chunk_task_id == index.chunk_parse_task_id))
        assert content.detail["index_committed_lease_versions"] == [index.lease_version]
        assert content.detail["parse_committed_lease_version"] == parse_leases[0].lease_version
    for stale in (replace(index, worker_id="other-worker"), replace(index, lease_version=index.lease_version + 50),
                  replace(index, video_chunk_id=str(uuid4()))):
        assert not repo.record_execution_timing(stale, started_at_ms=started, elapsed_ms=9000,
                                               finished=True, outcome="completed", now=at + timedelta(seconds=3))
    assert repo.record_execution_timing(index, started_at_ms=started, elapsed_ms=3000,
                                       finished=True, outcome="completed", now=at + timedelta(seconds=3)) is observed


@pytest.mark.parametrize("expired", [False, True])
def test_index_timing_preserves_failures_and_rejects_uncommitted_stale_leases(repository, expired):
    factory, queue, repo, task_id, video_lease, prepared = _prepare(repository, chunk_count=2)
    _commit_all_chunks(factory, queue, repo, video_lease, prepared, count=2)
    at = NOW + timedelta(minutes=1)
    refine = queue.claim_interval_refine("refiner", lease_duration=timedelta(minutes=5), now=at)
    repo.commit_interval_refinement(
        refine, **_refinement_inputs(repo, refine, now=at),
        interval_content={"updates": []}, member_updates={}, now=at,
    )
    index = queue.claim_chunk_index("indexer", lease_duration=timedelta(seconds=10), now=at)
    started = datetime_to_unix_time_ms(at)
    assert repo.record_execution_timing(index, started_at_ms=started, elapsed_ms=100, now=at)
    failed_at = at + timedelta(seconds=12 if expired else 1)
    if expired:
        assert not repo.record_execution_timing(
            index, started_at_ms=started, elapsed_ms=12000, finished=True, outcome="failed", now=failed_at,
        )
        assert queue.reap_expired_chunks(now=failed_at) == 1
    else:
        assert repo.record_execution_timing(
            index, started_at_ms=started, elapsed_ms=1000, finished=True, outcome="failed", now=failed_at,
        )
        assert queue.retry_chunk(index, error="embedding failed", retry_at=failed_at, now=failed_at)
    failed_at += timedelta(seconds=31)
    next_index = queue.claim_chunk_index("next-indexer", lease_duration=timedelta(minutes=5), now=failed_at)
    assert next_index.lease_version > index.lease_version
    snapshot = repo.load_chunk_indexing_snapshot(next_index, now=failed_at)
    _finish_index(repo,
        next_index, {chunk.chunk_parse_task_id: chunk.content_version for chunk in (snapshot.chunk,)}, now=failed_at,
    )
    assert not repo.record_execution_timing(
        index, started_at_ms=started, elapsed_ms=13000,
        finished=True, outcome="completed", now=failed_at + timedelta(seconds=1),
    )
    with factory() as session:
        entry = session.get(VideoParseTask, task_id).detail["_worker_runtime_v2"]["execution_timing"]["entries"][
            f"index:{index.chunk_parse_task_id}:{index.lease_version}"
        ]
        assert entry["elapsed_ms"] == (100 if expired else 1000)
        assert entry["finished"] is (not expired)
        assert entry["outcome"] == ("running" if expired else "failed")


@pytest.mark.parametrize("values", [
    {"started_at_ms": True}, {"started_at_ms": -1}, {"started_at_ms": 1.5},
    {"elapsed_ms": True}, {"elapsed_ms": -1}, {"elapsed_ms": 1.5},
    {"finished": 1}, {"outcome": None}, {"outcome": " "},
])
def test_execution_timing_rejects_invalid_sample_parameters(repository, values):
    _factory, queue, repo, _task_id, video_lease, prepared = _prepare(repository, chunk_count=1)
    lease = queue.claim_chunk("timed", lease_duration=timedelta(minutes=5), now=NOW)
    with pytest.raises(ValueError):
        repo.record_execution_timing(
            lease, **{"started_at_ms": 0, "elapsed_ms": 0, **values}, now=NOW,
        )


def test_refinement_preserves_concurrent_changes_in_unmodified_members(repository):
    factory, queue, repo, _task_id, video_lease, prepared = _prepare(repository)
    _commit_all_chunks(factory, queue, repo, video_lease, prepared)
    at = NOW + timedelta(minutes=1)
    first = queue.claim_interval_refine("first", lease_duration=timedelta(minutes=5), now=at)
    second = queue.claim_interval_refine("second", lease_duration=timedelta(minutes=5), now=at)
    assert first is not None and second is not None
    first_inputs, stale_inputs = _refinement_inputs(repo, first, now=at), _refinement_inputs(repo, second, now=at)
    overlap = prepared.chunk_parse_task_ids[1]
    target = prepared.chunk_parse_task_ids[3]
    repo.commit_interval_refinement(
        first, **first_inputs, interval_content={}, member_updates={overlap: {"persons": ["first writer"]}}, now=at,
    )
    repo.commit_interval_refinement(
        second, **stale_inputs, interval_content={}, member_updates={target: {"persons": ["independent writer"]}}, now=at,
    )
    with factory() as session:
        rows = {row.chunk_task_id: row for row in session.scalars(select(ChunkParseContent))}
        assert rows[overlap].content["persons"] == ["first writer"]
        assert rows[overlap].version == 2 and rows[overlap].detail["scope"] == [0, 1, 2, 3]
        assert rows[target].content["persons"] == ["independent writer"]
        assert rows[target].version == 2
        assert sum(len(row.content["timeline"]) for row in rows.values()) == 4
        assert session.get(IntervalParseContent, second.interval_parse_task_id) is not None
        assert session.get(IntervalParseTask, second.interval_parse_task_id).status == "COMPLETE"


def _overlapping_refinements(repository):
    factory, queue, repo, _task_id, _parent, prepared = _prepare(repository)
    for sequence in range(4):
        child = queue.claim_chunk("understand", lease_duration=timedelta(minutes=5), now=NOW)
        dialogue = {**_fixture_event("spoken line", sequence_no=sequence),
                    "event_type": "DIALOGUE", "speaker": "B", "participants": ["B"],
                    "text_variants": [{"language": "ja", "role": "ORIGINAL", "text": "line"}]}
        action = {**_fixture_event("simultaneous action", sequence_no=sequence),
                  "end_seconds": sequence * 10000 + 8000}
        repo.commit_chunk_result(child, content={"persons": ["A", "B"], "timeline": [dialogue, action]}, now=NOW)
    at = NOW + timedelta(minutes=1)
    first = queue.claim_interval_refine("first", lease_duration=timedelta(minutes=5), now=at)
    second = queue.claim_interval_refine("second", lease_duration=timedelta(minutes=5), now=at)
    assert first is not None and second is not None
    return factory, repo, prepared, first, second


def test_refinement_merges_independent_events_in_one_chunk(repository):
    factory, repo, prepared, first, second = _overlapping_refinements(repository)
    at = NOW + timedelta(minutes=1)
    first_inputs, second_inputs = _refinement_inputs(repo, first, now=at), _refinement_inputs(repo, second, now=at)
    target = prepared.chunk_parse_task_ids[1]
    first_timeline = deepcopy(first_inputs["input_contents"][target]["timeline"])
    second_timeline = deepcopy(second_inputs["input_contents"][target]["timeline"])
    first_timeline[0].update(speaker="A", participants=["A", "B"])
    second_timeline[1]["description"] = "independently corrected action"
    repo.commit_interval_refinement(first, **first_inputs, interval_content={},
                                   member_updates={target: {"timeline": first_timeline}}, now=at)
    repo.commit_interval_refinement(second, **second_inputs, interval_content={},
                                   member_updates={target: {"timeline": second_timeline}}, now=at)
    with factory() as session:
        row = session.scalar(select(ChunkParseContent).where(ChunkParseContent.chunk_task_id == target))
        expected = deepcopy(first_timeline)
        expected[1] = second_timeline[1]
        assert row.content == {"persons": ["A", "B"], "timeline": expected}
        assert row.version == 3 and row.detail["scope"] == [0, 1, 2, 3]
        assert session.get(IntervalParseContent, first.interval_parse_task_id) is not None
        assert session.get(IntervalParseContent, second.interval_parse_task_id) is not None


@pytest.mark.parametrize("same_field", [True, False])
def test_refinement_same_event_conflict_rolls_back_every_member(repository, same_field):
    factory, repo, prepared, first, second = _overlapping_refinements(repository)
    at = NOW + timedelta(minutes=1)
    first_inputs, second_inputs = _refinement_inputs(repo, first, now=at), _refinement_inputs(repo, second, now=at)
    target, untouched = prepared.chunk_parse_task_ids[2], prepared.chunk_parse_task_ids[1]
    current = deepcopy(first_inputs["input_contents"][target]["timeline"])
    proposed = deepcopy(second_inputs["input_contents"][target]["timeline"])
    current[0]["description"] = "first interpretation"
    proposed[0]["description" if same_field else "speaker"] = "second interpretation" if same_field else "A"
    if not same_field:
        proposed[0]["participants"] = ["A", "B"]
    repo.commit_interval_refinement(first, **first_inputs, interval_content={},
                                   member_updates={target: {"timeline": current}}, now=at)
    with factory() as session:
        before = {row.chunk_task_id: (deepcopy(row.content), row.version, deepcopy(row.detail))
                  for row in session.scalars(select(ChunkParseContent))}
    with pytest.raises(VideoParseSnapshotConflictError) as rejected:
        repo.commit_interval_refinement(second, **second_inputs, interval_content={}, now=at,
            member_updates={untouched: {"persons": ["A", "B", "must not be written"]},
                            target: {"timeline": proposed}})
    assert rejected.value.reason == "event_changed"
    assert rejected.value.context["chunk_task_id"] == target
    assert rejected.value.context["sequence_no"] == 2
    assert rejected.value.context["base_version"] == 1
    assert rejected.value.context["current_version"] == 2
    assert rejected.value.context["current_changed_fields"] == ["description"]
    assert rejected.value.context["proposed_changed_fields"] == (
        ["description"] if same_field else ["participants", "speaker"]
    )
    assert "first interpretation" not in str(rejected.value.context)
    with factory() as session:
        after = {row.chunk_task_id: (row.content, row.version, row.detail)
                 for row in session.scalars(select(ChunkParseContent))}
        assert after == before
        assert session.get(IntervalParseContent, second.interval_parse_task_id) is None
        assert session.get(IntervalParseTask, second.interval_parse_task_id).status == "REFINING"


def test_refinement_same_event_same_result_does_not_increment_version_twice(repository):
    factory, repo, prepared, first, second = _overlapping_refinements(repository)
    at = NOW + timedelta(minutes=1)
    first_inputs, second_inputs = _refinement_inputs(repo, first, now=at), _refinement_inputs(repo, second, now=at)
    target = prepared.chunk_parse_task_ids[1]
    proposed = deepcopy(first_inputs["input_contents"][target]["timeline"])
    proposed[0].update(speaker="A", participants=["A", "B"])
    for lease, inputs in ((first, first_inputs), (second, second_inputs)):
        repo.commit_interval_refinement(lease, **inputs, interval_content={},
                                       member_updates={target: {"timeline": proposed}}, now=at)
    with factory() as session:
        row = session.scalar(select(ChunkParseContent).where(ChunkParseContent.chunk_task_id == target))
        assert row.content["timeline"] == proposed
        assert row.version == 2 and row.detail["scope"] == [0, 1, 2, 3]
        assert session.get(IntervalParseContent, second.interval_parse_task_id) is not None


def test_refinement_concurrent_event_structure_changes_require_refresh(repository):
    factory, repo, prepared, first, second = _overlapping_refinements(repository)
    at = NOW + timedelta(minutes=1)
    first_inputs, second_inputs = _refinement_inputs(repo, first, now=at), _refinement_inputs(repo, second, now=at)
    target = prepared.chunk_parse_task_ids[1]
    current = deepcopy(first_inputs["input_contents"][target]["timeline"])
    proposed = deepcopy(second_inputs["input_contents"][target]["timeline"])
    current[0]["description"] = "first interpretation"
    proposed.append({**proposed[1], "start_seconds": 18500, "end_seconds": 19500})
    repo.commit_interval_refinement(first, **first_inputs, interval_content={},
                                   member_updates={target: {"timeline": current}}, now=at)
    with pytest.raises(VideoParseSnapshotConflictError):
        repo.commit_interval_refinement(second, **second_inputs, interval_content={},
                                       member_updates={target: {"timeline": proposed}}, now=at)
    with factory() as session:
        row = session.scalar(select(ChunkParseContent).where(ChunkParseContent.chunk_task_id == target))
        assert row.content["timeline"] == current and row.version == 2
        assert session.get(IntervalParseContent, second.interval_parse_task_id) is None


@pytest.mark.parametrize("invalid_proposal", [False, True])
def test_refinement_distinguishes_invalid_proposal_from_invalid_merge(repository, invalid_proposal):
    factory, repo, prepared, first, second = _overlapping_refinements(repository)
    at = NOW + timedelta(minutes=1)
    first_inputs, second_inputs = _refinement_inputs(repo, first, now=at), _refinement_inputs(repo, second, now=at)
    target = prepared.chunk_parse_task_ids[1]
    proposed = deepcopy(second_inputs["input_contents"][target]["timeline"])
    proposed[0]["speaker"] = "unknown" if invalid_proposal else "A"
    proposed[0]["participants"] = ["B", proposed[0]["speaker"]]
    repo.commit_interval_refinement(first, **first_inputs, interval_content={},
                                   member_updates={target: {"persons": ["B"]}}, now=at)
    expected = ValueError if invalid_proposal else VideoParseSnapshotConflictError
    with pytest.raises(expected) as rejected:
        repo.commit_interval_refinement(second, **second_inputs, interval_content={},
                                       member_updates={target: {"timeline": proposed}}, now=at)
    if not invalid_proposal:
        assert rejected.value.reason == "merged_validation_failed"
        assert rejected.value.context["chunk_task_id"] == target
        assert rejected.value.context["validation_errors"]
        assert all(set(error) == {"location", "type"} for error in rejected.value.context["validation_errors"])
    with factory() as session:
        row = session.scalar(select(ChunkParseContent).where(ChunkParseContent.chunk_task_id == target))
        assert row.content["persons"] == ["B"] and row.content["timeline"][0]["speaker"] == "B"
        assert row.version == 2
        assert session.get(IntervalParseContent, second.interval_parse_task_id) is None


@pytest.mark.parametrize("invalid", ["missing_member", "extra_member", "base_content", "fingerprint", "version"])
def test_refinement_rejects_forged_or_incomplete_base_identity(repository, invalid):
    factory, repo, prepared, first, _second = _overlapping_refinements(repository)
    at = NOW + timedelta(minutes=1)
    inputs = _refinement_inputs(repo, first, now=at)
    target = prepared.chunk_parse_task_ids[0]
    if invalid == "missing_member":
        inputs["input_contents"].pop(target)
    elif invalid == "extra_member":
        inputs["input_contents"][str(uuid4())] = deepcopy(inputs["input_contents"][target])
    elif invalid == "base_content":
        inputs["input_contents"][target]["persons"] = ["forged"]
    elif invalid == "fingerprint":
        inputs["input_fingerprints"][target] = "0" * 64
    else:
        inputs["input_versions"][target] = True
    with pytest.raises(VideoParseConflictError) as rejected:
        repo.commit_interval_refinement(first, **inputs, interval_content={}, member_updates={}, now=at)
    assert type(rejected.value) is VideoParseConflictError
    with factory() as session:
        assert session.get(IntervalParseContent, first.interval_parse_task_id) is None
        assert session.get(IntervalParseTask, first.interval_parse_task_id).status == "REFINING"


def test_refinement_fingerprint_and_full_result_validation_are_atomic(repository):
    factory, queue, repo, _task_id, video_lease, prepared = _prepare(repository, chunk_count=2)
    _commit_all_chunks(factory, queue, repo, video_lease, prepared, count=2)
    at = NOW + timedelta(minutes=1)
    lease = queue.claim_interval_refine("refine", lease_duration=timedelta(minutes=5), now=at)
    assert lease is not None
    stale = _refinement_inputs(repo, lease, now=at)
    target = prepared.chunk_parse_task_ids[0]
    with factory.begin() as session:
        row = session.scalar(select(ChunkParseContent).where(ChunkParseContent.chunk_task_id == target))
        row.content = {**row.content, "persons": ["intervening fact"]}
    with pytest.raises(VideoParseSnapshotConflictError) as rejected:
        repo.commit_interval_refinement(lease, **stale, interval_content={}, member_updates={}, now=at)
    assert rejected.value.reason == "member_snapshot_changed"
    assert rejected.value.context["chunk_task_id"] == target
    assert rejected.value.context["base_version"] == rejected.value.context["current_version"] == 1
    assert rejected.value.context["base_fingerprint"] != rejected.value.context["current_fingerprint"]
    current = _refinement_inputs(repo, lease, now=at)
    with pytest.raises(ValueError):
        repo.commit_interval_refinement(
            lease, **current, interval_content={}, now=at,
            member_updates={target: {"persons": ["valid first update"]},
                            prepared.chunk_parse_task_ids[1]: {"persons": ["duplicate", "duplicate"]}},
        )
    assert _refinement_inputs(repo, lease, now=at) == current
    with factory() as session:
        rows = list(session.scalars(select(ChunkParseContent)))
        assert all(row.detail["scope"] == [index] for index, row in sorted(
            ((prepared.chunk_parse_task_ids.index(row.chunk_task_id), row) for row in rows),
        ))
        assert session.get(IntervalParseContent, lease.interval_parse_task_id) is None


def test_successive_windows_preserve_previously_valid_cross_window_events(repository):
    factory, queue, repo, _task_id, video_lease, prepared = _prepare(repository)
    _commit_all_chunks(factory, queue, repo, video_lease, prepared)
    at = NOW + timedelta(minutes=1)
    first = queue.claim_interval_refine("first", lease_duration=timedelta(minutes=5), now=at)
    assert first is not None and first.start_sequence_no == 0
    retained_target = prepared.chunk_parse_task_ids[1]
    history = [_fixture_event("fact from earlier consulted scope"), _fixture_event("original fact", sequence_no=1)]
    repo.commit_interval_refinement(
        first, **_refinement_inputs(repo, first, now=at), interval_content={},
        member_updates={retained_target: {"timeline": history}}, now=at,
    )
    second = queue.claim_interval_refine("second", lease_duration=timedelta(minutes=5), now=at)
    assert second is not None and second.start_sequence_no == 1
    repo.commit_interval_refinement(
        second, **_refinement_inputs(repo, second, now=at), interval_content={},
        member_updates={prepared.chunk_parse_task_ids[3]: {"persons": ["updated member"]}}, now=at,
    )
    with factory() as session:
        content = session.scalar(select(ChunkParseContent).where(ChunkParseContent.chunk_task_id == retained_target))
        assert content.content["timeline"] == history
        assert content.detail["scope"] == [0, 1, 2, 3]
        assert content.version == 2


def test_original_36_event_canvas_result_is_committed_without_losing_other_events(repository):
    from agent.video_parse_v2.timelineEvent_canvas import ParseContextCanvas

    fixture = json.loads((Path(__file__).parents[1] / "agent/video_parse_v2/fixtures/context_canvas_refine_36.json").read_text())
    factory, task_id = _seed(repository, chunk_count=2)
    with factory.begin() as session:
        task = session.get(VideoParseTask, task_id)
        task.parse_config = {**task.parse_config, "chunk_duration": 60, "interval_duration": 180}
        session.get(Video, task.video_id).duration_ms = 90132
    queue, repo = MySqlVideoParseQueue(factory, video_concurrency=3), VideoParsePersistenceService(factory)
    lease = queue.claim_video("prepare", lease_duration=timedelta(minutes=10), now=NOW)
    chunks = repo.ensure_video_chunks(lease, [(0, 0, 60000), (1, 60000, 90132)], now=NOW)
    prepared = repo.ensure_parse_tasks(lease, [chunk.video_chunk_id for chunk in chunks], [(0, 2)], now=NOW)
    repo.activate_video_task(lease, now=NOW)
    members = {}
    for member in sorted(fixture["members"], key=lambda member: member["sequence_no"]):
        task = prepared.chunk_parse_task_ids[member["sequence_no"]]
        child = queue.claim_chunk("understand", lease_duration=timedelta(minutes=5), now=NOW)
        assert child is not None and child.chunk_parse_task_id == task
        repo.commit_chunk_result(child, content=member["content"], detail={"evidence": {"asr": None, "ocr": None}}, now=NOW)
        members[task] = {"sequence_no": member["sequence_no"], "version": 1, "bounds": member["bounds"], "content": member["content"]}
    refine = queue.claim_interval_refine("refine", lease_duration=timedelta(minutes=5), now=NOW)
    assert refine is not None
    inputs = _refinement_inputs(repo, refine, now=NOW)
    canvas = ParseContextCanvas(scope="refine:original-fixture", members=members)
    issued = {(value["member_ref"], value["source"]["position"]): ref for ref, value in canvas.items("events")}
    canvas.write(role="refine", batch_id="original-corrections", expected_revision=canvas.revision, operations=[
        {"op": "event_array", "event_ref": issued[(prepared.chunk_parse_task_ids[item["sequence_no"]], item["position"])],
         "field": "participants", "action": "add", "values": item["participants_add"]}
        for item in fixture["corrections"]
    ])
    result = canvas.interval_result()
    updates = {prepared.chunk_parse_task_ids[update.sequence_no]: update.model_dump(
        mode="json", exclude={"sequence_no"}, exclude_unset=True,
    ) for update in result.updates}
    repo.commit_interval_refinement(refine, **inputs, interval_content=result.model_dump(mode="json"), member_updates=updates, now=NOW)
    with factory() as session:
        stored = {row.chunk_task_id: row.content for row in session.scalars(select(ChunkParseContent))}
        assert sum(len(content["timeline"]) for content in stored.values()) == 36
        for reference in members:
            assert stored[reference] == canvas.result(reference).model_dump(mode="json")




def _config(*, chunk_count: int = 4, batch_size: int = 4) -> dict:
    config = default_video_parse_v2_config().model_dump(mode="json")
    config.update({
        "chunk_duration": 10,
        "interval_duration": 30,
        "batch_max_size": batch_size,
        "dispatch_interval_seconds": 0.0,
    })
    config["execution"].update({
        "max_retry_count": 2,
        "max_model_calls": 3,
    })
    return config


def _seed(repository, *, chunk_count: int = 4, accepted: bool = True, batch_size: int = 4):
    engine, _unused = repository
    factory = create_session_factory(engine)
    series_id, video_id, task_id = str(uuid4()), str(uuid4()), str(uuid4())
    config = _config(chunk_count=chunk_count, batch_size=batch_size)
    with factory.begin() as session:
        session.add(Series(
            id=series_id,
            tenant_id="mine",
            name=f"Series {series_id}",
            description=None,
            status="INDEXING",
            important_extra_data=None,
            character_bible=[],
        ))
        session.add(Video(
            id=video_id,
            series_id=series_id,
            episode_order=1,
            upload_sequence=1,
            filename="episode.mp4",
            source_uri="artifact://test/episode.mp4",
            hash="a" * 64,
            size_bytes=100,
            duration_ms=chunk_count * 10_000,
            upload_status="COMPLETED",
            parse_status="QUEUED",
            upload_progress=100,
            parse_progress=0,
        ))
        session.add(VideoParseTask(
            id=task_id,
            video_id=video_id,
            pipeline_version="video-parse-v2",
            status="PENDING",
            parse_config=config,
            chunk_count=chunk_count,
            version=1,
            next_execute_time=NOW,
            attempt_count=0,
            max_attempts=3,
            lease_version=0,
            processing_duration_ms=0,
            processing_timing_complete=True,
            is_test=True,
            is_deleted=False,
            detail={"_worker_runtime_v2": {"accepted": accepted}},
            create_time=datetime_to_unix_time_ms(NOW),
            update_time=datetime_to_unix_time_ms(NOW),
        ))
    return factory, task_id


def _prepare(repository, *, chunk_count: int = 4):
    factory, task_id = _seed(repository, chunk_count=chunk_count)
    queue = MySqlVideoParseQueue(factory, video_concurrency=3)
    repo = VideoParsePersistenceService(factory)
    lease = queue.claim_video(
        "prepare", lease_duration=timedelta(minutes=10), now=NOW
    )
    assert lease is not None
    chunks = repo.ensure_video_chunks(
        lease,
        [(index, index * 10_000, (index + 1) * 10_000) for index in range(chunk_count)],
        now=NOW,
    )
    interval_specs = ((0, chunk_count),) if chunk_count < 3 else tuple(
        (start, 3) for start in range(chunk_count - 2)
    )
    prepared = repo.ensure_parse_tasks(
        lease,
        [chunk.video_chunk_id for chunk in chunks],
        interval_specs,
        now=NOW,
    )
    repo.activate_video_task(lease, now=NOW)
    return factory, queue, repo, task_id, lease, prepared


def test_worker_rejects_task_without_frozen_index_generation(repository):
    factory, _, repo, task_id, _, _ = _prepare(repository, chunk_count=1)
    with factory.begin() as session:
        task = session.get(VideoParseTask, task_id)
        task.parse_config["indexing"].pop("index_generation")
        flag_modified(task, "parse_config")
    with pytest.raises(VideoParseConflictError, match="frozen index generation"):
        repo.load_task_input(task_id)


def test_preparation_preserves_fractional_last_chunk_boundary(repository):
    factory, task_id = _seed(repository, chunk_count=1)
    with factory.begin() as session:
        task = session.get(VideoParseTask, task_id)
        session.get(Video, task.video_id).duration_ms = 8008
    queue = MySqlVideoParseQueue(factory, video_concurrency=3)
    repo = VideoParsePersistenceService(factory)
    lease = queue.claim_video("prepare-ms", lease_duration=timedelta(minutes=10), now=NOW)
    assert lease is not None
    with pytest.raises(VideoParseConflictError, match="frozen parse config"):
        repo.ensure_video_chunks(lease, [(0, 0, 8)], now=NOW)
    chunks = repo.ensure_video_chunks(lease, [(0, 0, 8008)], now=NOW)
    assert [(chunk.start_seconds, chunk.end_seconds) for chunk in chunks] == [(0, 8008)]


def _commit_all_chunks(factory, queue, repo, video_lease, prepared, *, count: int = 4):
    leases = []
    for index, task_id in enumerate(prepared.chunk_parse_task_ids[:count]):
        lease = queue.claim_chunk(
            f"chunk-{index}", lease_duration=timedelta(minutes=10), now=NOW + timedelta(seconds=index)
        )
        assert lease is not None and lease.chunk_parse_task_id == task_id
        repo.commit_chunk_result(
            lease,
            content={"persons": [], "timeline": [_fixture_event(index, sequence_no=index)]},
            detail={"source": "fixture"},
            now=NOW + timedelta(seconds=index + 1),
        )
        leases.append(lease)
    return leases


def _prepare_two_chunk_prefix_chain(repository):
    factory, queue, repo, task_id, video_lease, prepared = _prepare(
        repository, chunk_count=2
    )
    with factory.begin() as session:
        task = session.get(VideoParseTask, task_id)
        config = dict(task.parse_config)
        config["compact_enabled"] = True
        config["prefix_context"] = {
            "series_compact_max_chars": 1000,
            "video_compact_max_chars": 1000,
            "max_tool_calls": 1,
        }
        task.parse_config = config
    _commit_all_chunks(factory, queue, repo, video_lease, prepared, count=2)
    refine = queue.claim_interval_refine(
        "refine", lease_duration=timedelta(minutes=5), now=NOW + timedelta(minutes=1)
    )
    assert refine is not None
    repo.commit_interval_refinement(
        refine,
        **_refinement_inputs(repo, refine, now=NOW + timedelta(minutes=1, seconds=1)), interval_content={"summary": "short"},
        member_updates={},
        now=NOW + timedelta(minutes=1, seconds=1),
    )
    _finish_ready_indexes(queue, repo, now=NOW + timedelta(minutes=1, seconds=3))
    assert repo.aggregate_task(
        video_lease, now=NOW + timedelta(minutes=1, seconds=4)
    ).status == "COMPLETE"
    return factory, queue, repo, task_id


def test_compact_only_prefix_preserves_legal_cross_chunk_refinement_events(repository):
    from agent.video_parse_v2.timelineEvent_canvas import ParseContextCanvas
    from tests.agent.video_parse_v2.test_framework import _inputs, _lease

    factory, _queue, repo, task_id = _prepare_two_chunk_prefix_chain(repository)
    with factory.begin() as session:
        row = session.scalar(select(ChunkParseContent).join(ChunkParseTask).join(VideoChunk).where(
            ChunkParseTask.video_task_id == task_id, VideoChunk.sequence_no == 0,
        ))
        row.content = {**row.content, "timeline": [*row.content["timeline"], _fixture_event("cross boundary", sequence_no=1)]}
        row.version += 1
    at = NOW + timedelta(minutes=2)
    prefix = repo.claim_next_prefix_any("prefix", lease_duration=timedelta(minutes=5), now=at)
    assert prefix is not None and prefix.sequence_no == 0
    assert prefix.content_detail["scope"] == [0, 1]
    canvas = ParseContextCanvas.from_prefix({
        "chunk_parse_task_id": prefix.chunk_parse_task_id, "video_parse_task_id": task_id,
        "inputs": _inputs(compact_enabled=True), "lease": _lease(), "sequence_no": 0,
        "allowed_start_seconds": 0, "allowed_end_seconds": 20_000,
        "scope": [0, 1], "content": VideoParseResult.model_validate(prefix.content),
        "content_version": prefix.content_version, "series_prefix_compact": "", "video_prefix_compact": "",
    })
    canvas.write(role="prefix", batch_id="summaries", expected_revision=canvas.revision, operations=[
        {"op": "set_compact", "series_compact": "same facts", "video_compact": "cross-boundary facts retained"},
    ])
    result = canvas.prefix_result()
    assert result.update is None
    repo.commit_prefix(prefix, content_updates={}, series_prefix_compact=result.series_compact,
                       video_prefix_compact=result.video_compact, now=at)
    with factory() as session:
        stored = session.scalar(select(ChunkParseContent).where(ChunkParseContent.chunk_task_id == prefix.chunk_parse_task_id))
        assert stored.content == prefix.content
        assert stored.version == prefix.content_version


def test_prepare_is_idempotent_and_short_video_gets_one_interval(repository):
    factory, queue, repo, task_id, _lease, prepared = _prepare(repository, chunk_count=2)
    assert repo.load_task_input(task_id).tenant_id == "mine"
    assert len(prepared.chunk_parse_task_ids) == 2
    assert len(prepared.interval_parse_task_ids) == 1
    with factory() as session:
        interval = session.get(IntervalParseTask, prepared.interval_parse_task_ids[0])
        assert (interval.start_sequence_no, interval.chunk_count) == (0, 2)
        assert interval.chunk_task_ids == list(prepared.chunk_parse_task_ids)
    assert queue.claim_video(
        "another", lease_duration=timedelta(minutes=1), now=NOW
    ) is None


def test_chunk_commit_masks_refine_scope_union_and_index_wait_are_atomic(repository):
    factory, queue, repo, task_id, video_lease, prepared = _prepare(repository)
    _commit_all_chunks(factory, queue, repo, video_lease, prepared)
    at = NOW + timedelta(minutes=1)
    first = queue.claim_interval_refine("refine-0", lease_duration=timedelta(minutes=10), now=at)
    repo.commit_interval_refinement(first, **_refinement_inputs(repo, first, now=at),
        interval_content={"summary": "first"}, member_updates={prepared.chunk_parse_task_ids[1]: {"persons": ["A"]}}, now=at)
    with factory() as session:
        rows = [session.get(ChunkParseTask, id_) for id_ in prepared.chunk_parse_task_ids]
        assert [(row.phase, row.status) for row in rows] == [(3, "QUEUED"), (2, "WAITING"), (2, "WAITING"), (2, "WAITING")]
        assert session.get(IntervalParseTask, first.interval_parse_task_id).status == "COMPLETE"
    index = queue.claim_chunk_index("index-0", lease_duration=timedelta(minutes=5), now=at)
    snapshot = repo.load_chunk_indexing_snapshot(index, now=at)
    chunk = snapshot.chunk
    receipt = {"parse_task_id": task_id, "video_chunk_id": chunk.video_chunk_id,
               "content_version": chunk.content_version, "index_generation": 1}
    for channel in ("semantic", "vision"):
        repo.save_index_embeddings(index, chunk.chunk_parse_task_id, chunk.content_version,
                                   {channel: {"vector": [1.0]}}, receipt=receipt, now=at)
    for invalid in ({**receipt, "content_version": True}, {**receipt, "index_generation": True},
                    {**receipt, "index_generation": 0}, {**receipt, "parse_task_id": str(uuid4())}):
        with pytest.raises(VideoParseConflictError):
            repo.save_index_embeddings(index, chunk.chunk_parse_task_id, chunk.content_version, {}, receipt=invalid, now=at)
    with pytest.raises(VideoParseConflictError, match="generation"):
        repo.save_index_embeddings(index, chunk.chunk_parse_task_id, chunk.content_version, {},
                                   receipt={**receipt, "index_generation": 2}, now=at)
    _finish_index(repo, index, now=at)
    second = queue.claim_interval_refine("refine-1", lease_duration=timedelta(minutes=10), now=at)
    repo.commit_interval_refinement(second, **_refinement_inputs(repo, second, now=at),
        interval_content={"summary": "second"},
        member_updates={prepared.chunk_parse_task_ids[1]: {"timeline": [_fixture_event("changed", sequence_no=1)]}}, now=at)
    with factory() as session:
        rows = [session.get(ChunkParseTask, id_) for id_ in prepared.chunk_parse_task_ids]
        assert [(row.phase, row.status) for row in rows] == [(3, "COMPLETE"), (3, "QUEUED"), (3, "QUEUED"), (3, "QUEUED")]
        overlap = session.scalar(select(ChunkParseContent).where(ChunkParseContent.chunk_task_id == prepared.chunk_parse_task_ids[1]))
        assert overlap.detail["scope"] == [0, 1, 2, 3] and overlap.version == 3
    _finish_ready_indexes(queue, repo, now=at)
    assert repo.aggregate_task(video_lease, now=at).status == "COMPLETE"
    assert queue.is_execution_lease_committed(video_lease)


def test_stale_chunk_lease_cannot_commit(repository):
    _factory, queue, repo, _task_id, video_lease, prepared = _prepare(repository, chunk_count=2)
    task_id = prepared.chunk_parse_task_ids[0]
    lease = queue.claim_chunk(
        "old", lease_duration=timedelta(seconds=5), now=NOW
    )
    assert lease is not None and lease.chunk_parse_task_id == task_id
    assert queue.reap_expired_chunks(now=NOW + timedelta(seconds=6)) == 1
    resumed_at = NOW + timedelta(seconds=6 + lease.retry_initial_delay_seconds)
    replacement = queue.claim_chunk(
        "new", lease_duration=timedelta(minutes=1), now=resumed_at
    )
    assert replacement is not None and replacement.lease_version > lease.lease_version
    assert replacement.chunk_parse_task_id == lease.chunk_parse_task_id
    with pytest.raises((VideoParseLeaseLostError, VideoParseTaskNotFoundError)):
        repo.commit_chunk_result(
            lease,
            content={"persons": [], "timeline": []},
            now=resumed_at + timedelta(seconds=1),
        )
    repo.commit_chunk_result(
        replacement,
        content={"persons": [], "timeline": []},
        now=resumed_at + timedelta(seconds=1),
    )


@pytest.mark.parametrize("phase", ["chunk", "refine", "index"])
def test_failure_waits_for_inflight_result_and_preserves_prefix_runtime(repository, phase):
    factory, queue, repo, task_id, video_lease, prepared = _prepare(repository)
    claimed_at = NOW + timedelta(minutes=1)
    if phase == "chunk":
        with factory.begin() as session:
            session.get(ChunkParseTask, prepared.chunk_parse_task_ids[0]).next_execute_time = (
                claimed_at + timedelta(minutes=1)
            )
        inflight = queue.claim_chunk(
            "inflight-chunk", lease_duration=timedelta(minutes=5), now=claimed_at
        )
        assert inflight is not None and inflight.chunk_parse_task_id == prepared.chunk_parse_task_ids[1]
    else:
        _commit_all_chunks(factory, queue, repo, video_lease, prepared)
        inflight = queue.claim_interval_refine(
            "inflight-refine", lease_duration=timedelta(minutes=5), now=claimed_at
        )
        if phase == "index":
            for index in range(2):
                assert inflight is not None
                repo.commit_interval_refinement(
                    inflight,
                    **_refinement_inputs(repo, inflight, now=claimed_at), interval_content={"summary": f"interval-{index}"},
                    member_updates={},
                    now=claimed_at,
                )
                inflight = queue.claim_interval_refine(
                    "next-refine", lease_duration=timedelta(minutes=5), now=claimed_at
                )
            inflight = queue.claim_chunk_index(
                "inflight-index", lease_duration=timedelta(minutes=5), now=claimed_at
            )
    assert inflight is not None
    failure_status = "INDEXING_FAILED" if phase == "index" else "PARSING_FAILED"
    with factory.begin() as session:
        task = session.get(VideoParseTask, task_id)
        prefix = {
            **task.detail["_worker_runtime_v2"]["prefix"],
            "lease_owner": "independent-prefix",
            "lease_expire_time_ms": int((NOW + timedelta(minutes=20)).timestamp() * 1000),
            "lease_version": 7,
        }
        task.detail = {
            **task.detail,
            "_worker_runtime_v2": {
                **task.detail["_worker_runtime_v2"],
                "model_calls": {"used_call_ids": ["call-1", "call-2"], "active": {}},
                "prefix": prefix,
            },
        }
        if phase == "chunk":
            session.get(ChunkParseTask, prepared.chunk_parse_task_ids[0]).status = "FAILED"
        elif phase == "index":
            failed_id = next(child_id for child_id in prepared.chunk_parse_task_ids if child_id != inflight.chunk_parse_task_id)
            session.get(ChunkParseTask, failed_id).status = "FAILED"
        else:
            failed_id = next(
                child_id for child_id in prepared.interval_parse_task_ids
                if child_id != inflight.interval_parse_task_id
            )
            session.get(IntervalParseTask, failed_id).status = failure_status

    draining = repo.aggregate_task(video_lease, now=claimed_at)
    assert draining.status == ("INDEXING" if phase == "index" else "PARSING")
    with factory() as session:
        parent = session.get(VideoParseTask, task_id)
        assert parent.finish_time is None
        assert parent.lease_owner == video_lease.worker_id
        assert parent.detail["_worker_runtime_v2"]["failure_status"] == failure_status
        assert session.get(Video, parent.video_id).parse_status == draining.status

    committed_at = claimed_at + timedelta(seconds=1)
    if phase == "chunk":
        assert queue.heartbeat_chunk(
            inflight, lease_duration=timedelta(minutes=5), now=committed_at
        )
        repo.commit_chunk_result(
            inflight,
            content={"persons": [], "timeline": [_fixture_event("retained-inflight", sequence_no=1)]},
            now=committed_at,
        )
    elif phase == "refine":
        assert queue.heartbeat_interval(
            inflight, lease_duration=timedelta(minutes=5), now=committed_at
        )
        repo.commit_interval_refinement(
            inflight,
            **_refinement_inputs(repo, inflight, now=committed_at), interval_content={"summary": "retained-inflight"},
            member_updates={},
            now=committed_at,
        )
    else:
        assert queue.heartbeat_chunk(
            inflight, lease_duration=timedelta(minutes=5), now=committed_at
        )
        snapshot = repo.load_chunk_indexing_snapshot(inflight, now=committed_at)
        _finish_index(repo,
            inflight,
            {item.chunk_parse_task_id: item.content_version for item in (snapshot.chunk,)},
            now=committed_at,
        )

    assert repo.aggregate_task(video_lease, now=committed_at).status == failure_status
    with factory() as session:
        parent = session.get(VideoParseTask, task_id)
        assert parent.finish_time is not None
        assert parent.lease_owner is None
        runtime = parent.detail["_worker_runtime_v2"]
        assert runtime["failure_status"] == failure_status
        assert runtime["prefix"] == prefix
        assert runtime["model_calls"] == {"used_call_ids": ["call-1", "call-2"], "active": {}}
        if phase == "chunk":
            content = session.scalar(select(ChunkParseContent).where(
                ChunkParseContent.chunk_task_id == inflight.chunk_parse_task_id
            ))
            assert content.content["timeline"] == [_fixture_event("retained-inflight", sequence_no=1)]
        elif phase == "refine":
            content = session.get(IntervalParseContent, inflight.interval_parse_task_id)
            assert content.content == {"summary": "retained-inflight"}
        else:
            assert session.get(
                ChunkParseTask, inflight.chunk_parse_task_id
            ).status == "COMPLETE"


def test_failure_does_not_treat_expired_but_unreleased_child_as_drained(repository):
    factory, _queue, repo, task_id, video_lease, prepared = _prepare(repository, chunk_count=2)
    with factory.begin() as session:
        session.get(ChunkParseTask, prepared.chunk_parse_task_ids[0]).status = "FAILED"
        unfinished = session.get(ChunkParseTask, prepared.chunk_parse_task_ids[1])
        unfinished.status = "RUNNING"
        unfinished.start_time = NOW
        unfinished.lease_owner = "expired-worker"
        unfinished.lease_expire_time = NOW

    assert repo.aggregate_task(video_lease, now=NOW + timedelta(seconds=1)).status == "PARSING"
    with factory() as session:
        parent = session.get(VideoParseTask, task_id)
        assert parent.finish_time is None
        assert parent.detail["_worker_runtime_v2"]["failure_status"] == "PARSING_FAILED"

    with factory.begin() as session:
        unfinished = session.get(ChunkParseTask, prepared.chunk_parse_task_ids[1])
        unfinished.status = "QUEUED"
        unfinished.lease_owner = None
        unfinished.lease_expire_time = None
    assert repo.aggregate_task(
        video_lease, now=NOW + timedelta(seconds=2)
    ).status == "PARSING_FAILED"


def test_chunk_lease_rejects_forged_parent_and_media_identity(repository):
    _factory, queue, repo, _task_id, video_lease, prepared = _prepare(
        repository, chunk_count=2
    )
    lease = queue.claim_chunk(
        "chunk-identity", lease_duration=timedelta(minutes=5), now=NOW
    )
    assert lease is not None

    forged_leases = (
        replace(lease, video_parse_task_id=str(uuid4())),
        replace(lease, video_chunk_id=str(uuid4())),
    )
    for forged in forged_leases:
        with pytest.raises((VideoParseLeaseLostError, VideoParseTaskNotFoundError)):
            repo.load_chunk_execution(forged, now=NOW)
        with pytest.raises((VideoParseLeaseLostError, VideoParseTaskNotFoundError)):
            repo.commit_chunk_result(
                forged,
                content={"persons": [], "timeline": []},
                now=NOW,
            )

    repo.commit_chunk_result(
        lease,
        content={"persons": [], "timeline": []},
        now=NOW,
    )


def test_interval_lease_rejects_forged_parent_phase_members_and_range(
    repository,
):
    _factory, queue, repo, _task_id, video_lease, prepared = _prepare(
        repository, chunk_count=2
    )
    _commit_all_chunks(
        _factory,
        queue,
        repo,
        video_lease,
        prepared,
        count=2,
    )
    lease = queue.claim_interval_refine(
        "interval-identity",
        lease_duration=timedelta(minutes=5),
        now=NOW + timedelta(minutes=1),
    )
    assert lease is not None

    forged_leases = (
        replace(lease, video_parse_task_id=str(uuid4())),
        replace(lease, phase="INDEXING"),
        replace(
            lease,
            chunk_parse_task_ids=(
                str(uuid4()),
                *lease.chunk_parse_task_ids[1:],
            ),
        ),
        replace(lease, start_sequence_no=lease.start_sequence_no + 1),
        replace(lease, chunk_count=lease.chunk_count + 1),
    )
    for forged in forged_leases:
        with pytest.raises((VideoParseLeaseLostError, VideoParseTaskNotFoundError)):
            repo.load_interval_members(
                forged,
                now=NOW + timedelta(minutes=1),
            )
        with pytest.raises((VideoParseLeaseLostError, VideoParseTaskNotFoundError)):
            repo.commit_interval_refinement(
                forged,
                **_refinement_inputs(repo, lease, now=NOW + timedelta(minutes=1)), interval_content={"summary": "forged"},
                member_updates={},
                now=NOW + timedelta(minutes=1),
            )

    repo.commit_interval_refinement(
        lease,
        **_refinement_inputs(repo, lease, now=NOW + timedelta(minutes=1)), interval_content={"summary": "valid"},
        member_updates={},
        now=NOW + timedelta(minutes=1),
    )


def test_content_version_fences_late_index_completion(repository):
    _factory, queue, repo, _task_id, video_lease, prepared = _prepare(repository, chunk_count=2)
    _commit_all_chunks(_factory, queue, repo, video_lease, prepared, count=2)
    refine = queue.claim_interval_refine(
        "refine", lease_duration=timedelta(minutes=5), now=NOW + timedelta(minutes=1)
    )
    assert refine is not None
    repo.commit_interval_refinement(
        refine,
        **_refinement_inputs(repo, refine, now=NOW + timedelta(minutes=1, seconds=1)), interval_content={"summary": "short"},
        member_updates={},
        now=NOW + timedelta(minutes=1, seconds=1),
    )
    indexing = queue.claim_chunk_index(
        "index", lease_duration=timedelta(minutes=5), now=NOW + timedelta(minutes=1, seconds=2)
    )
    assert indexing is not None
    snapshot = repo.load_chunk_indexing_snapshot(indexing, now=NOW + timedelta(minutes=1, seconds=2))
    wrong = {(snapshot.chunk,)[0].chunk_parse_task_id: (snapshot.chunk,)[0].content_version + 1}
    with pytest.raises(VideoParseConflictError, match="content.*changed"):
        _finish_index(repo,
            indexing, wrong, now=NOW + timedelta(minutes=1, seconds=3)
        )


def test_index_rebuild_preserves_results_and_fences_the_old_index_lease(repository):
    factory, queue, repo, task_id = _prepare_two_chunk_prefix_chain(repository)
    at = NOW + timedelta(minutes=2)
    with factory() as session:
        child_ids = set(session.scalars(select(ChunkParseTask.id).where(ChunkParseTask.video_task_id == task_id)))
        attempts = session.get(VideoParseTask, task_id).attempt_count
        contents = {row.chunk_task_id: (row.content, row.version, row.detail) for row in session.scalars(select(ChunkParseContent))}
        refines = {row.interval_task_id: row.content for row in session.scalars(select(IntervalParseContent))}
    assert set(repo.request_index_rebuild(task_id, now=at)) == child_ids
    assert repo.request_index_rebuild(task_id, now=at) == ()
    coordinator = queue.claim_video("coordinator", lease_duration=timedelta(minutes=5), now=at)
    old = queue.claim_chunk_index("old-index", lease_duration=timedelta(minutes=5), now=at)
    assert repo.request_index_rebuild(task_id, now=at) == (old.chunk_parse_task_id,)
    with pytest.raises((VideoParseLeaseLostError, VideoParseTaskNotFoundError)):
        _finish_index(repo, old, now=at)
    replacement = queue.claim_chunk_index("replacement", lease_duration=timedelta(minutes=5), now=at)
    assert replacement.lease_version > old.lease_version
    with factory() as session:
        assert session.get(VideoParseTask, task_id).attempt_count == attempts + 1
        assert {row.chunk_task_id: (row.content, row.version, row.detail) for row in session.scalars(select(ChunkParseContent))} == contents
        assert {row.interval_task_id: row.content for row in session.scalars(select(IntervalParseContent))} == refines
        assert all(row.status == "COMPLETE" for row in session.scalars(select(IntervalParseTask)))
    _finish_index(repo, replacement, now=at)
    _finish_ready_indexes(queue, repo, now=at)
    assert repo.aggregate_task(coordinator, now=at).status == "COMPLETE"


def test_index_rebuild_rejects_nonlatest_and_deleted_tasks(repository):
    factory, _queue, repo, task_id, _video_lease, _prepared = _prepare(
        repository, chunk_count=2
    )
    newer_id = str(uuid4())
    with factory.begin() as session:
        current = session.get(VideoParseTask, task_id)
        session.add(VideoParseTask(
            id=newer_id,
            video_id=current.video_id,
            pipeline_version=current.pipeline_version,
            status="PENDING",
            parse_config=current.parse_config,
            chunk_count=current.chunk_count,
            version=current.version + 1,
            next_execute_time=NOW,
            attempt_count=0,
            max_attempts=current.max_attempts,
            lease_version=0,
            processing_duration_ms=0,
            processing_timing_complete=True,
            is_test=True,
            is_deleted=False,
            detail={"_worker_runtime_v2": {"accepted": True}},
            create_time=datetime_to_unix_time_ms(NOW + timedelta(seconds=1)),
            update_time=datetime_to_unix_time_ms(NOW + timedelta(seconds=1)),
        ))

    with pytest.raises(VideoParseConflictError, match="latest"):
        repo.request_index_rebuild(task_id, now=NOW + timedelta(minutes=1))

    with factory.begin() as session:
        session.get(VideoParseTask, newer_id).is_deleted = True
    with pytest.raises(VideoParseTaskNotFoundError):
        repo.request_index_rebuild(newer_id, now=NOW + timedelta(minutes=1))


@pytest.mark.parametrize("has_failure_intent", [False, True])
def test_index_rebuild_clears_only_settled_index_failure_intent(repository, has_failure_intent):
    factory, _queue, repo, task_id = _prepare_two_chunk_prefix_chain(repository)
    with factory.begin() as session:
        task = session.get(VideoParseTask, task_id)
        task.status = "INDEXING_FAILED"
        runtime = {
            **task.detail["_worker_runtime_v2"],
            "model_calls": {"used_call_ids": ["call-1", "call-2"], "active": {}},
            "prefix": {**task.detail["_worker_runtime_v2"]["prefix"], "lease_version": 7},
        }
        if has_failure_intent:
            runtime["failure_status"] = "INDEXING_FAILED"
        task.detail = {**task.detail, "_worker_runtime_v2": runtime}
        interval = session.scalar(select(IntervalParseTask).where(
            IntervalParseTask.video_task_id == task_id
        ))
        children = list(session.scalars(select(ChunkParseTask).where(ChunkParseTask.video_task_id == task_id)))
        children[0].status = "FAILED"
        child_ids = {child.id for child in children}
        interval_id = interval.id
        content = session.get(IntervalParseContent, interval_id).content

    assert set(repo.request_index_rebuild(
        task_id, now=NOW + timedelta(minutes=2)
    )) == child_ids
    with factory() as session:
        parent = session.get(VideoParseTask, task_id)
        assert parent.status == "INDEXING"
        assert parent.finish_time is None
        assert parent.detail["_worker_runtime_v2"] == {
            key: value for key, value in runtime.items() if key != "failure_status"
        }
        assert session.get(IntervalParseContent, interval_id).content == content


@pytest.mark.parametrize(
    ("failure_status", "owner", "expires"),
    [
        ("PARSING_FAILED", None, None),
        ("INDEXING_FAILED", "inflight-index", NOW + timedelta(minutes=5)),
        ("INDEXING_FAILED", "expired-index", NOW),
    ],
)
def test_index_rebuild_rejects_parsing_failure_and_unfinished_failure_drain(
    repository, failure_status, owner, expires,
):
    factory, _queue, repo, task_id = _prepare_two_chunk_prefix_chain(repository)
    with factory.begin() as session:
        task = session.get(VideoParseTask, task_id)
        task.status = "INDEXING" if owner is not None else failure_status
        task.detail = {
            **task.detail,
            "_worker_runtime_v2": {
                **task.detail["_worker_runtime_v2"],
                "failure_status": failure_status,
            },
        }
        interval = session.scalar(select(ChunkParseTask).where(
            ChunkParseTask.video_task_id == task_id
        ))
        interval.status = "RUNNING" if owner is not None else "FAILED"
        interval.start_time = NOW if owner else None
        interval.lease_owner = owner
        interval.lease_expire_time = expires
        interval_id = interval.id
        before = (
            task.status, task.detail, interval.status,
            interval.lease_owner, interval.lease_version,
        )

    with pytest.raises(VideoParseConflictError):
        repo.request_index_rebuild(task_id, now=NOW + timedelta(minutes=2))
    with factory() as session:
        task = session.get(VideoParseTask, task_id)
        interval = session.get(ChunkParseTask, interval_id)
        assert (
            task.status, task.detail, interval.status,
            interval.lease_owner, interval.lease_version,
        ) == before


def test_model_call_history_and_concurrency_survive_worker_reentry(repository):
    factory, _queue, repo, task_id, lease, _prepared = _prepare(repository, chunk_count=2)
    # The legacy snapshot value remains visible but never caps cumulative calls.
    for number in range(1, 5):
        reservation = repo.reserve_model_call(lease, f"call-{number}", lease_duration=timedelta(minutes=1), now=NOW)
        assert (reservation.used_calls, reservation.active_calls) == (number, number)
        assert reservation.first_reservation and reservation.max_calls == 3
    repeated = repo.reserve_model_call(lease, "call-1", lease_duration=timedelta(minutes=1), now=NOW + timedelta(seconds=30))
    assert (repeated.used_calls, repeated.active_calls) == (4, 4) and not repeated.first_reservation
    assert repo.renew_model_call(lease, "call-1", lease_duration=timedelta(minutes=1), now=NOW + timedelta(seconds=30))
    assert not repo.renew_model_call(lease, "call-1", lease_duration=timedelta(minutes=1), now=NOW + timedelta(minutes=2))
    repeated = repo.reserve_model_call(lease, "call-1", lease_duration=timedelta(minutes=1), now=NOW + timedelta(minutes=2))
    assert (repeated.used_calls, repeated.active_calls) == (4, 1) and not repeated.first_reservation
    assert repo.release_model_call(task_id, "call-1", lease.worker_id, now=NOW + timedelta(minutes=2))
    reentered = VideoParsePersistenceService(factory)
    repeated = reentered.reserve_model_call(lease, "call-1", lease_duration=timedelta(minutes=1), now=NOW + timedelta(minutes=2))
    assert (repeated.used_calls, repeated.active_calls) == (4, 1) and not repeated.first_reservation
    next_call = reentered.reserve_model_call(lease, "call-5", lease_duration=timedelta(minutes=1), now=NOW + timedelta(minutes=2))
    assert (next_call.used_calls, next_call.active_calls, next_call.max_calls) == (5, 2, 3)
    assert next_call.first_reservation
    with pytest.raises((VideoParseLeaseLostError, VideoParseTaskNotFoundError)):
        repo.reserve_model_call(replace(lease, worker_id="stale"), "call-1", lease_duration=timedelta(minutes=1), now=NOW)
    with factory() as session:
        calls = session.get(VideoParseTask, task_id).detail["_worker_runtime_v2"]["model_calls"]
        assert calls["used_call_ids"] == ["call-1", "call-2", "call-3", "call-4", "call-5"]


def test_chunk_and_interval_model_slots_fence_the_actual_execution_lease(repository):
    factory, queue, repo, task_id, video_lease, prepared = _prepare(
        repository, chunk_count=2
    )
    chunk_lease = queue.claim_chunk(
        "chunk-model", lease_duration=timedelta(minutes=5), now=NOW
    )
    assert chunk_lease is not None
    chunk_slot = repo.reserve_model_call(
        chunk_lease,
        "chunk-call",
        lease_duration=timedelta(minutes=1),
        now=NOW,
    )
    assert chunk_slot.first_reservation
    with pytest.raises(ModelCallLeaseConflictError):
        repo.reserve_model_call(
            video_lease, "chunk-call", lease_duration=timedelta(minutes=1), now=NOW,
        )
    with pytest.raises(VideoParseLeaseLostError):
        repo.reserve_model_call(
            replace(chunk_lease, lease_version=chunk_lease.lease_version + 1),
            "stale-chunk-call", lease_duration=timedelta(minutes=1), now=NOW,
        )
    assert repo.renew_model_call(
        chunk_lease,
        "chunk-call",
        lease_duration=timedelta(minutes=1),
        now=NOW + timedelta(seconds=30),
    )
    assert queue.is_execution_lease_active(
        task_id,
        "chunk",
        chunk_lease.chunk_parse_task_id,
        chunk_lease.lease_version,
        now=NOW + timedelta(seconds=30),
    )
    assert not queue.is_execution_lease_committed(chunk_lease)
    assert repo.release_model_call(
        task_id,
        "chunk-call",
        chunk_lease.worker_id,
        now=NOW + timedelta(seconds=31),
    )
    repo.commit_chunk_result(
        chunk_lease,
        content={"persons": [], "timeline": []},
        now=NOW + timedelta(seconds=32),
    )
    assert queue.is_execution_lease_committed(chunk_lease)

    second = queue.claim_chunk(
        "chunk-second",
        lease_duration=timedelta(minutes=5),
        now=NOW + timedelta(seconds=33),
    )
    assert second is not None
    repo.commit_chunk_result(
        second,
        content={"persons": [], "timeline": []},
        now=NOW + timedelta(seconds=34),
    )

    interval_lease = queue.claim_interval_refine(
        "interval-model",
        lease_duration=timedelta(minutes=5),
        now=NOW + timedelta(minutes=1),
    )
    assert interval_lease is not None
    interval_slot = repo.reserve_model_call(
        interval_lease,
        "interval-call",
        lease_duration=timedelta(minutes=1),
        now=NOW + timedelta(minutes=1),
    )
    assert interval_slot.first_reservation
    assert repo.renew_model_call(
        interval_lease,
        "interval-call",
        lease_duration=timedelta(minutes=1),
        now=NOW + timedelta(minutes=1, seconds=30),
    )
    assert queue.is_execution_lease_active(
        task_id,
        "interval",
        interval_lease.interval_parse_task_id,
        interval_lease.lease_version,
        now=NOW + timedelta(minutes=1, seconds=30),
    )
    assert not queue.is_execution_lease_committed(interval_lease)
    assert repo.release_model_call(
        task_id,
        "interval-call",
        interval_lease.worker_id,
        now=NOW + timedelta(minutes=1, seconds=31),
    )
    repo.commit_interval_refinement(
        interval_lease,
        **_refinement_inputs(repo, interval_lease, now=NOW + timedelta(minutes=1, seconds=32)), interval_content={"summary": "committed"},
        member_updates={},
        now=NOW + timedelta(minutes=1, seconds=32),
    )
    assert queue.is_execution_lease_committed(interval_lease)
    index_lease = queue.claim_chunk_index(
        "interval-index",
        lease_duration=timedelta(minutes=5),
        now=NOW + timedelta(minutes=1, seconds=33),
    )
    assert index_lease is not None
    # The refinement's exact committed marker survives the next phase claim,
    # which increments the Interval row's current lease version.
    assert queue.is_execution_lease_committed(interval_lease)
    snapshot = repo.load_chunk_indexing_snapshot(
        index_lease, now=NOW + timedelta(minutes=1, seconds=33)
    )
    _finish_index(repo,
        index_lease,
        {
            item.chunk_parse_task_id: item.content_version
            for item in (snapshot.chunk,)
        },
        now=NOW + timedelta(minutes=1, seconds=34),
    )
    assert queue.is_execution_lease_committed(index_lease)


def test_v2_snapshots_always_expose_the_whole_video_media_source(repository):
    factory, queue, repo, _task_id, video_lease, prepared = _prepare(
        repository, chunk_count=2
    )
    proxy_ref = "artifact://test/whole-video-proxy.mp4"
    with factory.begin() as session:
        video = session.get(Video, video_lease.video_id)
        video.proxy_uri = proxy_ref
        chunks = list(session.scalars(
            select(VideoChunk)
            .where(VideoChunk.video_id == video_lease.video_id)
            .order_by(VideoChunk.sequence_no)
        ))
        for index, chunk in enumerate(chunks):
            # These are valid V1 local artifacts, but their time origin is 0.
            chunk.chunk_media_ref = f"artifact://v1/chunk-{index}.mp4"
            chunk.analysis_window_media_ref = f"artifact://v1/window-{index}.mp4"

    for index, child_id in enumerate(prepared.chunk_parse_task_ids):
        at = NOW + timedelta(seconds=index)
        lease = queue.claim_chunk(
            f"source-{index}", lease_duration=timedelta(minutes=5), now=at
        )
        assert lease is not None and lease.chunk_parse_task_id == child_id
        execution = repo.load_chunk_execution(lease, now=at)
        assert execution.source_media_ref == proxy_ref
        assert execution.chunk_media_ref.startswith("artifact://v1/")
        repo.commit_chunk_result(
            lease,
            content={"persons": [], "timeline": []},
            now=at + timedelta(seconds=1),
        )

    refine = queue.claim_interval_refine(
        "source-refine",
        lease_duration=timedelta(minutes=5),
        now=NOW + timedelta(minutes=1),
    )
    assert refine is not None
    repo.commit_interval_refinement(
        refine,
        **_refinement_inputs(repo, refine, now=NOW + timedelta(minutes=1, seconds=1)), interval_content={"summary": "source"},
        member_updates={},
        now=NOW + timedelta(minutes=1, seconds=1),
    )
    indexing = queue.claim_chunk_index(
        "source-index",
        lease_duration=timedelta(minutes=5),
        now=NOW + timedelta(minutes=1, seconds=2),
    )
    assert indexing is not None
    snapshot = repo.load_chunk_indexing_snapshot(
        indexing, now=NOW + timedelta(minutes=1, seconds=2)
    )
    assert snapshot.chunk.chunk_parse_task_id == indexing.chunk_parse_task_id
    assert {item.source_media_ref for item in (snapshot.chunk,)} == {proxy_ref}
    assert all(
        item.chunk_media_ref.startswith("artifact://v1/")
        for item in (snapshot.chunk,)
    )


def test_prefix_cursor_uses_independent_lease_and_preserves_scope(repository):
    factory, queue, repo, task_id = _prepare_two_chunk_prefix_chain(repository)

    first = repo.claim_next_prefix_any(
        "prefix",
        lease_duration=timedelta(minutes=5),
        now=NOW + timedelta(minutes=2),
    )
    assert first is not None and first.sequence_no == 0
    assert not queue.is_execution_lease_committed(first)
    assert queue.is_execution_lease_active(
        task_id,
        "prefix",
        first.chunk_parse_task_id,
        first.lease_version,
        now=NOW + timedelta(minutes=2),
    )
    # A deleted parent invalidates every Prefix fence immediately, including
    # the repository heartbeat/commit path and media-reaper predicate.
    with factory.begin() as session:
        session.get(VideoParseTask, task_id).is_deleted = True
    assert not repo.heartbeat_prefix(
        first,
        lease_duration=timedelta(minutes=5),
        now=NOW + timedelta(minutes=2, milliseconds=100),
    )
    assert not queue.is_execution_lease_active(
        task_id,
        "prefix",
        first.chunk_parse_task_id,
        first.lease_version,
        now=NOW + timedelta(minutes=2, milliseconds=100),
    )
    with pytest.raises((VideoParseLeaseLostError, VideoParseTaskNotFoundError)):
        repo.commit_prefix(
            first,
            content_updates={},
            video_prefix_compact="deleted",
            now=NOW + timedelta(minutes=2, milliseconds=200),
        )
    with factory.begin() as session:
        session.get(VideoParseTask, task_id).is_deleted = False
    with factory() as session:
        initial_delay = session.get(VideoParseTask, task_id).parse_config["execution"]["retry_initial_delay_seconds"]
    failed_at = NOW + timedelta(minutes=2, milliseconds=300)
    retry_at = failed_at + timedelta(seconds=initial_delay)
    assert repo.retry_prefix(
        first,
        error="previous failure",
        now=failed_at,
    )
    retried = repo.claim_next_prefix_any(
        "prefix-retried",
        lease_duration=timedelta(minutes=5),
        now=retry_at,
    )
    assert retried is not None and retried.retry_count == 1
    assert repo.release_prefix(
        retried,
        now=retry_at + timedelta(milliseconds=100),
    )
    assert not repo.release_prefix(
        retried,
        now=retry_at + timedelta(milliseconds=200),
    )
    assert not repo.release_prefix(
        first,
        now=retry_at + timedelta(milliseconds=200),
    )
    with factory() as session:
        task = session.get(VideoParseTask, task_id)
        prefix = task.detail["_worker_runtime_v2"]["prefix"]
        assert prefix["lease_owner"] is None
        assert prefix["lease_expire_time_ms"] is None
        assert prefix["execution_task_id"] is None
        assert prefix["next_execute_time_ms"] == int(
            (retry_at + timedelta(milliseconds=100)).timestamp()
            * 1000
        )
        assert prefix["retry_count"] == 1
        assert prefix["last_error"] == {"message": "previous failure"}
    first = repo.claim_next_prefix_any(
        "prefix-after-cancel",
        lease_duration=timedelta(minutes=5),
        now=retry_at + timedelta(milliseconds=100),
    )
    assert first is not None
    assert first.retry_count == 1
    repo.commit_prefix(
        first,
        content_updates={},
        video_prefix_compact="chunk zero",
        now=retry_at + timedelta(milliseconds=200),
    )
    assert queue.is_execution_lease_committed(first)
    second = repo.claim_next_prefix_any(
        "prefix",
        lease_duration=timedelta(minutes=5),
        now=retry_at + timedelta(seconds=1),
    )
    assert second is not None and second.sequence_no == 1
    assert queue.is_execution_lease_committed(first)
    repo.commit_prefix(
        second,
        content_updates={"persons": ["P"]},
        video_prefix_compact="chunk zero and one",
        series_prefix_compact="episode summary",
        now=retry_at + timedelta(seconds=2),
    )
    assert queue.is_execution_lease_committed(first)
    assert queue.is_execution_lease_committed(second)
    with factory() as session:
        content = session.scalar(
            select(ChunkParseContent).where(
                ChunkParseContent.chunk_task_id
                == second.chunk_parse_task_id
            )
        )
        task = session.get(VideoParseTask, task_id)
        prefix = task.detail["_worker_runtime_v2"]["prefix"]
        assert content.detail["scope"] == [0, 1]
        assert content.version == second.content_version + 1
        assert prefix["next_chunk_sequence_no"] == 2
        assert prefix["lease_owner"] is None
        assert task.status == "INDEXING"
        assert task.lease_owner is None


def test_interleaved_prefix_model_calls_and_timing_preserve_runtime(repository):
    factory, queue, repo, task_id = _prepare_two_chunk_prefix_chain(repository)
    at = NOW + timedelta(minutes=2)
    repo.request_index_rebuild(task_id, now=at)
    assert queue.claim_video("coordinator", lease_duration=timedelta(minutes=5), now=at)
    index = queue.claim_chunk_index("index", lease_duration=timedelta(minutes=5), now=at)
    prefix = repo.claim_next_prefix_any("prefix", lease_duration=timedelta(minutes=5), now=at)
    assert index is not None and prefix is not None
    assert index.chunk_parse_task_id == prefix.chunk_parse_task_id
    with factory.begin() as session:
        parent = session.get(VideoParseTask, task_id)
        detail = deepcopy(parent.detail)
        detail["unrelated"] = {"keep": "task detail"}
        detail["_worker_runtime_v2"].update({
            "unrelated": {"keep": "runtime detail"},
            "voiceprint_preparation": {"active": False, "failures": 1},
            "failure_status": "INDEXING_FAILED",
        })
        parent.detail = detail
        expected = deepcopy(detail)

    assert repo.reserve_model_call(
        prefix, "prefix-call", lease_duration=timedelta(minutes=1), now=at,
    ).first_reservation
    assert repo.reserve_model_call(
        index, "index-call", lease_duration=timedelta(minutes=1), now=at,
    ).active_calls == 2
    started_at_ms = datetime_to_unix_time_ms(at)
    assert repo.record_execution_timing(
        index, started_at_ms=started_at_ms, elapsed_ms=1000,
        now=at + timedelta(seconds=1),
    )
    assert repo.heartbeat_prefix(
        prefix, lease_duration=timedelta(minutes=5), now=at + timedelta(seconds=2),
    )
    assert repo.renew_model_call(
        prefix, "prefix-call", lease_duration=timedelta(minutes=1),
        now=at + timedelta(seconds=3),
    )
    assert repo.renew_model_call(
        index, "index-call", lease_duration=timedelta(minutes=1),
        now=at + timedelta(seconds=4),
    )
    assert repo.release_model_call(
        task_id, "prefix-call", prefix.worker_id, now=at + timedelta(seconds=5),
    )
    assert repo.commit_prefix(
        prefix, content_updates={}, video_prefix_compact="video compact",
        series_prefix_compact="series compact", now=at + timedelta(seconds=6),
    ) == ()
    assert repo.record_execution_timing(
        index, started_at_ms=started_at_ms, elapsed_ms=7000,
        now=at + timedelta(seconds=7),
    )
    assert repo.release_model_call(
        task_id, "index-call", index.worker_id, now=at + timedelta(seconds=8),
    )

    runtime = expected["_worker_runtime_v2"]
    runtime["model_calls"] = {"used_call_ids": ["prefix-call", "index-call"], "active": {}}
    runtime["prefix"].update({
        "next_chunk_sequence_no": 1,
        "video_prefix_compact": "video compact",
        "series_prefix_compact": "series compact",
        "lease_owner": None,
        "lease_expire_time_ms": None,
        "execution_task_id": None,
        "committed_leases": {str(prefix.lease_version): {
            "execution_task_id": prefix.chunk_parse_task_id, "sequence_no": prefix.sequence_no,
        }},
    })
    runtime["execution_timing"] = {"version": 1, "entries": {
        f"index:{index.chunk_parse_task_id}:{index.lease_version}": {
            "phase": "index", "execution_kind": "chunk",
            "execution_task_id": index.chunk_parse_task_id,
            "lease_version": index.lease_version, "worker_id": index.worker_id,
            "started_at_ms": started_at_ms, "elapsed_ms": 7000,
            "finished": False, "outcome": "running",
        },
    }}
    with factory() as session:
        assert session.get(VideoParseTask, task_id).detail == expected
        child = session.get(ChunkParseTask, index.chunk_parse_task_id)
        assert (child.phase, child.status, child.lease_owner) == (3, "RUNNING", index.worker_id)


def test_prefix_load_failure_and_expiry_follow_saved_retry_policy(tmp_path, monkeypatch):
    from server.database import create_database_engine, create_schema

    engine = create_database_engine(
        f"sqlite+pysqlite:///{tmp_path / 'prefix-retry.sqlite'}", environment="test",
    )
    create_schema(engine)
    factory, _queue, repo, task_id = _prepare_two_chunk_prefix_chain((engine, None))
    with factory.begin() as session:
        task = session.get(VideoParseTask, task_id)
        task.parse_config = {**task.parse_config, "execution": {
            **task.parse_config["execution"], "retry_initial_delay_seconds": 7.0,
            "retry_max_delay_seconds": 11.0, "max_retry_count": 2,
        }}
        original_status = task.status
    start = NOW + timedelta(minutes=2)
    first = repo.claim_next_prefix_any("first", lease_duration=timedelta(seconds=3), now=start)
    assert first is not None
    runtime = object.__new__(VideoParseRuntimeService)
    runtime._persistence = repo
    runtime._prefix_lease = ContextVar("prefix_retry_test", default=None)

    def fail_to_load(_task_id):
        raise RuntimeError("task input unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(repo, "load_task_input", fail_to_load)
        patch.setattr("server.service.video_parse_persistence.now_utc", lambda: start)
        asyncio.run(runtime._execute_prefix(first))
    assert runtime._prefix_lease.get() is None
    with factory() as session:
        prefix = session.get(VideoParseTask, task_id).detail["_worker_runtime_v2"]["prefix"]
        assert prefix["retry_count"] == 1 and prefix["lease_owner"] is None
        assert prefix["next_execute_time_ms"] == datetime_to_unix_time_ms(start + timedelta(seconds=7))
        assert prefix["last_error"] == {"message": "task input unavailable"}
    assert repo.claim_next_prefix_any(
        "too-early", lease_duration=timedelta(seconds=3), now=start + timedelta(seconds=7, milliseconds=-1),
    ) is None
    second = repo.claim_next_prefix_any(
        "second", lease_duration=timedelta(seconds=3), now=start + timedelta(seconds=7),
    )
    assert second is not None and second.retry_count == 1
    expired_at = start + timedelta(seconds=10)
    assert repo.claim_next_prefix_any(
        "reaper", lease_duration=timedelta(seconds=3), now=expired_at,
    ) is None
    with factory() as session:
        prefix = session.get(VideoParseTask, task_id).detail["_worker_runtime_v2"]["prefix"]
        assert prefix["retry_count"] == 2 and prefix["lease_owner"] is None
        assert prefix["next_execute_time_ms"] == datetime_to_unix_time_ms(expired_at + timedelta(seconds=11))
    assert repo.claim_next_prefix_any(
        "still-early", lease_duration=timedelta(seconds=3), now=expired_at + timedelta(seconds=11, milliseconds=-1),
    ) is None
    third = repo.claim_next_prefix_any(
        "third", lease_duration=timedelta(seconds=3), now=expired_at + timedelta(seconds=11),
    )
    assert third is not None and third.retry_count == 2
    assert repo.retry_prefix(third, error="retry exhausted", now=expired_at + timedelta(seconds=12))
    assert repo.claim_next_prefix_any(
        "over-budget", lease_duration=timedelta(seconds=3), now=expired_at + timedelta(minutes=1),
    ) is None
    with factory() as session:
        task = session.get(VideoParseTask, task_id)
        assert task.detail["_worker_runtime_v2"]["prefix"]["retry_count"] == 3
        assert task.status == original_status


@pytest.mark.parametrize("failure_status", ["PARSING_FAILED", "INDEXING_FAILED"])
def test_inflight_prefix_commit_preserves_published_failure_and_finish_time(repository, failure_status):
    factory, queue, repo, task_id = _prepare_two_chunk_prefix_chain(repository)
    prefix_lease = repo.claim_next_prefix_any(
        "inflight-prefix",
        lease_duration=timedelta(minutes=5),
        now=NOW + timedelta(minutes=2),
    )
    assert prefix_lease is not None
    failed_at = NOW + timedelta(minutes=2, seconds=1)
    with factory.begin() as session:
        task = session.get(VideoParseTask, task_id)
        task.status = failure_status
        task.finish_time = failed_at
        task.detail = {
            **task.detail,
            "_worker_runtime_v2": {
                **task.detail["_worker_runtime_v2"],
                "failure_status": failure_status,
            },
        }

    reset = repo.commit_prefix(
        prefix_lease,
        content_updates={"persons": ["retained-prefix-update"]},
        video_prefix_compact="retained-prefix",
        now=NOW + timedelta(minutes=2, seconds=2),
    )
    assert reset and queue.is_execution_lease_committed(prefix_lease)
    with factory() as session:
        task = session.get(VideoParseTask, task_id)
        assert task.status == failure_status
        assert task.finish_time == failed_at
        assert task.detail["_worker_runtime_v2"]["failure_status"] == failure_status
        prefix = task.detail["_worker_runtime_v2"]["prefix"]
        assert prefix["next_chunk_sequence_no"] == 1
        assert prefix["video_prefix_compact"] == "retained-prefix"
        assert prefix["lease_owner"] is None
        content = session.scalar(select(ChunkParseContent).where(
            ChunkParseContent.chunk_task_id == prefix_lease.chunk_parse_task_id
        ))
        assert content.content["persons"] == ["retained-prefix-update"]
        assert content.version == prefix_lease.content_version + 1


def test_prefix_frontier_never_falls_back_to_an_older_parse_version(repository):
    factory, queue, repo, _task_id, video_lease, prepared = _prepare(
        repository, chunk_count=1
    )
    with factory.begin() as session:
        current = session.get(
            VideoParseTask, video_lease.video_parse_task_id
        )
        config = dict(current.parse_config)
        config["compact_enabled"] = True
        current.parse_config = config
    _commit_all_chunks(factory, queue, repo, video_lease, prepared, count=1)
    refine = queue.claim_interval_refine(
        "old-refine",
        lease_duration=timedelta(minutes=5),
        now=NOW + timedelta(minutes=1),
    )
    assert refine is not None
    repo.commit_interval_refinement(
        refine,
        **_refinement_inputs(repo, refine, now=NOW + timedelta(minutes=1, seconds=1)), interval_content={"summary": "old"},
        member_updates={},
        now=NOW + timedelta(minutes=1, seconds=1),
    )
    index_lease = queue.claim_chunk_index(
        "old-index",
        lease_duration=timedelta(minutes=5),
        now=NOW + timedelta(minutes=1, seconds=2),
    )
    assert index_lease is not None
    snapshot = repo.load_chunk_indexing_snapshot(
        index_lease, now=NOW + timedelta(minutes=1, seconds=2)
    )
    _finish_index(repo,
        index_lease,
        {
            item.chunk_parse_task_id: item.content_version
            for item in (snapshot.chunk,)
        },
        now=NOW + timedelta(minutes=1, seconds=3),
    )
    assert repo.aggregate_task(
        video_lease, now=NOW + timedelta(minutes=1, seconds=4)
    ).status == "COMPLETE"

    with factory.begin() as session:
        old = session.get(
            VideoParseTask, video_lease.video_parse_task_id
        )
        video = session.get(Video, old.video_id)
        newer_id = str(uuid4())
        session.add(VideoParseTask(
            id=newer_id,
            video_id=old.video_id,
            pipeline_version=old.pipeline_version,
            status="PENDING",
            parse_config=dict(old.parse_config),
            chunk_count=old.chunk_count,
            version=old.version + 1,
            next_execute_time=NOW,
            attempt_count=0,
            max_attempts=old.max_attempts,
            lease_version=0,
            processing_duration_ms=0,
            processing_timing_complete=True,
            is_test=True,
            is_deleted=False,
            detail={"_worker_runtime_v2": {"accepted": True}},
            create_time=datetime_to_unix_time_ms(NOW + timedelta(minutes=2)),
            update_time=datetime_to_unix_time_ms(NOW + timedelta(minutes=2)),
        ))
        series_id = video.series_id

    # The old version has claimable Prefix input, while the latest version is
    # not parsed yet. The frontier must block instead of selecting the old row.
    assert repo.claim_next_prefix(
        series_id,
        "prefix",
        lease_duration=timedelta(minutes=5),
        now=NOW + timedelta(minutes=3),
    ) is None


def test_series_prefix_invalidator_is_atomic_and_skips_disabled_tasks(repository):
    engine, _unused = repository
    factory = create_session_factory(engine)
    series_id = str(uuid4())
    task_ids: list[str] = []
    stale_chunk_task_id = str(uuid4())
    stale_video_chunk_id = str(uuid4())
    retained_content_id = str(uuid4())
    with factory.begin() as session:
        session.add(Series(
            id=series_id,
            tenant_id="mine",
            name="Prefix chain",
            description=None,
            status="INDEXING",
            important_extra_data=None,
            character_bible=[],
        ))
        for order, enabled in ((1, True), (2, False), (3, True)):
            video_id = str(uuid4())
            task_id = str(uuid4())
            task_ids.append(task_id)
            session.add(Video(
                id=video_id,
                series_id=series_id,
                episode_order=order,
                upload_sequence=order,
                filename=f"episode-{order}.mp4",
                source_uri=f"artifact://test/{order}.mp4",
                hash=(str(order) * 64)[:64],
                size_bytes=100,
                duration_ms=10_000,
                upload_status="COMPLETED",
                parse_status="COMPLETE",
                upload_progress=100,
                parse_progress=100,
            ))
            config = _config(chunk_count=1)
            config["compact_enabled"] = enabled
            prefix = {
                "next_chunk_sequence_no": 0 if order == 3 else 1,
                "lease_owner": "active" if order == 3 else None,
                "lease_version": order,
                "lease_expire_time_ms": int(
                    (
                        NOW
                        + (timedelta(minutes=10) if order == 3 else timedelta(0))
                    ).timestamp()
                    * 1000
                ) if order == 3 else None,
                "execution_task_id": stale_chunk_task_id if order == 3 else None,
                "next_execute_time_ms": 0,
                "retry_count": 2,
                "video_prefix_compact": f"video-{order}",
                "series_prefix_compact": f"series-{order}",
                "last_error": {"message": "old"},
            }
            session.add(VideoParseTask(
                id=task_id,
                video_id=video_id,
                pipeline_version="video-parse-v2",
                status="COMPLETE",
                parse_config=config,
                chunk_count=1,
                version=1,
                next_execute_time=NOW,
                attempt_count=1,
                max_attempts=3,
                lease_version=1,
                processing_duration_ms=0,
                processing_timing_complete=True,
                is_test=True,
                is_deleted=False,
                detail={
                    "_worker_runtime_v2": {
                        "accepted": True,
                        "prefix": prefix,
                        "unrelated": {"preserved": True},
                    }
                },
                create_time=datetime_to_unix_time_ms(NOW),
                update_time=datetime_to_unix_time_ms(NOW),
            ))
            if order == 3:
                session.add(VideoChunk(
                    id=stale_video_chunk_id,
                    video_id=video_id,
                    sequence_no=0,
                    start_ms=0,
                    end_ms=10_000,
                    window_start_ms=0,
                    window_end_ms=10_000,
                    chunk_media_ref="artifact://test/chunk-3.mp4",
                    analysis_window_media_ref="artifact://test/window-3.mp4",
                    transcript=[],
                    baseline_visual_caption=None,
                    keyframe_uris=[],
                ))
                session.add(ChunkParseTask(
                    id=stale_chunk_task_id,
                    video_task_id=task_id,
                    chunk_id=stale_video_chunk_id,
                    phase=3, indexed_content_version=1,
                    status="COMPLETE",
                    next_execute_time=NOW,
                    retry_count=0,
                    lease_version=1,
                    finish_time=NOW,
                ))
                session.add(ChunkParseContent(
                    id=retained_content_id,
                    chunk_task_id=stale_chunk_task_id,
                    content={"persons": ["retained"], "timeline": []},
                    version=1,
                    detail={"scope": [0], "compact": "retained child"},
                ))

    stale_lease = PrefixParseLease(
        video_parse_task_id=task_ids[2],
        chunk_parse_task_id=stale_chunk_task_id,
        video_chunk_id=stale_video_chunk_id,
        sequence_no=0,
        worker_id="active",
        lease_version=3,
        lease_expire_time=NOW + timedelta(minutes=10),
        retry_count=2,
        content_version=1,
        content={"persons": ["retained"], "timeline": []},
        content_detail={"scope": [0], "compact": "retained child"},
        series_prefix_compact="series-3",
        video_prefix_compact="video-3",
    )
    queue = MySqlVideoParseQueue(factory, video_concurrency=3)
    repo = VideoParsePersistenceService(factory)
    assert queue.is_execution_lease_active(
        task_ids[2],
        "prefix",
        stale_chunk_task_id,
        stale_lease.lease_version,
        now=NOW + timedelta(minutes=1),
    )

    with pytest.raises(VideoParseConflictError, match="active lease"):
        with factory.begin() as session:
            first_video = session.get(
                VideoParseTask, task_ids[0]
            ).video_id
            invalidate_series_prefixes_from(
                session,
                series_id=series_id,
                episode_order=1,
                video_id=first_video,
                now=NOW + timedelta(minutes=1),
            )
    with factory.begin() as session:
        third = session.get(VideoParseTask, task_ids[2])
        detail = dict(third.detail)
        runtime = dict(detail["_worker_runtime_v2"])
        prefix = dict(runtime["prefix"])
        prefix["lease_expire_time_ms"] = int(
            (NOW - timedelta(seconds=1)).timestamp() * 1000
        )
        runtime["prefix"] = prefix
        detail["_worker_runtime_v2"] = runtime
        third.detail = detail
        first_video = session.get(
            VideoParseTask, task_ids[0]
        ).video_id
        invalidated = invalidate_series_prefixes_from(
            session,
            series_id=series_id,
            episode_order=1,
            video_id=first_video,
            now=NOW + timedelta(minutes=1),
        )
        assert invalidated == (task_ids[0], task_ids[2])

    with factory() as session:
        first, disabled, third = (
            session.get(VideoParseTask, task_id)
            for task_id in task_ids
        )
        for task, old_version in ((first, 1), (third, 3)):
            runtime = task.detail["_worker_runtime_v2"]
            prefix = runtime["prefix"]
            assert prefix["next_chunk_sequence_no"] == 0
            assert prefix["video_prefix_compact"] == ""
            assert prefix["series_prefix_compact"] == ""
            assert prefix["lease_owner"] is None
            assert prefix["execution_task_id"] is None
            assert prefix["retry_count"] == 0
            assert prefix["lease_version"] == old_version + 1
            assert runtime["unrelated"] == {"preserved": True}
        assert disabled.detail["_worker_runtime_v2"]["prefix"][
            "series_prefix_compact"
        ] == "series-2"
        retained = session.get(ChunkParseContent, retained_content_id)
        assert retained.content == {"persons": ["retained"], "timeline": []}
        assert retained.detail == {"scope": [0], "compact": "retained child"}

    assert not repo.heartbeat_prefix(
        stale_lease,
        lease_duration=timedelta(minutes=5),
        now=NOW + timedelta(minutes=1, seconds=1),
    )
    assert not repo.retry_prefix(
        stale_lease,
        error="late worker",
        now=NOW + timedelta(minutes=1, seconds=1),
    )
    with pytest.raises((VideoParseLeaseLostError, VideoParseTaskNotFoundError)):
        repo.commit_prefix(
            stale_lease,
            content_updates={"persons": ["late"]},
            video_prefix_compact="late compact",
            now=NOW + timedelta(minutes=1, seconds=1),
        )


def test_prefix_invalidation_history_does_not_revive_old_lease_after_cursor_aba(
    repository,
) -> None:
    factory, queue, repo, task_id = _prepare_two_chunk_prefix_chain(repository)
    with factory() as session:
        task = session.get(VideoParseTask, task_id)
        video = session.get(Video, task.video_id)
        series_id = video.series_id
        video_id = video.id
        episode_order = video.episode_order

    old_lease = repo.claim_next_prefix(
        series_id,
        "old-prefix",
        lease_duration=timedelta(minutes=5),
        now=NOW + timedelta(minutes=2),
    )
    assert old_lease is not None and old_lease.sequence_no == 0
    repo.commit_prefix(
        old_lease,
        content_updates={"persons": ["old-chain-zero"]},
        video_prefix_compact="old chain zero",
        now=NOW + timedelta(minutes=2, seconds=1),
    )
    # Normal same-chain control: the exact successful lease remains committed
    # after its cursor advances beyond sequence zero.
    assert queue.is_execution_lease_committed(old_lease)

    invalidated_at = NOW + timedelta(minutes=2, seconds=2)
    with factory.begin() as session:
        assert invalidate_series_prefixes_from(
            session,
            series_id=series_id,
            episode_order=episode_order,
            video_id=video_id,
            now=invalidated_at,
        ) == (task_id,)
    with factory() as session:
        task = session.get(VideoParseTask, task_id)
        prefix = task.detail["_worker_runtime_v2"]["prefix"]
        old_marker = prefix["committed_leases"][str(old_lease.lease_version)]
        assert prefix["next_chunk_sequence_no"] == 0
        assert prefix["lease_version"] == old_lease.lease_version + 1
        assert old_marker == {
            "execution_task_id": old_lease.chunk_parse_task_id,
            "sequence_no": 0,
        }
    # Invalidation preserves the historical marker but resets the cursor, so
    # the committed query is false until the new chain passes that sequence.
    assert not queue.is_execution_lease_committed(old_lease)

    new_zero = repo.claim_next_prefix(
        series_id,
        "new-prefix-zero",
        lease_duration=timedelta(minutes=5),
        now=NOW + timedelta(minutes=2, seconds=3),
    )
    assert new_zero is not None and new_zero.sequence_no == 0
    assert new_zero.lease_version == old_lease.lease_version + 2
    assert new_zero.content_version == old_lease.content_version + 1
    assert not queue.is_execution_lease_committed(new_zero)
    repo.commit_prefix(
        new_zero,
        content_updates={"persons": ["new-chain-zero"]},
        video_prefix_compact="new chain zero",
        now=NOW + timedelta(minutes=2, seconds=4),
    )
    assert queue.is_execution_lease_committed(new_zero)
    # This is the ABA-shaped observation: once the new cursor passes sequence
    # zero, the preserved historical marker makes the old committed query true
    # again. It is historical success evidence, not current write authority.
    assert queue.is_execution_lease_committed(old_lease)

    new_one = repo.claim_next_prefix(
        series_id,
        "new-prefix-one",
        lease_duration=timedelta(minutes=5),
        now=NOW + timedelta(minutes=2, seconds=5),
    )
    assert new_one is not None and new_one.sequence_no == 1
    assert new_one.lease_version == new_zero.lease_version + 1
    assert queue.is_execution_lease_committed(new_zero)
    assert queue.is_execution_lease_committed(old_lease)
    assert not queue.is_execution_lease_committed(new_one)

    def persisted_chain_state():
        with factory() as session:
            task = session.get(VideoParseTask, task_id)
            contents = session.scalars(
                select(ChunkParseContent)
                .where(ChunkParseContent.chunk_task_id.in_(
                    [new_zero.chunk_parse_task_id, new_one.chunk_parse_task_id]
                ))
                .order_by(ChunkParseContent.id)
            )
            return task.detail, task.update_date, [
                (row.id, row.content, row.version, row.detail, row.update_date)
                for row in contents
            ]

    # Check each rejected operation separately, including exact lease expiry,
    # dispatch timing and timestamps; later operations cannot mask a mutation.
    before_stale_attempts = persisted_chain_state()
    stale_attempt_at = NOW + timedelta(minutes=2, seconds=6)
    assert not repo.heartbeat_prefix(
        old_lease,
        lease_duration=timedelta(minutes=5),
        now=stale_attempt_at,
    )
    assert persisted_chain_state() == before_stale_attempts
    assert not repo.retry_prefix(
        old_lease,
        error="stale retry",
        now=stale_attempt_at,
    )
    assert persisted_chain_state() == before_stale_attempts
    with pytest.raises((VideoParseLeaseLostError, VideoParseTaskNotFoundError)):
        repo.commit_prefix(
            old_lease,
            content_updates={"persons": ["stale overwrite"]},
            video_prefix_compact="stale compact",
            now=stale_attempt_at,
        )
    assert persisted_chain_state() == before_stale_attempts
    assert not repo.release_prefix(old_lease, now=stale_attempt_at)
    assert persisted_chain_state() == before_stale_attempts
    assert queue.is_execution_lease_active(
        task_id,
        "prefix",
        new_one.chunk_parse_task_id,
        new_one.lease_version,
        now=stale_attempt_at,
    )
    with factory() as session:
        task = session.get(VideoParseTask, task_id)
        prefix = task.detail["_worker_runtime_v2"]["prefix"]
        zero_content = session.scalar(
            select(ChunkParseContent).where(
                ChunkParseContent.chunk_task_id
                == new_zero.chunk_parse_task_id
            )
        )
        one_content = session.scalar(
            select(ChunkParseContent).where(
                ChunkParseContent.chunk_task_id
                == new_one.chunk_parse_task_id
            )
        )
        assert prefix["next_chunk_sequence_no"] == 1
        assert prefix["lease_owner"] == new_one.worker_id
        assert prefix["lease_version"] == new_one.lease_version
        assert prefix["execution_task_id"] == new_one.chunk_parse_task_id
        assert prefix["retry_count"] == 0
        assert prefix["last_error"] is None
        assert prefix["video_prefix_compact"] == "new chain zero"
        assert prefix["series_prefix_compact"] == ""
        assert zero_content.content["persons"] == ["new-chain-zero"]
        assert zero_content.version == new_zero.content_version + 1
        assert one_content.content["persons"] == []
        assert one_content.version == new_one.content_version

    repo.commit_prefix(
        new_one,
        content_updates={"persons": ["new-chain-one"]},
        video_prefix_compact="new chain complete",
        series_prefix_compact="new series summary",
        now=NOW + timedelta(minutes=2, seconds=7),
    )
    assert queue.is_execution_lease_committed(old_lease)
    assert queue.is_execution_lease_committed(new_zero)
    assert queue.is_execution_lease_committed(new_one)
    with factory() as session:
        task = session.get(VideoParseTask, task_id)
        prefix = task.detail["_worker_runtime_v2"]["prefix"]
        committed = prefix["committed_leases"]
        one_content = session.scalar(
            select(ChunkParseContent).where(
                ChunkParseContent.chunk_task_id
                == new_one.chunk_parse_task_id
            )
        )
        assert prefix["next_chunk_sequence_no"] == 2
        assert prefix["lease_owner"] is None
        assert prefix["execution_task_id"] is None
        assert prefix["video_prefix_compact"] == "new chain complete"
        assert prefix["series_prefix_compact"] == "new series summary"
        assert committed[str(old_lease.lease_version)] == {
            "execution_task_id": old_lease.chunk_parse_task_id,
            "sequence_no": 0,
        }
        assert committed[str(new_zero.lease_version)] == {
            "execution_task_id": new_zero.chunk_parse_task_id,
            "sequence_no": 0,
        }
        assert committed[str(new_one.lease_version)] == {
            "execution_task_id": new_one.chunk_parse_task_id,
            "sequence_no": 1,
        }
        assert one_content.content["persons"] == ["new-chain-one"]
        assert one_content.version == new_one.content_version + 1


def _seed_retrieval_chunk(repository):
    factory, task_id = _seed(repository, chunk_count=1)
    repo = VideoParsePersistenceService(factory)
    chunk_id = str(uuid4())
    child_id = str(uuid4())
    content = VideoParseResult.model_validate({
        "persons": ["千早爱音"],
        "timeline": [{
            "event_type": "DIALOGUE",
            "start_seconds": 1000,
            "end_seconds": 3000,
            "description": "千早爱音回答",
            "participants": ["千早爱音"],
            "visible_persons": ["千早爱音"],
            "speaker": "千早爱音",
            "text_variants": [{
                "language": "zh-Hans",
                "role": "ORIGINAL",
                "text": "我没事",
            }],
        }],
    })
    receipt = {
        "parse_task_id": task_id,
        "video_chunk_id": chunk_id,
        "content_version": 1,
        "index_generation": 2,
    }
    with factory.begin() as session:
        task = session.get(VideoParseTask, task_id)
        assert task is not None
        task.status = "COMPLETE"
        task.parse_config = {
            **task.parse_config,
            "indexing": {**task.parse_config["indexing"], "index_generation": 2},
        }
        task.finish_time = NOW
        video = session.get(Video, task.video_id)
        assert video is not None
        video.parse_status = "COMPLETE"
        video.parse_progress = 100
        session.add(VideoChunk(
            id=chunk_id,
            video_id=task.video_id,
            sequence_no=0,
            start_ms=0,
            end_ms=10_000,
            window_start_ms=0,
            window_end_ms=10_000,
            chunk_media_ref="artifact://v2/chunk.mp4",
            analysis_window_media_ref="artifact://v2/window.mp4",
            transcript=[],
            baseline_visual_caption=None,
            keyframe_uris=[],
        ))
        session.add(ChunkParseTask(
            id=child_id,
            video_task_id=task_id,
            chunk_id=chunk_id,
            phase=3, indexed_content_version=1,
            status="COMPLETE",
            next_execute_time=NOW,
            retry_count=0,
            lease_version=0,
            detail=None,
            create_time=datetime_to_unix_time_ms(NOW),
            update_time=datetime_to_unix_time_ms(NOW),
        ))
        session.add(ChunkParseContent(
            id=str(uuid4()),
            chunk_task_id=child_id,
            content=content.model_dump(mode="json"),
            version=1,
            detail={
                "compact": "她表示自己没事。",
                INDEX_EMBEDDINGS_DETAIL_KEY: {
                    "content_version": 1,
                    "representations": {},
                    "receipt": receipt,
                },
            },
            create_time=datetime_to_unix_time_ms(NOW),
            update_time=datetime_to_unix_time_ms(NOW),
        ))

    identity = VideoChunkRetrievalIdentity(
        tenant_id="mine",
        series_id=video.series_id,
        video_id=video.id,
        parse_task_id=task_id,
        content_version=1,
        index_generation=2,
    )
    return factory, repo, video, identity, chunk_id


def test_retrieval_scope_reads_only_task_and_chunk_projections_without_lazy_loading(repository):
    factory, repo, video, identity, _chunk_id = _seed_retrieval_chunk(repository)
    with factory.begin() as session:
        task = session.get(VideoParseTask, identity.parse_task_id)
        task.detail = {**task.detail, "large_runtime_trace": "模型调用记录" * 100_000}
        content = session.scalar(select(ChunkParseContent))
        content.detail = {**content.detail, "large_embedding_cache": [0.5] * 100_000}

    statements = []

    def record_sql(_connection, _cursor, statement, _parameters, _context, _many):
        if "FROM video_parse_task" in statement or "FROM chunk_parse_task" in statement:
            statements.append(statement)

    engine = repository[0]
    event.listen(engine, "before_cursor_execute", record_sql)
    try:
        scope = repo.resolve_retrieval_scope([video.series_id], tenant_id="mine")
    finally:
        event.remove(engine, "before_cursor_execute", record_sql)

    assert scope.parse_tasks_by_video_id == {video.id: identity.parse_task_id}
    assert scope.index_generation == 2
    assert len(statements) == 2
    columns = statements[0].split("FROM", 1)[0]
    assert "video_parse_task.parse_config" in columns
    assert "video_parse_task.detail" not in columns
    assert "video_parse_task.last_error" not in columns
    assert "video_parse_task.lease_owner" not in columns
    chunk_columns = statements[1].split("FROM", 1)[0]
    assert "chunk_parse_task.video_task_id" in chunk_columns
    assert "video_chunk.video_id" in chunk_columns
    assert "video_chunk.sequence_no" in chunk_columns
    assert "chunk_parse_content" not in chunk_columns
    assert "detail" not in chunk_columns


@pytest.mark.parametrize("invalid", ["phase", "status", "unpublished_version", "missing_content", "sequence", "video"])
def test_retrieval_scope_projection_preserves_chunk_publication_and_coverage_checks(repository, invalid):
    factory, repo, video, identity, chunk_id = _seed_retrieval_chunk(repository)
    other_chunk_id = _seed_retrieval_chunk(repository)[4] if invalid == "video" else None
    with factory.begin() as session:
        child = session.scalar(select(ChunkParseTask).where(ChunkParseTask.video_task_id == identity.parse_task_id))
        content = session.scalar(select(ChunkParseContent).where(ChunkParseContent.chunk_task_id == child.id))
        chunk = session.get(VideoChunk, chunk_id)
        if invalid == "phase":
            child.phase = 2
            child.status = "WAITING"
        elif invalid == "status":
            child.status = "QUEUED"
        elif invalid == "unpublished_version":
            content.version += 1
        elif invalid == "missing_content":
            session.delete(content)
        elif invalid == "sequence":
            chunk.sequence_no = 1
        else:
            child.chunk_id = other_chunk_id
    with pytest.raises(VideoParseConflictError, match="incomplete Chunk content"):
        repo.resolve_retrieval_scope([video.series_id], tenant_id="mine")


def test_v2_retrieval_scope_and_hydrate_are_pinned_to_latest_complete_task(
    repository,
) -> None:
    factory, repo, video, identity, chunk_id = _seed_retrieval_chunk(repository)
    task_id = identity.parse_task_id
    scope = repo.resolve_retrieval_scope([video.series_id], tenant_id="mine")
    assert scope.searched_video_ids == (video.id,)
    assert scope.parse_tasks_by_video_id == {video.id: task_id}
    assert scope.series_by_video_id == {video.id: video.series_id}
    assert scope.index_generation == 2
    assert scope.embedding_model == scope.parse_config.models.embedding.model
    hydrated = repo.hydrate_retrieval_candidates([(chunk_id, identity)], include_context=True)
    assert hydrated[0].facts.retrieval_identity == identity
    assert hydrated[0].facts.result.timeline[0].text_variants[0].text == "我没事"
    assert hydrated[0].facts.enrichments == ()
    assert hydrated[0].contexts == ()
    evidence = repo.hydrate_retrieval_evidence([(chunk_id, identity)])
    assert evidence[0].video_chunk_id == chunk_id
    assert evidence[0].transcript[0].text == "我没事"
    assert evidence[0].parse_provenance == {
        "parseTaskId": task_id,
        "contentVersion": 1,
        "indexGeneration": 2,
    }
    assert repo.get_chunk_evidence(chunk_id, tenant_id="mine") == evidence[0]
    with pytest.raises(VideoChunkNotFoundError):
        repo.get_chunk_evidence(chunk_id, tenant_id="other")
    with pytest.raises(VideoChunkNotFoundError):
        repo.get_chunk_evidence(str(uuid4()), tenant_id="mine")

    empty_series_id = str(uuid4())
    with factory.begin() as session:
        session.add(Series(
            id=empty_series_id,
            tenant_id="mine",
            name="Empty Series",
            description=None,
            status="INDEXING",
            important_extra_data=None,
            character_bible=[],
        ))
    partial_scope = repo.resolve_retrieval_scope(
        [video.series_id, empty_series_id],
        tenant_id="mine",
    )
    assert partial_scope.partial is True
    assert partial_scope.searched_video_ids == (video.id,)
    assert "没有视频" in partial_scope.warnings[0]

    with factory.begin() as session:
        session.add(VideoParseTask(
            id=str(uuid4()),
            video_id=video.id,
            pipeline_version="video-parse-v2",
            status="PENDING",
            parse_config=_config(chunk_count=1),
            chunk_count=1,
            version=2,
            next_execute_time=NOW + timedelta(minutes=1),
            attempt_count=0,
            max_attempts=3,
            lease_version=0,
            processing_duration_ms=0,
            processing_timing_complete=True,
            is_test=True,
            is_deleted=False,
            detail={"_worker_runtime_v2": {"accepted": True}},
            create_time=datetime_to_unix_time_ms(NOW + timedelta(minutes=1)),
            update_time=datetime_to_unix_time_ms(NOW + timedelta(minutes=1)),
        ))

    with pytest.raises(SeriesNotReadyError, match="no COMPLETE"):
        repo.resolve_retrieval_scope([video.series_id], tenant_id="mine")
    with pytest.raises(VideoParseConflictError, match="latest V2 task"):
        repo.hydrate_retrieval_evidence([(chunk_id, identity)])
    with pytest.raises(VideoParseConflictError, match="not COMPLETE"):
        repo.get_chunk_evidence(chunk_id, tenant_id="mine")

    with factory.begin() as session:
        newer = session.scalar(select(VideoParseTask).where(
            VideoParseTask.video_id == video.id,
            VideoParseTask.version == 2,
        ))
        newer.is_deleted = True
    restored = repo.resolve_retrieval_scope([video.series_id], tenant_id="mine")
    assert restored.parse_tasks_by_video_id == {video.id: task_id}
    assert repo.get_chunk_evidence(chunk_id, tenant_id="mine") == evidence[0]
    with factory.begin() as session:
        session.get(VideoParseTask, task_id).is_deleted = True
    with pytest.raises(VideoChunkNotFoundError):
        repo.get_chunk_evidence(chunk_id, tenant_id="mine")


@pytest.mark.parametrize("enabled,weights", [(False, {"17": 0.5}), (True, None), (True, {}), (True, {"17": -1.0})])
def test_sparse_scope_uses_frozen_publication_contract_not_cached_weights(repository, enabled, weights):
    factory, repo, video, identity, _chunk_id = _seed_retrieval_chunk(repository)
    with factory.begin() as session:
        task = session.get(VideoParseTask, identity.parse_task_id)
        task.parse_config = {**task.parse_config, "indexing": {**task.parse_config["indexing"], "sparse_enabled": enabled}}
    if weights is not None:
        with factory.begin() as session:
            content = session.scalar(select(ChunkParseContent).join(ChunkParseTask,
                ChunkParseTask.id == ChunkParseContent.chunk_task_id).where(
                ChunkParseTask.video_task_id == identity.parse_task_id))
            detail = dict(content.detail)
            envelope = dict(detail[INDEX_EMBEDDINGS_DETAIL_KEY])
            envelope["representations"] = {"sparse": {"channel": "sparse", "weights": weights}}
            detail[INDEX_EMBEDDINGS_DETAIL_KEY] = envelope
            content.detail = detail
    if not enabled:
        with pytest.raises(VideoParseConflictError, match="Sparse representation"):
            repo.resolve_retrieval_scope([video.series_id], tenant_id="mine", require_sparse=True)
    else:
        scope = repo.resolve_retrieval_scope([video.series_id], tenant_id="mine", require_sparse=True)
        assert scope.searched_video_ids == (video.id,)


@pytest.mark.parametrize("field,value", [
    ("tenant_id", "other"),
    ("series_id", str(uuid4())),
    ("video_id", str(uuid4())),
    ("parse_task_id", str(uuid4())),
    ("content_version", 2),
    ("index_generation", 3),
])
def test_v2_hydration_rejects_current_identity_mismatch(repository, field, value):
    _factory, repo, _video, identity, chunk_id = _seed_retrieval_chunk(repository)
    mismatched = identity.model_copy(update={field: value})
    with pytest.raises(VideoParseConflictError, match="retrieval candidate"):
        repo.hydrate_retrieval_candidates([(chunk_id, mismatched)])


@pytest.mark.parametrize("field,value", [
    ("content_version", True),
    ("content_version", 2),
    ("index_generation", True),
    ("index_generation", 0),
    ("video_chunk_id", str(uuid4())),
])
def test_v2_retrieval_uses_published_versions_even_with_stale_checkpoint(repository, field, value):
    factory, repo, video, identity, chunk_id = _seed_retrieval_chunk(repository)
    with factory.begin() as session:
        content = session.scalar(select(ChunkParseContent))
        envelope = content.detail[INDEX_EMBEDDINGS_DETAIL_KEY]
        content.detail = {
            **content.detail,
            INDEX_EMBEDDINGS_DETAIL_KEY: {
                **envelope, "receipt": {**envelope["receipt"], field: value},
            },
        }
        flag_modified(content, "detail")
    assert repo.resolve_retrieval_scope([video.series_id], tenant_id="mine").index_generation == 2
    assert repo.hydrate_retrieval_candidates([(chunk_id, identity)])[0].facts.retrieval_identity == identity
    assert repo.get_chunk_evidence(chunk_id, tenant_id="mine").video_chunk_id == chunk_id


@pytest.mark.parametrize("issue", ["child_pending", "content_missing", "published_version_old"])
def test_v2_media_evidence_requires_complete_current_content(repository, issue):
    factory, repo, _video, _identity, chunk_id = _seed_retrieval_chunk(repository)
    with factory.begin() as session:
        content = session.scalar(select(ChunkParseContent))
        if issue == "child_pending":
            session.get(ChunkParseTask, content.chunk_task_id).status = "QUEUED"
        elif issue == "content_missing":
            session.delete(content)
        elif issue == "published_version_old":
            session.get(ChunkParseTask, content.chunk_task_id).indexed_content_version = 99
    with pytest.raises(VideoParseConflictError, match="content|receipt"):
        repo.get_chunk_evidence(chunk_id, tenant_id="mine")


def test_v2_chunk_media_api_reads_current_publication_and_registered_images(tmp_path):
    from types import SimpleNamespace
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from server.api.errors import register_exception_handlers
    from server.database import create_database_engine, create_schema
    from server.router.media import router
    from server.service.media import MediaService
    from server.storage.artifact import LocalArtifactStore

    engine = create_database_engine(f"sqlite+pysqlite:///{tmp_path / 'media.sqlite'}", environment="test")
    create_schema(engine)
    factory, repo, _video, identity, chunk_id = _seed_retrieval_chunk((engine, None))
    store = LocalArtifactStore(tmp_path / "media", environment="test")
    frame = store.base_directory / "frame.png"
    frame.write_bytes(b"\x89PNG\r\n\x1a\nregistered-v2-image")
    with factory.begin() as session:
        session.get(VideoChunk, chunk_id).keyframe_uris = [{
            "uri": store.uri_for_path(frame), "at_seconds": 1000,
        }]
    media = MediaService(SimpleNamespace(), store, parse_persistence=repo)
    app = FastAPI()
    app.state.services = SimpleNamespace(media=media)
    app.include_router(router)
    register_exception_handlers(app)
    client = TestClient(app)
    params = {"tenantId": "mine", "videoChunkId": chunk_id}

    response = client.get("/api/video-chunks/get", params=params)
    assert response.status_code == 200
    evidence = response.json()
    assert evidence["seriesStatus"] == "READY"
    assert evidence["pipelineVersion"] == "video-parse-v2"
    assert media.get_chunk_evidence(chunk_id, tenant_id="mine").parse_provenance == {
        "parseTaskId": identity.parse_task_id, "contentVersion": 1, "indexGeneration": 2,
    }
    assert (evidence["startSeconds"], evidence["endSeconds"]) == (0, 10_000)
    assert evidence["keyframes"][0]["atSeconds"] == 1000
    image_url = evidence["keyframes"][0]["url"]
    image_response = client.get(image_url)
    assert image_response.status_code == 200
    assert image_response.content == frame.read_bytes()
    assert client.get(image_url.replace("tenantId=mine", "tenantId=other")).status_code == 404
    assert client.get("/api/video-chunks/get", params={**params, "tenantId": "other"}).status_code == 404

    with factory.begin() as session:
        content = session.scalar(select(ChunkParseContent))
        original_detail = content.detail
        content.detail = {}
        content.version += 1
    response = client.get("/api/video-chunks/get", params=params)
    assert response.status_code == 409
    assert response.json()["code"] == "MEDIA_NOT_READY"
    assert client.get(image_url).status_code == 409
    with factory.begin() as session:
        content = session.scalar(select(ChunkParseContent))
        content.detail = original_detail
        content.version -= 1
        session.get(VideoParseTask, identity.parse_task_id).status = "PARSING"
    response = client.get("/api/video-chunks/get", params=params)
    assert response.status_code == 409
    assert response.json()["code"] == "MEDIA_NOT_READY"
    assert client.get(image_url).status_code == 409


@pytest.mark.parametrize("field", ["source", "model", "dimensions", "video_fps", "max_height", "input_mode", "query_instruction"])
def test_v2_scope_rejects_mixed_index_configuration_but_allows_understanding_changes(
    repository, field,
):
    factory, repo, video, _identity, _chunk_id = _seed_retrieval_chunk(repository)
    _factory, _repo, other, other_identity, _other_chunk = _seed_retrieval_chunk(repository)
    with factory.begin() as session:
        task = session.get(VideoParseTask, other_identity.parse_task_id)
        config = dict(task.parse_config)
        config["models"] = {
            **config["models"],
            "understanding": {**config["models"]["understanding"], "model": "other-understanding"},
        }
        task.parse_config = config
    scope = repo.resolve_retrieval_scope([video.series_id, other.series_id], tenant_id="mine")
    assert set(scope.searched_video_ids) == {video.id, other.id}
    with factory.begin() as session:
        task = session.get(VideoParseTask, other_identity.parse_task_id)
        config = dict(task.parse_config)
        if field == "max_height":
            config["visual_input"] = {**config["visual_input"], "max_height": 480}
        else:
            embedding = dict(config["models"]["embedding"])
            embedding[field] = {
                "source": "other-source", "model": "other-model", "dimensions": 768,
                "video_fps": 2.0, "input_mode": "text", "query_instruction": "Find course text",
            }[field]
            config["models"] = {**config["models"], "embedding": embedding}
        task.parse_config = config
    with pytest.raises(VideoParseConflictError, match="different index contracts"):
        repo.resolve_retrieval_scope([video.series_id, other.series_id], tenant_id="mine")


@pytest.mark.parametrize("first_model,first_encoder,other_model,other_encoder,compatible", [
    pytest.param(
        "ep-shared", "doubao-embedding-vision-250615",
        "ep-shared", "doubao-embedding-vision-251215", False,
        id="same-endpoint-different-encoders",
    ),
    pytest.param(
        "ep-first", "doubao-embedding-vision-250615",
        "ep-second", "doubao-embedding-vision-250615", True,
        id="different-endpoints-same-encoder",
    ),
    pytest.param(
        "doubao-embedding-vision-250615", None,
        "ep-pinned", "doubao-embedding-vision-250615", True,
        id="direct-model-and-pinned-endpoint",
    ),
    pytest.param(
        "ep-shared", None,
        "ep-shared", "doubao-embedding-vision-250615", False,
        id="legacy-endpoint-and-pinned-encoder",
    ),
])
def test_v2_scope_compares_fixed_encoder_identity_without_changing_request_model(
    repository, first_model, first_encoder, other_model, other_encoder, compatible,
):
    factory, repo, video, identity, _chunk_id = _seed_retrieval_chunk(repository)
    _factory, _repo, other, other_identity, _other_chunk = _seed_retrieval_chunk(repository)
    with factory.begin() as session:
        for task_id, model, encoder_model in (
            (identity.parse_task_id, first_model, first_encoder),
            (other_identity.parse_task_id, other_model, other_encoder),
        ):
            task = session.get(VideoParseTask, task_id)
            config = dict(task.parse_config)
            config["models"] = {
                **config["models"],
                "embedding": {
                    **config["models"]["embedding"],
                    "model": model,
                    "encoder_model": encoder_model,
                },
            }
            task.parse_config = config

    if not compatible:
        with pytest.raises(VideoParseConflictError, match="different index contracts"):
            repo.resolve_retrieval_scope([video.series_id, other.series_id], tenant_id="mine")
        return

    scope = repo.resolve_retrieval_scope([video.series_id, other.series_id], tenant_id="mine")
    assert set(scope.searched_video_ids) == {video.id, other.id}
    assert scope.parse_tasks_by_video_id == {
        video.id: identity.parse_task_id,
        other.id: other_identity.parse_task_id,
    }
    assert scope.embedding_model == scope.parse_config.models.embedding.model
    assert scope.embedding_model in {first_model, other_model}
    assert scope.index_generation == 2


@pytest.mark.parametrize("issue", [None, "missing", "gap", "stale_checkpoint_generation", "published_version"])
def test_v2_hydration_reads_adjacent_context_with_its_own_version(repository, issue):
    factory, repo, video, identity, chunk_id = _seed_retrieval_chunk(repository)
    neighbors = []
    with factory.begin() as session:
        task = session.get(VideoParseTask, identity.parse_task_id)
        task.chunk_count = 3
        session.get(Video, video.id).duration_ms = 30_000
        chunk = session.get(VideoChunk, chunk_id)
        chunk.sequence_no = 1
        chunk.start_ms = chunk.window_start_ms = 10_000
        chunk.end_ms = chunk.window_end_ms = 20_000
        for sequence in (0, 2):
            other_chunk_id, child_id = str(uuid4()), str(uuid4())
            neighbors.append(other_chunk_id)
            start, end = sequence * 10_000, (sequence + 1) * 10_000
            receipt = {
                "parse_task_id": task.id, "video_chunk_id": other_chunk_id,
                "content_version": sequence + 2, "index_generation": 2,
            }
            if sequence == 2:
                if issue == "gap":
                    start += 1
                elif issue == "stale_checkpoint_generation":
                    receipt["index_generation"] = 3
                elif issue == "published_version":
                    receipt["content_version"] = 99
            session.add(VideoChunk(
                id=other_chunk_id, video_id=video.id, sequence_no=sequence,
                start_ms=start, end_ms=end,
                window_start_ms=start, window_end_ms=end,
                chunk_media_ref=f"artifact://v2/chunk-{sequence}.mp4",
                analysis_window_media_ref=f"artifact://v2/window-{sequence}.mp4",
                transcript=[], baseline_visual_caption=None, keyframe_uris=[],
            ))
            session.add(ChunkParseTask(
                id=child_id, video_task_id=task.id, chunk_id=other_chunk_id,
                phase=3, indexed_content_version=receipt["content_version"],
                status="QUEUED" if issue == "missing" and sequence == 2 else "COMPLETE",
                next_execute_time=NOW, retry_count=0, lease_version=0,
                detail=None, create_time=datetime_to_unix_time_ms(NOW), update_time=datetime_to_unix_time_ms(NOW),
            ))
            session.add(ChunkParseContent(
                id=str(uuid4()), chunk_task_id=child_id, version=sequence + 2,
                content={"persons": [], "timeline": []},
                detail={INDEX_EMBEDDINGS_DETAIL_KEY: {
                    "content_version": sequence + 2, "representations": {}, "receipt": receipt,
                }},
                create_time=datetime_to_unix_time_ms(NOW), update_time=datetime_to_unix_time_ms(NOW),
            ))
    # Context is loaded only when explicitly requested.
    assert repo.hydrate_retrieval_candidates([(chunk_id, identity)])[0].contexts == ()
    if issue in {"missing", "gap", "published_version"}:
        with pytest.raises(VideoParseConflictError, match="context|receipt"):
            repo.hydrate_retrieval_candidates([(chunk_id, identity)], include_context=True)
        return
    result = repo.hydrate_retrieval_candidates([(chunk_id, identity)], include_context=True)
    assert result[0].facts.retrieval_identity.content_version == 1
    assert [item.video_chunk_id for item in result[0].contexts] == neighbors
    assert [item.retrieval_identity.content_version for item in result[0].contexts] == [2, 4]
    assert all(item.retrieval_identity.index_generation == identity.index_generation for item in result[0].contexts)
    assert [(item.start_seconds, item.end_seconds) for item in result[0].contexts] == [(0, 10_000), (20_000, 30_000)]


def test_parse_success_and_timing_survive_the_same_chunk_index_lease(repository):
    factory, queue, repo, task_id, video_lease, prepared = _prepare(repository, chunk_count=1)
    parse = queue.claim_chunk("parse", lease_duration=timedelta(minutes=5), now=NOW)
    started = datetime_to_unix_time_ms(NOW)
    assert repo.record_execution_timing(parse, started_at_ms=started, elapsed_ms=0, now=NOW)
    repo.commit_chunk_result(parse, content={"persons": [], "timeline": []}, now=NOW)
    refine = queue.claim_interval_refine("refine", lease_duration=timedelta(minutes=5), now=NOW)
    repo.commit_interval_refinement(refine, **_refinement_inputs(repo, refine, now=NOW),
                                   interval_content={"updates": []}, member_updates={}, now=NOW)
    index = queue.claim_chunk_index("index", lease_duration=timedelta(minutes=5), now=NOW)
    assert index.chunk_parse_task_id == parse.chunk_parse_task_id and index.lease_version > parse.lease_version
    assert not queue.heartbeat_chunk(parse, lease_duration=timedelta(minutes=5), now=NOW)
    assert queue.is_execution_lease_committed(parse)
    assert repo.record_execution_timing(parse, started_at_ms=started, elapsed_ms=1000,
                                       finished=True, outcome="completed", now=NOW)
    with factory() as session:
        child = session.get(ChunkParseTask, parse.chunk_parse_task_id)
        assert (child.phase, child.status, child.lease_owner) == (3, "RUNNING", "index")


def test_refine_result_and_all_member_advancement_rollback_together(repository, monkeypatch):
    factory, queue, repo, task_id, video_lease, prepared = _prepare(repository, chunk_count=2)
    _commit_all_chunks(factory, queue, repo, video_lease, prepared, count=2)
    at = NOW + timedelta(minutes=1)
    refine = queue.claim_interval_refine("refine", lease_duration=timedelta(minutes=5), now=at)
    inputs = _refinement_inputs(repo, refine, now=at)
    def interrupt(*args, **kwargs):
        raise RuntimeError("interrupted before transaction commit")
    monkeypatch.setattr("server.service.video_parse_persistence._advance_ready_chunks", interrupt)
    with pytest.raises(RuntimeError, match="interrupted"):
        repo.commit_interval_refinement(refine, **inputs, interval_content={"updates": []},
                                       member_updates={prepared.chunk_parse_task_ids[0]: {"persons": ["changed"]}}, now=at)
    with factory() as session:
        assert session.get(IntervalParseContent, refine.interval_parse_task_id) is None
        assert session.get(IntervalParseTask, refine.interval_parse_task_id).status == "REFINING"
        for child_id in prepared.chunk_parse_task_ids:
            child = session.get(ChunkParseTask, child_id)
            content = session.scalar(select(ChunkParseContent).where(ChunkParseContent.chunk_task_id == child_id))
            assert (child.phase, child.status, content.version, content.content["persons"]) == (2, "WAITING", 1, [])


def test_early_index_exhaustion_closes_parent_with_unparsed_later_chunk(repository):
    factory, queue, repo, task_id, video_lease, prepared = _prepare(repository, chunk_count=4)
    _commit_all_chunks(factory, queue, repo, video_lease, prepared, count=3)
    at = NOW + timedelta(minutes=1)
    refine = queue.claim_interval_refine("refine", lease_duration=timedelta(minutes=5), now=at)
    repo.commit_interval_refinement(refine, **_refinement_inputs(repo, refine, now=at),
                                   interval_content={"updates": []}, member_updates={}, now=at)
    with factory.begin() as session:
        session.get(ChunkParseTask, prepared.chunk_parse_task_ids[0]).retry_count = 2
    index = queue.claim_chunk_index("index", lease_duration=timedelta(minutes=5), now=at)
    assert queue.retry_chunk(index, error="ES unavailable", retry_at=at, now=at)
    assert queue.claim_chunk("later", lease_duration=timedelta(minutes=5), now=at) is None
    with factory() as session:
        parent = session.get(VideoParseTask, task_id)
        assert parent.status == "INDEXING_FAILED"
        assert not parent.capacity_acquired
        later = session.get(ChunkParseTask, prepared.chunk_parse_task_ids[3])
        assert (later.phase, later.status) == (1, "QUEUED")
        failed = session.get(ChunkParseTask, index.chunk_parse_task_id)
        assert (failed.phase, failed.status) == (3, "FAILED")
        assert session.scalar(select(ChunkParseContent).where(ChunkParseContent.chunk_task_id == failed.id)) is not None


def test_prefix_content_change_revokes_index_but_preserves_failure_intent(repository):
    factory, queue, repo, task_id = _prepare_two_chunk_prefix_chain(repository)
    at = NOW + timedelta(minutes=2)
    repo.request_index_rebuild(task_id, now=at)
    queue.claim_video("coordinator", lease_duration=timedelta(minutes=5), now=at)
    index = queue.claim_chunk_index("old-index", lease_duration=timedelta(minutes=5), now=at)
    prefix = repo.claim_next_prefix_any("prefix", lease_duration=timedelta(minutes=5), now=at)
    assert prefix.chunk_parse_task_id == index.chunk_parse_task_id
    with factory.begin() as session:
        parent = session.get(VideoParseTask, task_id)
        detail = json.loads(json.dumps(parent.detail))
        detail["_worker_runtime_v2"]["failure_status"] = "INDEXING_FAILED"
        parent.detail = detail
    requeued = repo.commit_prefix(prefix, content_updates={"persons": ["new"]},
                                 video_prefix_compact="updated", now=at)
    assert requeued == (index.chunk_parse_task_id,)
    with factory() as session:
        child = session.get(ChunkParseTask, index.chunk_parse_task_id)
        assert (child.phase, child.status, child.lease_owner, child.start_time) == (3, "QUEUED", None, None)
        assert child.lease_version > index.lease_version
        assert session.get(VideoParseTask, task_id).detail["_worker_runtime_v2"]["failure_status"] == "INDEXING_FAILED"
        assert all(item.status == "COMPLETE" for item in session.scalars(select(IntervalParseTask)))
    with pytest.raises(VideoParseLeaseLostError):
        _finish_index(repo, index, now=at)
    assert queue.claim_chunk_index("new-index", lease_duration=timedelta(minutes=5), now=at) is None


def test_index_completion_checks_parent_target_generation_and_checkpoint(repository):
    factory, queue, repo, task_id, video_lease, prepared = _prepare(repository, chunk_count=1)
    _commit_all_chunks(factory, queue, repo, video_lease, prepared, count=1)
    at = NOW + timedelta(minutes=1)
    refine = queue.claim_interval_refine("refine", lease_duration=timedelta(minutes=5), now=at)
    repo.commit_interval_refinement(refine, **_refinement_inputs(repo, refine, now=at),
                                   interval_content={"updates": []}, member_updates={}, now=at)
    index = queue.claim_chunk_index("index", lease_duration=timedelta(minutes=5), now=at)
    with pytest.raises(VideoParseConflictError, match="receipt"):
        repo.complete_chunk_indexing(index, 1, 1, now=at)
    repo.save_index_embeddings(index, index.chunk_parse_task_id, 1, {}, receipt={
        "parse_task_id": task_id, "video_chunk_id": index.video_chunk_id,
        "content_version": 1, "index_generation": 1,
    }, now=at)
    with pytest.raises(VideoParseConflictError, match="generation changed"):
        repo.complete_chunk_indexing(index, 1, 2, now=at)
    with factory() as session:
        child = session.get(ChunkParseTask, index.chunk_parse_task_id)
        assert child.status == "RUNNING" and child.indexed_content_version is None


def test_published_migrated_chunk_is_retrievable_without_embedding_checkpoint(repository):
    factory, repo, video, identity, chunk_id = _seed_retrieval_chunk(repository)
    with factory.begin() as session:
        content = session.scalar(select(ChunkParseContent))
        content.detail = {}
    assert repo.resolve_retrieval_scope([video.series_id], tenant_id="mine").index_generation == identity.index_generation
    assert repo.get_chunk_evidence(chunk_id, tenant_id="mine").video_chunk_id == chunk_id
    assert repo.hydrate_retrieval_candidates([(chunk_id, identity)])[0].facts.retrieval_identity == identity


def test_preparation_rebuilds_ready_masks_from_committed_understanding(repository):
    factory, queue, repo, _task_id, video_lease, prepared = _prepare(repository, chunk_count=2)
    _commit_all_chunks(factory, queue, repo, video_lease, prepared, count=2)
    at = NOW + timedelta(minutes=1)
    refine = queue.claim_interval_refine("refine", lease_duration=timedelta(minutes=5), now=at)
    repo.commit_interval_refinement(refine, **_refinement_inputs(repo, refine, now=at),
                                   interval_content={"updates": []}, member_updates={}, now=at)
    with factory.begin() as session:
        interval = session.get(IntervalParseTask, prepared.interval_parse_task_ids[0])
        interval.ready_mask = 0
        children = [session.get(ChunkParseTask, child_id) for child_id in prepared.chunk_parse_task_ids]
        children[0].status = "FAILED"
        chunk_ids = [child.chunk_id for child in children]
        original = [(row.id, row.version, row.content, row.detail) for row in session.scalars(select(ChunkParseContent))]
    repo.ensure_parse_tasks(video_lease, chunk_ids, [(0, 2)], now=at)
    with factory() as session:
        assert session.get(IntervalParseTask, prepared.interval_parse_task_ids[0]).ready_mask == 3
        failed = session.get(ChunkParseTask, prepared.chunk_parse_task_ids[0])
        assert (failed.phase, failed.status) == (3, "FAILED")
        assert [(row.id, row.version, row.content, row.detail) for row in session.scalars(select(ChunkParseContent))] == original


def test_prefix_new_content_reopens_completed_parent_without_old_coordinator_backoff(repository):
    factory, queue, repo, task_id = _prepare_two_chunk_prefix_chain(repository)
    at = NOW + timedelta(minutes=2)
    with factory.begin() as session:
        parent = session.get(VideoParseTask, task_id)
        assert parent.status == "COMPLETE"
        parent.next_execute_time = at + timedelta(hours=1)
    prefix = repo.claim_next_prefix_any("prefix", lease_duration=timedelta(minutes=5), now=at)
    assert prefix is not None
    repo.commit_prefix(prefix, content_updates={"persons": ["new"]},
                       video_prefix_compact="updated", now=at)
    with factory() as session:
        parent = session.get(VideoParseTask, task_id)
        assert (parent.status, parent.next_execute_time) == ("INDEXING", at)
        child = session.get(ChunkParseTask, prefix.chunk_parse_task_id)
        assert (child.phase, child.status) == (3, "QUEUED")
    assert queue.claim_video("new-coordinator", lease_duration=timedelta(minutes=5), now=at) is not None
    assert queue.claim_chunk_index("new-index", lease_duration=timedelta(minutes=5), now=at) is not None
