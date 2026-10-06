from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from engine.parsev2.schemas import VideoParseResult
from engine.rag import (
    compute_index_projection_revision,
    compute_source_revision,
    project_chunk_index,
)
from engine.rag.contracts import VideoChunkRetrievalIdentity
from server.config.parse_profile import default_video_parse_v2_config
from server.database import create_session_factory
from server.model.dto.jobDTO import PrefixParseLease
from server.model.job import (
    ChunkParseContentRecord,
    ChunkParseTaskRecord,
    IntervalParseContentRecord,
    IntervalParseTaskRecord,
    VideoParseTaskRecord,
)
from server.model.video import SeriesRecord, VideoChunkRecord, VideoRecord
from server.mq.video_parse_queue import MySqlVideoParseQueue
from server.repository.video_parse import (
    INDEX_EMBEDDINGS_DETAIL_KEY,
    ModelCallBudgetExhaustedError,
    ModelCallConcurrencyBusyError,
    VideoParseConflictError,
    VideoParseLeaseLostError,
    VideoParseRepository,
    VideoParseTaskNotFoundError,
    invalidate_series_prefixes_from,
)
from server.repository.video import SeriesNotReadyError


NOW = datetime(2026, 9, 29, 2, 0, tzinfo=UTC)


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
        "max_concurrent_model_calls": 1,
    })
    return config


def _seed(repository, *, chunk_count: int = 4, accepted: bool = True, batch_size: int = 4):
    engine, _unused = repository
    factory = create_session_factory(engine)
    series_id, video_id, task_id = str(uuid4()), str(uuid4()), str(uuid4())
    config = _config(chunk_count=chunk_count, batch_size=batch_size)
    with factory.begin() as session:
        session.add(SeriesRecord(
            series_id=series_id,
            workspace_id="mine",
            name=f"Series {series_id}",
            description=None,
            status="INDEXING",
            important_extra_data=None,
            character_bible=[],
        ))
        session.add(VideoRecord(
            video_id=video_id,
            series_id=series_id,
            episode_order=1,
            upload_sequence=1,
            original_filename="episode.mp4",
            original_source_uri="artifact://test/episode.mp4",
            content_hash="a" * 64,
            total_bytes=100,
            duration_seconds=chunk_count * 10,
            upload_status="COMPLETED",
            parse_status="QUEUED",
            upload_progress=100,
            parse_progress=0,
        ))
        session.add(VideoParseTaskRecord(
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
            create_time=NOW,
            update_time=NOW,
        ))
    return factory, task_id


