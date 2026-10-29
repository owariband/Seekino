from __future__ import annotations

from copy import deepcopy
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from agent.video_summarize.state import VideoSummaryResult, VideoSummarySubmission
from server.constants.parse import RUNTIME_DETAIL_KEY
from server.model.job import ChunkParseContent, VideoParseTask
from server.model.video import Video
from server.service.video_parse_errors import VideoParseLeaseLostError, VideoParseSnapshotConflictError
from tests.server.test_video_parse_repository import (
    NOW, _prepare, _commit_all_chunks, _refinement_inputs, _finish_ready_indexes,
)


SNAPSHOT = {"model": {"source": "test"}, "limits": {"max_retries": 2}}
AT = NOW + timedelta(minutes=1)


def _ready(repository, *, chunks=1):
    factory, queue, repo, task_id, video_lease, prepared = _prepare(repository, chunk_count=chunks)
    _commit_all_chunks(factory, queue, repo, video_lease, prepared, count=chunks)
    while (refine := queue.claim_interval_refine("refine", lease_duration=timedelta(minutes=5), now=AT)):
        repo.commit_interval_refinement(
            refine, **_refinement_inputs(repo, refine, now=AT), interval_content={"summary": "done"},
            member_updates={}, now=AT,
        )
    return factory, queue, repo, task_id, video_lease


def _claim(repo, *, at=AT, snapshot=SNAPSHOT):
    return repo.claim_next_summary("summary", lease_duration=timedelta(minutes=5), config_snapshot=snapshot, now=at)


def _state(factory, task_id):
    with factory() as session:
        return deepcopy(session.get(VideoParseTask, task_id).detail[RUNTIME_DETAIL_KEY]["summarize"])


def _result(*, order=3):
    return VideoSummaryResult(VideoSummarySubmission(
        title="迷路", description="两人终于在街角相遇。", episode_order=order,
        evidence=[{"field": "description", "chunk_parse_task_id": "source", "quote": "相遇"}],
    ), None, ({"inspection_id": "receipt", "frames": [{"requested_timestamp_ms": 1000}]},))


def test_only_last_committed_refinement_queues_summary(repository):
    factory, queue, repo, task_id, video_lease, prepared = _prepare(repository, chunk_count=4)
    _commit_all_chunks(factory, queue, repo, video_lease, prepared, count=4)
    assert not repo.mark_summary_ready(task_id, now=AT)
    refine = queue.claim_interval_refine("first", lease_duration=timedelta(minutes=5), now=AT)
    repo.commit_interval_refinement(refine, **_refinement_inputs(repo, refine, now=AT),
                                   interval_content={"summary": "first"}, member_updates={}, now=AT)
    assert _claim(repo) is None
    refine = queue.claim_interval_refine("last", lease_duration=timedelta(minutes=5), now=AT)
    repo.commit_interval_refinement(refine, **_refinement_inputs(repo, refine, now=AT),
                                   interval_content={"summary": "last"}, member_updates={}, now=AT)
    assert _state(factory, task_id)["status"] == "PENDING"
    assert _claim(repo) is not None


def test_summary_does_not_wait_for_index_prefix_or_block_parse_completion(repository):
    factory, queue, repo, task_id, video_lease = _ready(repository)
    with factory.begin() as session:
        task = session.get(VideoParseTask, task_id)
        task.parse_config = {**task.parse_config, "compact_enabled": True}
    lease = _claim(repo)
    assert lease is not None
    assert len(repo.load_summary_input(lease, now=AT).chunks) == 1
    _finish_ready_indexes(queue, repo, now=AT)
    queue.release_execution(video_lease, now=AT)
    assert repo.load_task_input(task_id).task.status == "COMPLETE"
    assert _state(factory, task_id)["status"] == "RUNNING"


def test_lease_claim_recovery_and_config_snapshot_are_independent(repository):
    factory, _queue, repo, task_id, _video_lease = _ready(repository)
    first = _claim(repo)
    assert _claim(repo) is None
    assert repo.heartbeat_summary(first, lease_duration=timedelta(minutes=5), now=AT + timedelta(minutes=1))
    assert _claim(repo, at=AT + timedelta(minutes=5)) is None
    second = _claim(repo, at=AT + timedelta(minutes=7), snapshot={"limits": {"max_retries": 99}})
    assert second.retry_count == 1 and second.config_snapshot == SNAPSHOT
    assert second.lease_version > first.lease_version
    assert not repo.heartbeat_summary(first, lease_duration=timedelta(minutes=5), now=AT + timedelta(minutes=7))
    assert not repo.release_summary(first, now=AT + timedelta(minutes=7))