def _prepare(repository, *, chunk_count: int = 4):
    factory, task_id = _seed(repository, chunk_count=chunk_count)
    queue = MySqlVideoParseQueue(factory)
    repo = VideoParseRepository(factory)
    lease = queue.claim_video(
        "prepare", lease_duration=timedelta(minutes=10), now=NOW
    )
    assert lease is not None
    chunks = repo.ensure_video_chunks(
        lease,
        [(index, index * 10, (index + 1) * 10) for index in range(chunk_count)],
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


def _commit_all_chunks(factory, queue, repo, video_lease, prepared, *, count: int = 4):
    leases = []
    for index, task_id in enumerate(prepared.chunk_parse_task_ids[:count]):
        assert queue.dispatch_chunk(
            task_id, video_lease, now=NOW + timedelta(seconds=index)
        )
        lease = queue.claim_chunk(
            f"chunk-{index}", lease_duration=timedelta(minutes=10), now=NOW + timedelta(seconds=index)
        )
        assert lease is not None and lease.chunk_parse_task_id == task_id
        repo.commit_chunk_result(
            lease,
            content={"persons": [], "timeline": [{"value": index}]},
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
        task = session.get(VideoParseTaskRecord, task_id)
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
        interval_content={"summary": "short"},
        member_updates={},
        now=NOW + timedelta(minutes=1, seconds=1),
    )
    index_lease = queue.claim_interval_index(
        "index",
        lease_duration=timedelta(minutes=5),
        now=NOW + timedelta(minutes=1, seconds=2),
    )
    assert index_lease is not None
    index_snapshot = repo.load_interval_indexing_snapshot(
        index_lease, now=NOW + timedelta(minutes=1, seconds=2)
    )
    repo.complete_interval_indexing(
        index_lease,
        {
            item.chunk_parse_task_id: item.content_version
            for item in index_snapshot.chunks
        },
        now=NOW + timedelta(minutes=1, seconds=3),
    )
    assert repo.aggregate_task(
        video_lease, now=NOW + timedelta(minutes=1, seconds=4)
    ).status == "COMPLETE"
    return factory, queue, repo, task_id


def test_prepare_is_idempotent_and_short_video_gets_one_interval(repository):
    factory, queue, repo, task_id, _lease, prepared = _prepare(repository, chunk_count=2)
    assert repo.load_task_input(task_id).workspace_id == "mine"
    assert len(prepared.chunk_parse_task_ids) == 2
    assert len(prepared.interval_parse_task_ids) == 1
    with factory() as session:
        interval = session.get(IntervalParseTaskRecord, prepared.interval_parse_task_ids[0])
        assert (interval.start_sequence_no, interval.chunk_count) == (0, 2)
        assert interval.chunk_parse_task_ids == list(prepared.chunk_parse_task_ids)
    assert queue.claim_video(
        "another", lease_duration=timedelta(minutes=1), now=NOW
    ) is None


def test_chunk_commit_masks_refine_scope_union_and_index_wait_are_atomic(repository):
    factory, queue, repo, task_id, video_lease, prepared = _prepare(repository)
    _commit_all_chunks(factory, queue, repo, video_lease, prepared)

    first = queue.claim_interval_refine(
        "refine-0", lease_duration=timedelta(minutes=10), now=NOW + timedelta(minutes=1)
    )
    assert first is not None and first.start_sequence_no == 0
    snapshot = repo.load_interval_members(first, now=NOW + timedelta(minutes=1))
    repo.commit_interval_refinement(
        first,
        interval_content={"summary": "first"},
        member_updates={snapshot.members[1].chunk_parse_task_id: {"persons": ["A"]}},
        now=NOW + timedelta(minutes=1, seconds=1),
    )

    indexing = queue.claim_interval_index(
        "index-0", lease_duration=timedelta(minutes=10), now=NOW + timedelta(minutes=1, seconds=2)
    )
    assert indexing is not None and indexing.interval_parse_task_id == first.interval_parse_task_id
    index_snapshot = repo.load_interval_indexing_snapshot(
        indexing, now=NOW + timedelta(minutes=1, seconds=2)
    )
    assert [item.sequence_no for item in index_snapshot.chunks] == [0]
    indexed_chunk = index_snapshot.chunks[0]
    repo.save_index_embeddings(
        indexing,
        indexed_chunk.chunk_parse_task_id,
        indexed_chunk.content_version,
        {"semantic": {"vector": [1.0]}},
        now=NOW + timedelta(minutes=1, seconds=2),
    )
    repo.save_index_embeddings(
        indexing,
        indexed_chunk.chunk_parse_task_id,
        indexed_chunk.content_version,
        {"vision": {"vector": [2.0]}},
        receipt={
            "parse_task_id": indexing.video_parse_task_id,
            "video_chunk_id": indexed_chunk.video_chunk_id,
            "content_version": indexed_chunk.content_version,
            "source_revision": "source-1",
            "projection_revision": "projection-1",
            "retrieval_contract_id": "retrieval-1",
            "semantic_encoder_id": "semantic-1",
            "vision_encoder_id": "vision-1",
            "index_generation": 1,
        },
        now=NOW + timedelta(minutes=1, seconds=2),
    )
    with factory() as session:
        cached = session.scalar(
            select(ChunkParseContentRecord).where(
                ChunkParseContentRecord.chunk_parse_task_id
                == indexed_chunk.chunk_parse_task_id
            )
        ).detail["_index_embeddings_v2"]
        assert set(cached["representations"]) == {"semantic", "vision"}
        assert cached["receipt"] == {
            "parse_task_id": indexing.video_parse_task_id,
            "video_chunk_id": indexed_chunk.video_chunk_id,
            "content_version": indexed_chunk.content_version,
            "source_revision": "source-1",
            "projection_revision": "projection-1",
            "retrieval_contract_id": "retrieval-1",
            "semantic_encoder_id": "semantic-1",
            "vision_encoder_id": "vision-1",
            "index_generation": 1,
        }
    state = repo.complete_interval_indexing(
        indexing,
        {item.chunk_parse_task_id: item.content_version for item in index_snapshot.chunks},
        now=NOW + timedelta(minutes=1, seconds=3),
    )
    assert state.status == "PARSING"
    with factory() as session:
        interval = session.get(IntervalParseTaskRecord, first.interval_parse_task_id)
        assert interval.status == "INDEXING" and interval.lease_owner is None
        assert interval.retry_count == 0
        assert interval.next_execute_time > NOW + timedelta(minutes=1, seconds=3)

    second = queue.claim_interval_refine(
        "refine-1", lease_duration=timedelta(minutes=10), now=NOW + timedelta(minutes=2)
    )
    assert second is not None and second.start_sequence_no == 1
    second_snapshot = repo.load_interval_members(second, now=NOW + timedelta(minutes=2))
    repo.commit_interval_refinement(
        second,
        interval_content={"summary": "second"},
        member_updates={second_snapshot.members[0].chunk_parse_task_id: {"timeline": [{"value": "changed"}]}},
        now=NOW + timedelta(minutes=2, seconds=1),
    )
    with factory() as session:
        contents = list(session.scalars(
            select(ChunkParseContentRecord).order_by(
                ChunkParseContentRecord.chunk_parse_task_id
            )
        ))
        by_id = {item.chunk_parse_task_id: item for item in contents}
        overlap = by_id[prepared.chunk_parse_task_ids[1]]
        assert overlap.detail["scope"] == [0, 1, 2, 3]
        assert overlap.version == 3

    # Both Intervals can now finish from current content versions.
    for worker in ("index-a", "index-b", "index-c"):
        lease = queue.claim_interval_index(
            worker,
            lease_duration=timedelta(minutes=10),
            now=NOW + timedelta(minutes=3),
        )
        if lease is None:
            break
        current = repo.load_interval_indexing_snapshot(
            lease, now=NOW + timedelta(minutes=3)
        )
        if lease.interval_parse_task_id == first.interval_parse_task_id:
            assert [item.sequence_no for item in current.chunks] == [0, 1, 2]
        repo.complete_interval_indexing(
            lease,
            {item.chunk_parse_task_id: item.content_version for item in current.chunks},
            now=NOW + timedelta(minutes=3, seconds=1),
        )
    assert repo.aggregate_task(
        video_lease, now=NOW + timedelta(minutes=4)
    ).status == "COMPLETE"
    assert queue.is_execution_lease_committed(video_lease)


def test_stale_chunk_lease_cannot_commit(repository):
    _factory, queue, repo, _task_id, video_lease, prepared = _prepare(repository, chunk_count=2)
    task_id = prepared.chunk_parse_task_ids[0]
    assert queue.dispatch_chunk(task_id, video_lease, now=NOW)
    lease = queue.claim_chunk(
        "old", lease_duration=timedelta(seconds=5), now=NOW
    )
    assert lease is not None
    assert queue.reap_expired(
        retry_delay=timedelta(0), now=NOW + timedelta(seconds=6)
    ) == 1
    assert queue.dispatch_chunk(
        task_id, video_lease, now=NOW + timedelta(seconds=6)
    )
    replacement = queue.claim_chunk(
        "new", lease_duration=timedelta(minutes=1), now=NOW + timedelta(seconds=6)
    )
    assert replacement is not None and replacement.lease_version > lease.lease_version
    with pytest.raises(VideoParseLeaseLostError):
        repo.commit_chunk_result(
            lease,
            content={"persons": [], "timeline": []},
            now=NOW + timedelta(seconds=7),
        )
    repo.commit_chunk_result(
        replacement,
        content={"persons": [], "timeline": []},
        now=NOW + timedelta(seconds=7),
    )


@pytest.mark.parametrize("phase", ["chunk", "refine", "index"])
def test_failure_waits_for_inflight_result_and_preserves_prefix_runtime(repository, phase):
    factory, queue, repo, task_id, video_lease, prepared = _prepare(repository)
    claimed_at = NOW + timedelta(minutes=1)
    if phase == "chunk":
        assert queue.dispatch_chunk(
            prepared.chunk_parse_task_ids[1], video_lease, now=claimed_at
        )
        inflight = queue.claim_chunk(
            "inflight-chunk", lease_duration=timedelta(minutes=5), now=claimed_at
        )
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
                    interval_content={"summary": f"interval-{index}"},
                    member_updates={},
                    now=claimed_at,
                )
                inflight = queue.claim_interval_refine(
                    "next-refine", lease_duration=timedelta(minutes=5), now=claimed_at
                )
            inflight = queue.claim_interval_index(
                "inflight-index", lease_duration=timedelta(minutes=5), now=claimed_at
            )
    assert inflight is not None
    failure_status = "INDEXING_FAILED" if phase == "index" else "PARSING_FAILED"
    with factory.begin() as session:
        task = session.get(VideoParseTaskRecord, task_id)
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
            session.get(ChunkParseTaskRecord, prepared.chunk_parse_task_ids[0]).status = "FAILED"
        else:
            failed_id = next(
                child_id for child_id in prepared.interval_parse_task_ids
                if child_id != inflight.interval_parse_task_id
            )
            session.get(IntervalParseTaskRecord, failed_id).status = failure_status

    draining = repo.aggregate_task(video_lease, now=claimed_at)
    assert draining.status == ("INDEXING" if phase == "index" else "PARSING")
    with factory() as session:
        parent = session.get(VideoParseTaskRecord, task_id)
        assert parent.finish_time is None
        assert parent.lease_owner == video_lease.worker_id
        assert parent.detail["_worker_runtime_v2"]["failure_status"] == failure_status
        assert session.get(VideoRecord, parent.video_id).parse_status == draining.status

    committed_at = claimed_at + timedelta(seconds=1)
    if phase == "chunk":
        assert queue.heartbeat_chunk(
            inflight, lease_duration=timedelta(minutes=5), now=committed_at
        )
        repo.commit_chunk_result(
            inflight,
            content={"persons": [], "timeline": [{"value": "retained-inflight"}]},
            now=committed_at,
        )
    elif phase == "refine":
        assert queue.heartbeat_interval(
            inflight, lease_duration=timedelta(minutes=5), now=committed_at
        )
        repo.commit_interval_refinement(
            inflight,
            interval_content={"summary": "retained-inflight"},
            member_updates={},
            now=committed_at,
        )
    else:
        assert queue.heartbeat_interval(
            inflight, lease_duration=timedelta(minutes=5), now=committed_at
        )
        snapshot = repo.load_interval_indexing_snapshot(inflight, now=committed_at)
        repo.complete_interval_indexing(
            inflight,
            {item.chunk_parse_task_id: item.content_version for item in snapshot.chunks},
            now=committed_at,
        )

    assert repo.aggregate_task(video_lease, now=committed_at).status == failure_status
    with factory() as session:
        parent = session.get(VideoParseTaskRecord, task_id)
        assert parent.finish_time is not None
        assert parent.lease_owner is None
        runtime = parent.detail["_worker_runtime_v2"]
        assert runtime["failure_status"] == failure_status
        assert runtime["prefix"] == prefix
        assert runtime["model_calls"] == {"used_call_ids": ["call-1", "call-2"], "active": {}}
        if phase == "chunk":
            content = session.scalar(select(ChunkParseContentRecord).where(
                ChunkParseContentRecord.chunk_parse_task_id == inflight.chunk_parse_task_id
            ))
            assert content.content["timeline"] == [{"value": "retained-inflight"}]
        elif phase == "refine":
            content = session.get(IntervalParseContentRecord, inflight.interval_parse_task_id)
            assert content.content == {"summary": "retained-inflight"}
        else:
            assert session.get(
                IntervalParseTaskRecord, inflight.interval_parse_task_id
            ).status == "COMPLETE"