def test_retry_budget_is_terminal_and_does_not_change_parent_status(repository):
    factory, _queue, repo, task_id, _video_lease = _ready(repository)
    parent = repo.load_task_input(task_id).task.status
    lease = _claim(repo)
    for attempt in range(3):
        at = AT + timedelta(seconds=attempt)
        assert repo.retry_summary(lease, error="failure", retry_at=at + timedelta(seconds=1), max_retries=2, now=at)
        assert _claim(repo, at=at) is None
        lease = _claim(repo, at=at + timedelta(seconds=1))
    assert lease is None
    assert _state(factory, task_id)["status"] == "FAILED"
    assert repo.load_task_input(task_id).task.status == parent


def test_publish_metadata_and_episode_order_atomically_and_preserve_runtime_siblings(repository):
    factory, _queue, repo, task_id, _video_lease = _ready(repository)
    lease = _claim(repo)
    with factory.begin() as session:
        task = session.get(VideoParseTask, task_id)
        detail = deepcopy(task.detail)
        detail[RUNTIME_DETAIL_KEY]["other_phase"] = {"new": True}
        task.detail = detail
    assert repo.complete_summary(lease, _result(), cover_uri="artifact://cover.jpg", now=AT)
    with factory() as session:
        video = session.get(Video, lease.video_id)
        assert (video.episode_order, video.upload_sequence) == (3, 3)
        assert (video.display_name, video.description, video.cover_uri) == ("迷路", "两人终于在街角相遇。", "artifact://cover.jpg")
        assert session.get(VideoParseTask, task_id).detail[RUNTIME_DETAIL_KEY]["other_phase"] == {"new": True}
    assert _state(factory, task_id)["status"] == "COMPLETE"
    assert _state(factory, task_id)["frame_receipts"] == list(_result().frame_receipts)
    assert _claim(repo) is None


def test_occupied_episode_keeps_existing_order_and_saves_other_metadata(repository):
    factory, _queue, repo, task_id, _video_lease = _ready(repository)
    lease = _claim(repo)
    with factory.begin() as session:
        existing = session.get(Video, lease.video_id)
        session.add(Video(id=str(uuid4()), series_id=existing.series_id, episode_order=3, upload_sequence=3,
                          filename="other.mp4", source_uri="other", hash="b" * 64, size_bytes=10,
                          duration_ms=10000, upload_status="COMPLETED", parse_status="COMPLETE",
                          upload_progress=100, parse_progress=100))
    assert repo.complete_summary(lease, _result(), cover_uri=None, now=AT)
    with factory() as session:
        video = session.get(Video, lease.video_id)
        assert video.episode_order == 1 and video.display_name == "迷路"
    assert _state(factory, task_id)["episode_order_conflict"]


def test_generated_title_is_published_with_provenance_without_changing_episode_order(repository):
    factory, _queue, repo, task_id, _video_lease = _ready(repository)
    lease = _claim(repo)
    result = VideoSummaryResult(VideoSummarySubmission(
        title="街角的重逢", title_kind="generated", description="两人终于在街角相遇。",
        evidence=[{"field": "title", "chunk_parse_task_id": "source", "quote": "在街角相遇"},
                  {"field": "description", "chunk_parse_task_id": "source", "quote": "在街角相遇"}],
    ), None)
    assert repo.complete_summary(lease, result, cover_uri=None, now=AT)
    with factory() as session:
        video = session.get(Video, lease.video_id)
        assert video.display_name == "街角的重逢"
        assert video.episode_order == 1
    saved = _state(factory, task_id)["result"]
    assert saved["title_kind"] == "generated"
    assert saved["evidence"] == result.submission.model_dump(mode="json")["evidence"]