def test_failure_does_not_treat_expired_but_unreleased_child_as_drained(repository):
    factory, _queue, repo, task_id, video_lease, prepared = _prepare(repository, chunk_count=2)
    with factory.begin() as session:
        session.get(ChunkParseTaskRecord, prepared.chunk_parse_task_ids[0]).status = "FAILED"
        unfinished = session.get(ChunkParseTaskRecord, prepared.chunk_parse_task_ids[1])
        unfinished.status = "UNDERSTANDING"
        unfinished.lease_owner = "expired-worker"
        unfinished.lease_expire_time = NOW

    assert repo.aggregate_task(video_lease, now=NOW + timedelta(seconds=1)).status == "PARSING"
    with factory() as session:
        parent = session.get(VideoParseTaskRecord, task_id)
        assert parent.finish_time is None
        assert parent.detail["_worker_runtime_v2"]["failure_status"] == "PARSING_FAILED"

    with factory.begin() as session:
        unfinished = session.get(ChunkParseTaskRecord, prepared.chunk_parse_task_ids[1])
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
    assert queue.dispatch_chunk(
        prepared.chunk_parse_task_ids[0], video_lease, now=NOW
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
        with pytest.raises(VideoParseLeaseLostError):
            repo.load_chunk_execution(forged, now=NOW)
        with pytest.raises(VideoParseLeaseLostError):
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
        with pytest.raises(VideoParseLeaseLostError):
            repo.load_interval_members(
                forged,
                now=NOW + timedelta(minutes=1),
            )
        with pytest.raises(VideoParseLeaseLostError):
            repo.commit_interval_refinement(
                forged,
                interval_content={"summary": "forged"},
                member_updates={},
                now=NOW + timedelta(minutes=1),
            )

    repo.commit_interval_refinement(
        lease,
        interval_content={"summary": "valid"},
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
        interval_content={"summary": "short"},
        member_updates={},
        now=NOW + timedelta(minutes=1, seconds=1),
    )
    indexing = queue.claim_interval_index(
        "index", lease_duration=timedelta(minutes=5), now=NOW + timedelta(minutes=1, seconds=2)
    )
    assert indexing is not None
    snapshot = repo.load_interval_indexing_snapshot(indexing, now=NOW + timedelta(minutes=1, seconds=2))
    wrong = {snapshot.chunks[0].chunk_parse_task_id: snapshot.chunks[0].content_version + 1}
    with pytest.raises(VideoParseConflictError, match="version changed"):
        repo.complete_interval_indexing(
            indexing, wrong, now=NOW + timedelta(minutes=1, seconds=3)
        )


def test_index_rebuild_preserves_results_and_fences_the_old_index_lease(repository):
    factory, queue, repo, task_id, video_lease, prepared = _prepare(
        repository, chunk_count=2
    )
    _commit_all_chunks(factory, queue, repo, video_lease, prepared, count=2)
    refine = queue.claim_interval_refine(
        "refine",
        lease_duration=timedelta(minutes=5),
        now=NOW + timedelta(minutes=1),
    )
    assert refine is not None
    repo.commit_interval_refinement(
        refine,
        interval_content={"summary": "preserved"},
        member_updates={},
        now=NOW + timedelta(minutes=1, seconds=1),
    )
    initial_index = queue.claim_interval_index(
        "initial-index",
        lease_duration=timedelta(minutes=5),
        now=NOW + timedelta(minutes=1, seconds=2),
    )
    assert initial_index is not None
    snapshot = repo.load_interval_indexing_snapshot(
        initial_index, now=NOW + timedelta(minutes=1, seconds=2)
    )
    first_chunk = snapshot.chunks[0]
    repo.save_index_embeddings(
        initial_index,
        first_chunk.chunk_parse_task_id,
        first_chunk.content_version,
        {"semantic": {"vector": [1.0]}},
        now=NOW + timedelta(minutes=1, seconds=2),
    )
    indexed_versions = {
        item.chunk_parse_task_id: item.content_version for item in snapshot.chunks
    }
    repo.complete_interval_indexing(
        initial_index,
        indexed_versions,
        now=NOW + timedelta(minutes=1, seconds=3),
    )
    assert repo.aggregate_task(
        video_lease, now=NOW + timedelta(minutes=1, seconds=4)
    ).status == "COMPLETE"

    first_rebuild_at = NOW + timedelta(minutes=2)
    assert repo.request_index_rebuild(
        task_id, now=first_rebuild_at
    ) == (initial_index.interval_parse_task_id,)
    with factory() as session:
        task = session.get(VideoParseTaskRecord, task_id)
        interval = session.get(
            IntervalParseTaskRecord, initial_index.interval_parse_task_id
        )
        interval_content = session.get(
            IntervalParseContentRecord, initial_index.interval_parse_task_id
        )
        chunk_tasks = list(session.scalars(
            select(ChunkParseTaskRecord).where(
                ChunkParseTaskRecord.video_parse_task_id == task_id
            )
        ))
        chunk_content = session.scalar(
            select(ChunkParseContentRecord).where(
                ChunkParseContentRecord.chunk_parse_task_id
                == first_chunk.chunk_parse_task_id
            )
        )
        assert task.status == "INDEXING"
        assert task.next_execute_time == first_rebuild_at
        assert task.attempt_count == 0
        assert task.last_error is None
        assert interval.status == "INDEXING"
        assert interval.lease_owner is None
        assert interval.lease_version == initial_index.lease_version + 1
        assert interval.next_execute_time == first_rebuild_at
        assert interval_content.content == {"summary": "preserved"}
        assert interval_content.detail["indexed_versions"] == indexed_versions
        assert chunk_content.detail["_index_embeddings_v2"]["representations"] == {
            "semantic": {"vector": [1.0]}
        }
        assert {item.status for item in chunk_tasks} == {"COMPLETE"}

    rebuild_coordinator = queue.claim_video(
        "rebuild-coordinator",
        lease_duration=timedelta(minutes=5),
        now=first_rebuild_at,
    )
    assert rebuild_coordinator is not None
    assert rebuild_coordinator.video_parse_task_id == task_id

    old_index = queue.claim_interval_index(
        "old-index",
        lease_duration=timedelta(minutes=5),
        video_parse_task_id=task_id,
        now=first_rebuild_at,
    )
    assert old_index is not None
    second_rebuild_at = NOW + timedelta(minutes=2, seconds=1)
    assert repo.request_index_rebuild(
        task_id, now=second_rebuild_at
    ) == (old_index.interval_parse_task_id,)
    with pytest.raises(VideoParseLeaseLostError):
        repo.complete_interval_indexing(
            old_index,
            indexed_versions,
            now=NOW + timedelta(minutes=2, seconds=2),
        )

    replacement = queue.claim_interval_index(
        "replacement-index",
        lease_duration=timedelta(minutes=5),
        video_parse_task_id=task_id,
        now=NOW + timedelta(minutes=2, seconds=2),
    )
    assert replacement is not None
    assert replacement.lease_version > old_index.lease_version


def test_index_rebuild_rejects_nonlatest_and_deleted_tasks(repository):
    factory, _queue, repo, task_id, _video_lease, _prepared = _prepare(
        repository, chunk_count=2
    )
    newer_id = str(uuid4())
    with factory.begin() as session:
        current = session.get(VideoParseTaskRecord, task_id)
        session.add(VideoParseTaskRecord(
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
            create_time=NOW + timedelta(seconds=1),
            update_time=NOW + timedelta(seconds=1),
        ))

    with pytest.raises(VideoParseConflictError, match="latest"):
        repo.request_index_rebuild(task_id, now=NOW + timedelta(minutes=1))

    with factory.begin() as session:
        session.get(VideoParseTaskRecord, newer_id).is_deleted = True
    with pytest.raises(VideoParseTaskNotFoundError):
        repo.request_index_rebuild(newer_id, now=NOW + timedelta(minutes=1))


@pytest.mark.parametrize("has_failure_intent", [False, True])
def test_index_rebuild_clears_only_settled_index_failure_intent(repository, has_failure_intent):
    factory, _queue, repo, task_id = _prepare_two_chunk_prefix_chain(repository)
    with factory.begin() as session:
        task = session.get(VideoParseTaskRecord, task_id)
        task.status = "INDEXING_FAILED"
        runtime = {
            **task.detail["_worker_runtime_v2"],
            "model_calls": {"used_call_ids": ["call-1", "call-2"], "active": {}},
            "prefix": {**task.detail["_worker_runtime_v2"]["prefix"], "lease_version": 7},
        }
        if has_failure_intent:
            runtime["failure_status"] = "INDEXING_FAILED"
        task.detail = {**task.detail, "_worker_runtime_v2": runtime}
        interval = session.scalar(select(IntervalParseTaskRecord).where(
            IntervalParseTaskRecord.video_parse_task_id == task_id
        ))
        interval.status = "INDEXING_FAILED"
        interval_id = interval.id
        content = session.get(IntervalParseContentRecord, interval_id).content

    assert repo.request_index_rebuild(
        task_id, now=NOW + timedelta(minutes=2)
    ) == (interval_id,)
    with factory() as session:
        parent = session.get(VideoParseTaskRecord, task_id)
        assert parent.status == "INDEXING"
        assert parent.finish_time is None
        assert parent.detail["_worker_runtime_v2"] == {
            key: value for key, value in runtime.items() if key != "failure_status"
        }
        assert session.get(IntervalParseContentRecord, interval_id).content == content


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
        task = session.get(VideoParseTaskRecord, task_id)
        task.status = "INDEXING" if owner is not None else failure_status
        task.detail = {
            **task.detail,
            "_worker_runtime_v2": {
                **task.detail["_worker_runtime_v2"],
                "failure_status": failure_status,
            },
        }
        interval = session.scalar(select(IntervalParseTaskRecord).where(
            IntervalParseTaskRecord.video_parse_task_id == task_id
        ))
        interval.status = "INDEXING" if owner is not None else "INDEXING_FAILED"
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
        task = session.get(VideoParseTaskRecord, task_id)
        interval = session.get(IntervalParseTaskRecord, interval_id)
        assert (
            task.status, task.detail, interval.status,
            interval.lease_owner, interval.lease_version,
        ) == before


def test_model_call_budget_and_concurrency_survive_worker_reentry(repository):
    _factory, _queue, repo, task_id, video_lease, _prepared = _prepare(
        repository, chunk_count=2
    )
    first = repo.reserve_model_call(
        video_lease,
        "call-1",
        lease_duration=timedelta(minutes=1),
        now=NOW,
    )
    assert (first.used_calls, first.active_calls) == (1, 1)
    with pytest.raises(ModelCallConcurrencyBusyError):
        repo.reserve_model_call(
            video_lease,
            "call-2",
            lease_duration=timedelta(minutes=1),
            now=NOW,
        )
    assert repo.renew_model_call(
        video_lease,
        "call-1",
        lease_duration=timedelta(minutes=1),
        now=NOW + timedelta(seconds=30),
    )
    assert not repo.renew_model_call(
        video_lease,
        "call-1",
        lease_duration=timedelta(minutes=1),
        now=NOW + timedelta(minutes=2),
    )
    assert repo.release_model_call(
        task_id, "call-1", video_lease.worker_id, now=NOW + timedelta(minutes=2)
    )

    # Reusing the stable Gateway call_id does not consume budget again.
    repeated = repo.reserve_model_call(
        video_lease,
        "call-1",
        lease_duration=timedelta(minutes=1),
        now=NOW + timedelta(minutes=2),
    )
    assert repeated.used_calls == 1 and not repeated.first_reservation
    assert repo.release_model_call(
        task_id, "call-1", video_lease.worker_id, now=NOW + timedelta(minutes=2)
    )
    for call_id in ("call-2", "call-3"):
        repo.reserve_model_call(
            video_lease,
            call_id,
            lease_duration=timedelta(minutes=1),
            now=NOW + timedelta(minutes=2),
        )
        assert repo.release_model_call(
            task_id, call_id, video_lease.worker_id, now=NOW + timedelta(minutes=2)
        )
    with pytest.raises(ModelCallBudgetExhaustedError):
        repo.reserve_model_call(
            video_lease,
            "call-4",
            lease_duration=timedelta(minutes=1),
            now=NOW + timedelta(minutes=2),
        )


def test_chunk_and_interval_model_slots_fence_the_actual_execution_lease(repository):
    factory, queue, repo, task_id, video_lease, prepared = _prepare(
        repository, chunk_count=2
    )
    assert queue.dispatch_chunk(prepared.chunk_parse_task_ids[0], video_lease, now=NOW)
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

    assert queue.dispatch_chunk(
        prepared.chunk_parse_task_ids[1], video_lease, now=NOW + timedelta(seconds=33)
    )
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
        interval_content={"summary": "committed"},
        member_updates={},
        now=NOW + timedelta(minutes=1, seconds=32),
    )
    assert queue.is_execution_lease_committed(interval_lease)
    index_lease = queue.claim_interval_index(
        "interval-index",
        lease_duration=timedelta(minutes=5),
        now=NOW + timedelta(minutes=1, seconds=33),
    )
    assert index_lease is not None
    # The refinement's exact committed marker survives the next phase claim,
    # which increments the Interval row's current lease version.
    assert queue.is_execution_lease_committed(interval_lease)
    snapshot = repo.load_interval_indexing_snapshot(
        index_lease, now=NOW + timedelta(minutes=1, seconds=33)
    )
    repo.complete_interval_indexing(
        index_lease,
        {
            item.chunk_parse_task_id: item.content_version
            for item in snapshot.chunks
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
        video = session.get(VideoRecord, video_lease.video_id)
        video.inference_proxy_uri = proxy_ref
        chunks = list(session.scalars(
            select(VideoChunkRecord)
            .where(VideoChunkRecord.video_id == video_lease.video_id)
            .order_by(VideoChunkRecord.sequence_no)
        ))
        for index, chunk in enumerate(chunks):
            # These are valid V1 local artifacts, but their time origin is 0.
            chunk.chunk_media_ref = f"artifact://v1/chunk-{index}.mp4"
            chunk.analysis_window_media_ref = f"artifact://v1/window-{index}.mp4"

    for index, child_id in enumerate(prepared.chunk_parse_task_ids):
        at = NOW + timedelta(seconds=index)
        assert queue.dispatch_chunk(child_id, video_lease, now=at)
        lease = queue.claim_chunk(
            f"source-{index}", lease_duration=timedelta(minutes=5), now=at
        )
        assert lease is not None
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
        interval_content={"summary": "source"},
        member_updates={},
        now=NOW + timedelta(minutes=1, seconds=1),
    )
    indexing = queue.claim_interval_index(
        "source-index",
        lease_duration=timedelta(minutes=5),
        now=NOW + timedelta(minutes=1, seconds=2),
    )
    assert indexing is not None
    snapshot = repo.load_interval_indexing_snapshot(
        indexing, now=NOW + timedelta(minutes=1, seconds=2)
    )
    assert snapshot.chunks
    assert {item.source_media_ref for item in snapshot.chunks} == {proxy_ref}
    assert all(
        item.chunk_media_ref.startswith("artifact://v1/")
        for item in snapshot.chunks
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
        session.get(VideoParseTaskRecord, task_id).is_deleted = True
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
    with pytest.raises(VideoParseLeaseLostError):
        repo.commit_prefix(
            first,
            content_updates={},
            video_prefix_compact="deleted",
            now=NOW + timedelta(minutes=2, milliseconds=200),
        )
    with factory.begin() as session:
        session.get(VideoParseTaskRecord, task_id).is_deleted = False
    assert repo.retry_prefix(
        first,
        error="previous failure",
        retry_at=NOW + timedelta(minutes=2, seconds=1),
        now=NOW + timedelta(minutes=2, milliseconds=300),
    )
    retried = repo.claim_next_prefix_any(
        "prefix-retried",
        lease_duration=timedelta(minutes=5),
        now=NOW + timedelta(minutes=2, seconds=1),
    )
    assert retried is not None and retried.retry_count == 1
    assert repo.release_prefix(
        retried,
        now=NOW + timedelta(minutes=2, seconds=1, milliseconds=100),
    )
    assert not repo.release_prefix(
        retried,
        now=NOW + timedelta(minutes=2, seconds=1, milliseconds=200),
    )
    assert not repo.release_prefix(
        first,
        now=NOW + timedelta(minutes=2, seconds=1, milliseconds=200),
    )
    with factory() as session:
        task = session.get(VideoParseTaskRecord, task_id)
        prefix = task.detail["_worker_runtime_v2"]["prefix"]
        assert prefix["lease_owner"] is None
        assert prefix["lease_expire_time_ms"] is None
        assert prefix["execution_task_id"] is None
        assert prefix["next_execute_time_ms"] == int(
            (NOW + timedelta(minutes=2, seconds=1, milliseconds=100)).timestamp()
            * 1000
        )
        assert prefix["retry_count"] == 1
        assert prefix["last_error"] == {"message": "previous failure"}
    first = repo.claim_next_prefix_any(
        "prefix-after-cancel",
        lease_duration=timedelta(minutes=5),
        now=NOW + timedelta(minutes=2, seconds=1, milliseconds=100),
    )
    assert first is not None
    assert first.retry_count == 1
    repo.commit_prefix(
        first,
        content_updates={},
        video_prefix_compact="chunk zero",
        now=NOW + timedelta(minutes=2, seconds=1, milliseconds=200),
    )
    assert queue.is_execution_lease_committed(first)
    second = repo.claim_next_prefix_any(
        "prefix",
        lease_duration=timedelta(minutes=5),
        now=NOW + timedelta(minutes=2, seconds=2),
    )
    assert second is not None and second.sequence_no == 1
    assert queue.is_execution_lease_committed(first)
    repo.commit_prefix(
        second,
        content_updates={"persons": ["P"]},
        video_prefix_compact="chunk zero and one",
        series_prefix_compact="episode summary",
        now=NOW + timedelta(minutes=2, seconds=3),
    )
    assert queue.is_execution_lease_committed(first)
    assert queue.is_execution_lease_committed(second)
    with factory() as session:
        content = session.scalar(
            select(ChunkParseContentRecord).where(
                ChunkParseContentRecord.chunk_parse_task_id
                == second.chunk_parse_task_id
            )
        )
        task = session.get(VideoParseTaskRecord, task_id)
        prefix = task.detail["_worker_runtime_v2"]["prefix"]
        assert content.detail["scope"] == [0, 1]
        assert content.version == second.content_version + 1
        assert prefix["next_chunk_sequence_no"] == 2
        assert prefix["lease_owner"] is None
        assert task.status == "INDEXING"
        assert task.lease_owner is None


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
        task = session.get(VideoParseTaskRecord, task_id)
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
        task = session.get(VideoParseTaskRecord, task_id)
        assert task.status == failure_status
        assert task.finish_time == failed_at
        assert task.detail["_worker_runtime_v2"]["failure_status"] == failure_status
        prefix = task.detail["_worker_runtime_v2"]["prefix"]
        assert prefix["next_chunk_sequence_no"] == 1
        assert prefix["video_prefix_compact"] == "retained-prefix"
        assert prefix["lease_owner"] is None
        content = session.scalar(select(ChunkParseContentRecord).where(
            ChunkParseContentRecord.chunk_parse_task_id == prefix_lease.chunk_parse_task_id
        ))
        assert content.content["persons"] == ["retained-prefix-update"]
        assert content.version == prefix_lease.content_version + 1


def test_prefix_frontier_never_falls_back_to_an_older_parse_version(repository):
    factory, queue, repo, _task_id, video_lease, prepared = _prepare(
        repository, chunk_count=1
    )
    with factory.begin() as session:
        current = session.get(
            VideoParseTaskRecord, video_lease.video_parse_task_id
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
        interval_content={"summary": "old"},
        member_updates={},
        now=NOW + timedelta(minutes=1, seconds=1),
    )
    index_lease = queue.claim_interval_index(
        "old-index",
        lease_duration=timedelta(minutes=5),
        now=NOW + timedelta(minutes=1, seconds=2),
    )
    assert index_lease is not None
    snapshot = repo.load_interval_indexing_snapshot(
        index_lease, now=NOW + timedelta(minutes=1, seconds=2)
    )
    repo.complete_interval_indexing(
        index_lease,
        {
            item.chunk_parse_task_id: item.content_version
            for item in snapshot.chunks
        },
        now=NOW + timedelta(minutes=1, seconds=3),
    )
    assert repo.aggregate_task(
        video_lease, now=NOW + timedelta(minutes=1, seconds=4)
    ).status == "COMPLETE"

    with factory.begin() as session:
        old = session.get(
            VideoParseTaskRecord, video_lease.video_parse_task_id
        )
        video = session.get(VideoRecord, old.video_id)
        newer_id = str(uuid4())
        session.add(VideoParseTaskRecord(
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
            create_time=NOW + timedelta(minutes=2),
            update_time=NOW + timedelta(minutes=2),
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
        session.add(SeriesRecord(
            series_id=series_id,
            workspace_id="mine",
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
            session.add(VideoRecord(
                video_id=video_id,
                series_id=series_id,
                episode_order=order,
                upload_sequence=order,
                original_filename=f"episode-{order}.mp4",
                original_source_uri=f"artifact://test/{order}.mp4",
                content_hash=(str(order) * 64)[:64],
                total_bytes=100,
                duration_seconds=10,
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
            session.add(VideoParseTaskRecord(
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
                create_time=NOW,
                update_time=NOW,
            ))
            if order == 3:
                session.add(VideoChunkRecord(
                    video_chunk_id=stale_video_chunk_id,
                    video_id=video_id,
                    sequence_no=0,
                    start_seconds=0,
                    end_seconds=10,
                    analysis_window_start_seconds=0,
                    analysis_window_end_seconds=10,
                    chunk_media_ref="artifact://test/chunk-3.mp4",
                    analysis_window_media_ref="artifact://test/window-3.mp4",
                    transcript=[],
                    baseline_visual_caption=None,
                    keyframe_uris=[],
                ))
                session.add(ChunkParseTaskRecord(
                    id=stale_chunk_task_id,
                    video_parse_task_id=task_id,
                    video_chunk_id=stale_video_chunk_id,
                    status="COMPLETE",
                    next_execute_time=NOW,
                    retry_count=0,
                    lease_version=1,
                    finish_time=NOW,
                ))
                session.add(ChunkParseContentRecord(
                    id=retained_content_id,
                    chunk_parse_task_id=stale_chunk_task_id,
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
    queue = MySqlVideoParseQueue(factory)
    repo = VideoParseRepository(factory)
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
                VideoParseTaskRecord, task_ids[0]
            ).video_id
            invalidate_series_prefixes_from(
                session,
                series_id=series_id,
                episode_order=1,
                video_id=first_video,
                now=NOW + timedelta(minutes=1),
            )
    with factory.begin() as session:
        third = session.get(VideoParseTaskRecord, task_ids[2])
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
            VideoParseTaskRecord, task_ids[0]
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
            session.get(VideoParseTaskRecord, task_id)
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
        retained = session.get(ChunkParseContentRecord, retained_content_id)
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
        retry_at=NOW + timedelta(minutes=2),
        now=NOW + timedelta(minutes=1, seconds=1),
    )
    with pytest.raises(VideoParseLeaseLostError):
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
        task = session.get(VideoParseTaskRecord, task_id)
        video = session.get(VideoRecord, task.video_id)
        series_id = video.series_id
        video_id = video.video_id
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
        task = session.get(VideoParseTaskRecord, task_id)
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
            task = session.get(VideoParseTaskRecord, task_id)
            contents = session.scalars(
                select(ChunkParseContentRecord)
                .where(ChunkParseContentRecord.chunk_parse_task_id.in_(
                    [new_zero.chunk_parse_task_id, new_one.chunk_parse_task_id]
                ))
                .order_by(ChunkParseContentRecord.id)
            )
            return task.detail, task.update_time, [
                (row.id, row.content, row.version, row.detail, row.update_time)
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
        retry_at=stale_attempt_at + timedelta(minutes=1),
        now=stale_attempt_at,
    )
    assert persisted_chain_state() == before_stale_attempts
    with pytest.raises(VideoParseLeaseLostError):
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
        task = session.get(VideoParseTaskRecord, task_id)
        prefix = task.detail["_worker_runtime_v2"]["prefix"]
        zero_content = session.scalar(
            select(ChunkParseContentRecord).where(
                ChunkParseContentRecord.chunk_parse_task_id
                == new_zero.chunk_parse_task_id
            )
        )
        one_content = session.scalar(
            select(ChunkParseContentRecord).where(
                ChunkParseContentRecord.chunk_parse_task_id
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
        task = session.get(VideoParseTaskRecord, task_id)
        prefix = task.detail["_worker_runtime_v2"]["prefix"]
        committed = prefix["committed_leases"]
        one_content = session.scalar(
            select(ChunkParseContentRecord).where(
                ChunkParseContentRecord.chunk_parse_task_id
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


def test_v2_retrieval_scope_and_hydrate_are_pinned_to_latest_complete_task(
    repository,
) -> None:
    factory, task_id = _seed(repository, chunk_count=1)
    repo = VideoParseRepository(factory)
    chunk_id = str(uuid4())
    child_id = str(uuid4())
    content = VideoParseResult.model_validate({
        "persons": ["千早爱音"],
        "timeline": [{
            "event_type": "DIALOGUE",
            "start_seconds": 1,
            "end_seconds": 3,
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
    projection = project_chunk_index(
        content,
        chunk_start_seconds=0,
        chunk_end_seconds=10,
    )
    receipt = {
        "parse_task_id": task_id,
        "video_chunk_id": chunk_id,
        "content_version": 1,
        "source_revision": compute_source_revision(content),
        "projection_revision": compute_index_projection_revision(projection),
        "retrieval_contract_id": "retrieval-v2",
        "semantic_encoder_id": "semantic-v2",
        "vision_encoder_id": "vision-v2",
        "index_generation": 2,
    }
    with factory.begin() as session:
        task = session.get(VideoParseTaskRecord, task_id)
        assert task is not None
        task.status = "COMPLETE"
        task.finish_time = NOW
        video = session.get(VideoRecord, task.video_id)
        assert video is not None
        video.parse_status = "COMPLETE"
        video.parse_progress = 100
        session.add(VideoChunkRecord(
            video_chunk_id=chunk_id,
            video_id=task.video_id,
            sequence_no=0,
            start_seconds=0,
            end_seconds=10,
            analysis_window_start_seconds=0,
            analysis_window_end_seconds=10,
            chunk_media_ref="artifact://v2/chunk.mp4",
            analysis_window_media_ref="artifact://v2/window.mp4",
            transcript=[],
            baseline_visual_caption=None,
            keyframe_uris=[],
        ))
        session.add(ChunkParseTaskRecord(
            id=child_id,
            video_parse_task_id=task_id,
            video_chunk_id=chunk_id,
            status="COMPLETE",
            next_execute_time=NOW,
            retry_count=0,
            lease_version=0,
            detail=None,
            create_time=NOW,
            update_time=NOW,
        ))
        session.add(ChunkParseContentRecord(
            id=str(uuid4()),
            chunk_parse_task_id=child_id,
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
            create_time=NOW,
            update_time=NOW,
        ))

    scope = repo.resolve_retrieval_scope([video.series_id], workspace_id="mine")
    assert scope.searched_video_ids == (video.video_id,)
    assert scope.parse_tasks_by_video_id == {video.video_id: task_id}
    identity = VideoChunkRetrievalIdentity(
        workspace_id="mine",
        series_id=video.series_id,
        video_id=video.video_id,
        parse_task_id=task_id,
        content_version=1,
        index_generation=2,
        source_revision=receipt["source_revision"],
        projection_revision=receipt["projection_revision"],
        retrieval_contract_id="retrieval-v2",
        semantic_encoder_id="semantic-v2",
        vision_encoder_id="vision-v2",
    )
    evidence = repo.hydrate_retrieval_evidence([(chunk_id, identity)])
    assert evidence[0].video_chunk_id == chunk_id
    assert evidence[0].transcript[0].text == "我没事"
    assert evidence[0].parse_provenance["contentVersion"] == 1

    empty_series_id = str(uuid4())
    with factory.begin() as session:
        session.add(SeriesRecord(
            series_id=empty_series_id,
            workspace_id="mine",
            name="Empty Series",
            description=None,
            status="INDEXING",
            important_extra_data=None,
            character_bible=[],
        ))
    partial_scope = repo.resolve_retrieval_scope(
        [video.series_id, empty_series_id],
        workspace_id="mine",
    )
    assert partial_scope.partial is True
    assert partial_scope.searched_video_ids == (video.video_id,)
    assert "没有视频" in partial_scope.warnings[0]

    with factory.begin() as session:
        session.add(VideoParseTaskRecord(
            id=str(uuid4()),
            video_id=video.video_id,
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
            create_time=NOW + timedelta(minutes=1),
            update_time=NOW + timedelta(minutes=1),
        ))

    with pytest.raises(SeriesNotReadyError, match="no COMPLETE"):
        repo.resolve_retrieval_scope([video.series_id], workspace_id="mine")
    with pytest.raises(VideoParseConflictError, match="latest V2 task"):
        repo.hydrate_retrieval_evidence([(chunk_id, identity)])