@pytest.mark.parametrize("change", ["content", "manual_order", "deleted", "new_parse"])
def test_stale_result_cannot_modify_video_or_parent(repository, change):
    factory, _queue, repo, task_id, _video_lease = _ready(repository)
    lease = _claim(repo)
    with factory.begin() as session:
        task = session.get(VideoParseTask, task_id)
        if change == "content":
            content = session.scalar(select(ChunkParseContent))
            content.version += 1
        elif change == "manual_order":
            session.get(Video, lease.video_id).episode_order = 7
            session.get(Video, lease.video_id).upload_sequence = 7
        elif change == "deleted":
            task.is_deleted = True
        else:
            session.add(VideoParseTask(id=str(uuid4()), video_id=task.video_id,
                                      pipeline_version=task.pipeline_version, status="PENDING",
                                      parse_config=task.parse_config, chunk_count=task.chunk_count,
                                      version=2, next_execute_time=AT, max_attempts=3,
                                      detail={RUNTIME_DETAIL_KEY: {"accepted": True}}))
    with pytest.raises((VideoParseLeaseLostError, VideoParseSnapshotConflictError)):
        repo.complete_summary(lease, _result(), cover_uri="artifact://stale.jpg", now=AT)
    with factory() as session:
        video = session.get(Video, lease.video_id)
        assert video.display_name is None and video.cover_uri is None
        assert video.episode_order == (7 if change == "manual_order" else 1)


def test_new_source_after_retry_refreshes_input_and_stops_old_lease(repository):
    factory, _queue, repo, task_id, _video_lease = _ready(repository)
    first = _claim(repo)
    with factory.begin() as session:
        content = session.scalar(select(ChunkParseContent))
        content.version += 1
    with pytest.raises(VideoParseSnapshotConflictError):
        repo.load_summary_input(first, now=AT)
    assert repo.retry_summary(first, error="changed", retry_at=AT, max_retries=2, now=AT)
    second = _claim(repo)
    assert second.source_fingerprint != first.source_fingerprint
    assert second.retry_count == 0
    assert repo.load_summary_input(second, now=AT).chunks[0].version == 2


def test_no_implicit_historical_backfill_or_duplicate_ready_notification(repository):
    factory, _queue, repo, task_id, _video_lease = _ready(repository)
    lease = _claim(repo)
    assert not repo.mark_summary_ready(task_id, now=AT)
    assert repo.release_summary(lease, now=AT)
    with factory.begin() as session:
        task = session.get(VideoParseTask, task_id)
        detail = deepcopy(task.detail)
        detail[RUNTIME_DETAIL_KEY].pop("summarize")
        task.detail = detail
    assert _claim(repo) is None
    assert repo.mark_summary_ready(task_id, now=AT)
    assert _claim(repo) is not None


@pytest.mark.parametrize("order", [None, 3])
def test_publication_yields_to_busy_parent_and_rolls_back_episode_edit(repository, monkeypatch, order):
    from server.repository import video_parse_task as task_store

    factory, _queue, repo, task_id, _video_lease = _ready(repository)
    lease = _claim(repo)
    original = task_store.get_task

    def busy_parent(session, record_id, **kwargs):
        if record_id == task_id and kwargs.get("for_update"):
            assert kwargs.get("skip_locked"), "summary must not wait for Index while holding Video"
            return None
        return original(session, record_id, **kwargs)

    monkeypatch.setattr(task_store, "get_task", busy_parent)
    with pytest.raises(VideoParseSnapshotConflictError, match="busy"):
        repo.complete_summary(lease, _result(order=order), cover_uri=None, now=AT)
    with factory() as session:
        video = session.get(Video, lease.video_id)
        assert video.episode_order == 1 and video.display_name is None
    assert _state(factory, task_id)["status"] == "RUNNING"


def test_source_background_change_rolls_back_number_and_preserves_prefix(repository):
    from server.model.video import Series

    factory, _queue, repo, task_id, _video_lease = _ready(repository)
    with factory.begin() as session:
        task = session.get(VideoParseTask, task_id)
        task.parse_config = {**task.parse_config, "compact_enabled": True}
        detail = deepcopy(task.detail)
        detail[RUNTIME_DETAIL_KEY]["prefix"]["video_prefix_compact"] = "kept"
        task.detail = detail
    lease = _claim(repo)
    with factory.begin() as session:
        session.get(Series, lease.task_input.series_id).description = "New background"
    with pytest.raises(VideoParseSnapshotConflictError):
        repo.complete_summary(lease, _result(), cover_uri=None, now=AT)
    with factory() as session:
        video = session.get(Video, lease.video_id)
        assert video.episode_order == 1 and video.display_name is None
        assert session.get(VideoParseTask, task_id).detail[RUNTIME_DETAIL_KEY]["prefix"]["video_prefix_compact"] == "kept"
