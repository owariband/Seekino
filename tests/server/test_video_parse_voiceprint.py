from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import event, select

from server.model.job import ChunkParseContent, ChunkParseTask, IntervalParseTask, VideoParseTask
from server.model.upload import UploadSession
from server.model.video import Video
from server.repository.upload import UploadRepository
from server.repository.video_parse import (
    VideoParseConflictError,
    VideoParseLeaseLostError,
    VideoParseSnapshotConflictError,
)
from tests.server.test_video_parse_repository import NOW, _prepare, _refinement_inputs


def _enable(factory, task_id):
    with factory.begin() as session:
        task = session.get(VideoParseTask, task_id)
        task.parse_config = {**task.parse_config, "voiceprint": {
            "execution_timeout_seconds": 120.0, "max_tool_calls": 32,
        }}


def _dialogues(names, sequence):
    names = list(names)
    return {"persons": sorted({name for name in names if name}), "timeline": [
        {"event_type": "DIALOGUE", "speaker": name,
         "start_seconds": sequence * 10000 + 100 + index * 1500,
         "end_seconds": sequence * 10000 + 1100 + index * 1500,
         "description": "confirmed source dialogue", "participants": [name] if name else [],
         "visible_persons": [], "text_variants": [{"language": "ja", "role": "ORIGINAL", "text": f"line {index}"}]}
        for index, name in enumerate(names)
    ]}


def _commit(queue, repo, names, sequence):
    child = queue.claim_chunk(f"understand-{sequence}", lease_duration=timedelta(minutes=5), now=NOW)
    assert child is not None
    repo.commit_chunk_result(child, content=_dialogues(names, sequence), now=NOW)
    return child


def _register(repo, lease, snapshot, speaker, **overrides):
    source_ref, source = next((key, value) for key, value in snapshot["sources"].items() if value["speaker"] == speaker)
    args = {"source_ref": source_ref, "speaker": speaker,
            "start_ms": source["start_ms"] + 100, "end_ms": source["end_ms"] - 100,
            "selection_basis": "Independent attribution and clean single-speaker audio",
            "confidence": 0.9, "model": "campplus", "encoder_revision": "v1.0.0",
            "vector": [1.0] + [0.0] * 191,
            "pcm_sha256": format(source["start_ms"], "064x"), "duration_ms": 800}
    args.update(overrides)
    return repo.register_voiceprint(lease, **args, now=NOW)


def _content_row(session, chunk_id):
    return session.scalar(select(ChunkParseContent).join(
        ChunkParseTask, ChunkParseTask.id == ChunkParseContent.chunk_task_id,
    ).where(ChunkParseTask.chunk_id == chunk_id))


def test_half_snapshot_freezes_speaker_list_and_uses_original_source(repository):
    factory, queue, repo, task_id, parent, _prepared = _prepare(repository, chunk_count=5)
    _enable(factory, task_id)
    _commit(queue, repo, ["A", None], 0)
    _commit(queue, repo, ["B"], 1)
    with pytest.raises(VideoParseConflictError, match="half"):
        repo.begin_voiceprint_preparation(parent, now=NOW)
    _commit(queue, repo, ["C"], 2)
    snapshot = repo.begin_voiceprint_preparation(parent, now=NOW)
    assert snapshot["speaker_list"] == ["A", "B", "C"]
    assert set(snapshot["member_sequence_nos"].values()) == {0, 1, 2}
    assert snapshot["source_media_ref"].startswith("artifact://")
    _commit(queue, repo, ["later NPC"], 3)
    again = repo.begin_voiceprint_preparation(parent, now=NOW)
    assert again["speaker_list"] == snapshot["speaker_list"]
    assert again["member_sequence_nos"] == snapshot["member_sequence_nos"]


def test_refine_is_not_claimed_until_atomic_majority_finish(repository):
    factory, queue, repo, task_id, parent, prepared = _prepare(repository, chunk_count=2)
    _enable(factory, task_id)
    _commit(queue, repo, ["A", "B", "NPC"], 0)
    snapshot = repo.begin_voiceprint_preparation(parent, now=NOW)
    _commit(queue, repo, [], 1)
    first = _register(repo, parent, snapshot, "A")
    assert _register(repo, parent, snapshot, "A") == first
    incomplete = repo.finish_voiceprint_preparation(parent, now=NOW)
    assert incomplete["ready"] is False and incomplete["minimum_speaker_count"] == 2
    coverage = repo.get_voiceprint_speaker_list(parent, now=NOW)
    assert coverage["registered_speaker_count"] == 1
    assert coverage["minimum_speaker_count"] == 2
    assert {item["speaker"] for item in coverage["speaker_list"]} == {"A", "B", "NPC"}
    assert queue.claim_interval_refine("refine", lease_duration=timedelta(minutes=1), now=NOW) is None
    with factory() as session:
        assert session.get(VideoParseTask, task_id).detail["voiceprint"]["ready"] is False
        interval = session.get(IntervalParseTask, prepared.interval_parse_task_ids[0])
        assert (interval.status, interval.retry_count, interval.lease_version) == ("WAITING", 0, 0)
    _register(repo, parent, snapshot, "B")
    result = repo.finish_voiceprint_preparation(parent, skip_reasons={"NPC": "too short"}, now=NOW)
    assert result == {"ready": True, "registered_speaker_count": 2, "speaker_list_size": 3,
                      "missing_speakers": ["NPC"], "skip_reasons": {"NPC": "too short"}, "minimum_speaker_count": 2}
    assert repo.begin_voiceprint_preparation(parent, now=NOW) is None
    assert repo.finish_voiceprint_preparation(parent, now=NOW)["ready"] is True
    assert repo.get_voiceprint_speaker_list(parent, now=NOW)["registered_speaker_count"] == 2
    refine = queue.claim_interval_refine("refine", lease_duration=timedelta(minutes=1), now=NOW)
    assert refine is not None
    references = repo.load_voiceprint_references(refine, now=NOW)
    assert {ref["speaker"] for ref in references} == {"A", "B"}
    assert len(references) == 2


def test_zero_speakers_finishes_without_any_registration(repository):
    factory, queue, repo, task_id, parent, _prepared = _prepare(repository, chunk_count=1)
    _enable(factory, task_id)
    _commit(queue, repo, [None], 0)
    snapshot = repo.begin_voiceprint_preparation(parent, now=NOW)
    assert snapshot["sources"] == {} and snapshot["speaker_list"] == []
    assert repo.finish_voiceprint_preparation(parent, now=NOW)["registered_speaker_count"] == 0
    assert queue.claim_interval_refine("refine", lease_duration=timedelta(minutes=1), now=NOW)


def test_historical_blank_speaker_is_not_in_reference_denominator(repository):
    factory, queue, repo, task_id, parent, _prepared = _prepare(repository, chunk_count=1)
    _enable(factory, task_id)
    _commit(queue, repo, [None], 0)
    with factory.begin() as session:
        child = session.scalar(select(ChunkParseTask).where(ChunkParseTask.video_task_id == task_id))
        result = session.scalar(select(ChunkParseContent).where(ChunkParseContent.chunk_task_id == child.id))
        content = deepcopy(result.content)
        content["timeline"][0]["speaker"] = "  \t "
        result.content = content
    snapshot = repo.begin_voiceprint_preparation(parent, now=NOW)
    assert snapshot["speaker_list"] == [] and snapshot["sources"] == {}
    assert repo.get_voiceprint_speaker_list(parent, now=NOW)["speaker_list_size"] == 0
    assert repo.finish_voiceprint_preparation(parent, now=NOW)["ready"] is True


@pytest.mark.parametrize(("names", "registered", "ready"), [
    (["A", "B"], 1, False),
    (["A", "B", "C", "D"], 2, False),
    (["A", "B", "C", "D"], 3, True),
])
def test_completion_requires_strict_majority(repository, names, registered, ready):
    factory, queue, repo, task_id, parent, _prepared = _prepare(repository, chunk_count=1)
    _enable(factory, task_id)
    _commit(queue, repo, names, 0)
    snapshot = repo.begin_voiceprint_preparation(parent, now=NOW)
    for name in names[:registered]:
        _register(repo, parent, snapshot, name)
    assert repo.finish_voiceprint_preparation(parent, now=NOW)["ready"] is ready


def test_failed_finish_transaction_keeps_registered_references_unpublished(repository):
    factory, queue, repo, task_id, parent, _prepared = _prepare(repository, chunk_count=1)
    _enable(factory, task_id)
    _commit(queue, repo, ["A"], 0)
    snapshot = repo.begin_voiceprint_preparation(parent, now=NOW)
    _register(repo, parent, snapshot, "A")

    def fail_commit(session):
        if any(isinstance(item, VideoParseTask) and item.detail["voiceprint"]["ready"] for item in session.dirty):
            raise RuntimeError("database commit failed")

    event.listen(factory, "before_commit", fail_commit)
    try:
        with pytest.raises(RuntimeError, match="commit failed"):
            repo.finish_voiceprint_preparation(parent, now=NOW)
    finally:
        event.remove(factory, "before_commit", fail_commit)
    with factory() as session:
        voiceprint = session.get(VideoParseTask, task_id).detail["voiceprint"]
        assert voiceprint["ready"] is False
        assert len(voiceprint["voiceprintList"][0]["references"]) == 1
    assert queue.claim_interval_refine("refine", lease_duration=timedelta(minutes=1), now=NOW) is None


def test_registration_rejects_foreign_source_stale_lease_and_outside_segment(repository):
    factory, queue, repo, task_id, parent, _prepared = _prepare(repository, chunk_count=1)
    _enable(factory, task_id)
    _commit(queue, repo, ["A"], 0)
    snapshot = repo.begin_voiceprint_preparation(parent, now=NOW)
    with pytest.raises(VideoParseConflictError, match="outside"):
        _register(repo, parent, snapshot, "A", source_ref="foreign-video:" + "0" * 64)
    with pytest.raises(VideoParseSnapshotConflictError, match="segment"):
        _register(repo, parent, snapshot, "A", start_ms=0)
    with pytest.raises(VideoParseLeaseLostError):
        _register(repo, replace(parent, lease_version=parent.lease_version + 1), snapshot, "A")
    with pytest.raises(VideoParseLeaseLostError):
        repo.finish_voiceprint_preparation(replace(parent, worker_id="other"), now=NOW)


def test_finish_revalidates_identity_but_not_unrelated_source_version(repository):
    factory, queue, repo, task_id, parent, _prepared = _prepare(repository, chunk_count=1)
    _enable(factory, task_id)
    _commit(queue, repo, ["A"], 0)
    snapshot = repo.begin_voiceprint_preparation(parent, now=NOW)
    _register(repo, parent, snapshot, "A")
    chunk_id = next(iter(snapshot["member_contents"]))
    with factory.begin() as session:
        row = _content_row(session, chunk_id)
        content = deepcopy(row.content)
        content["timeline"][0]["description"] = "unrelated visual description correction"
        row.content = content
        row.version += 1
    assert repo.finish_voiceprint_preparation(parent, now=NOW)["ready"] is True
    refine = queue.claim_interval_refine("refine", lease_duration=timedelta(minutes=1), now=NOW)
    assert len(repo.load_voiceprint_references(refine, now=NOW)) == 1
    with factory.begin() as session:
        row = _content_row(session, chunk_id)
        content = deepcopy(row.content)
        content["timeline"][0]["speaker"] = None
        row.content = content
        row.version += 1
    assert repo.load_voiceprint_references(refine, now=NOW) == []
    # A completed preparation remains completed; later invalidation does not launch another round.
    assert repo.begin_voiceprint_preparation(parent, now=NOW) is None
    assert repo.finish_voiceprint_preparation(parent, now=NOW)["ready"] is True


def test_changed_identity_prevents_partial_registration_from_becoming_ready(repository):
    factory, queue, repo, task_id, parent, _prepared = _prepare(repository, chunk_count=1)
    _enable(factory, task_id)
    _commit(queue, repo, ["A"], 0)
    snapshot = repo.begin_voiceprint_preparation(parent, now=NOW)
    _register(repo, parent, snapshot, "A")
    with factory.begin() as session:
        row = _content_row(session, next(iter(snapshot["member_contents"])))
        content = deepcopy(row.content)
        content["timeline"][0]["text_variants"][0]["text"] = "different utterance"
        row.content = content
        row.version += 1
    with pytest.raises(VideoParseSnapshotConflictError, match="identity changed"):
        repo.finish_voiceprint_preparation(parent, now=NOW)
    with factory() as session:
        assert session.get(VideoParseTask, task_id).detail["voiceprint"]["ready"] is False


def test_refine_commit_fences_reference_outside_interval(repository):
    factory, queue, repo, task_id, parent, _prepared = _prepare(repository, chunk_count=6)
    _enable(factory, task_id)
    for sequence in range(3):
        _commit(queue, repo, ["A"] if sequence == 0 else [], sequence)
    snapshot = repo.begin_voiceprint_preparation(parent, now=NOW)
    _register(repo, parent, snapshot, "A")
    repo.finish_voiceprint_preparation(parent, now=NOW)
    for sequence in range(3, 6):
        _commit(queue, repo, [], sequence)
    with factory.begin() as session:
        for interval in session.scalars(select(IntervalParseTask).where(IntervalParseTask.video_task_id == task_id)):
            if interval.start_sequence_no != 3:
                interval.next_execute_time = NOW + timedelta(hours=1)
    refine = queue.claim_interval_refine("refine", lease_duration=timedelta(minutes=1), now=NOW)
    assert refine.start_sequence_no == 3
    references = repo.load_voiceprint_references(refine, now=NOW)
    assert len(references) == 1
    with factory.begin() as session:
        row = _content_row(session, references[0]["chunk_id"])
        content = deepcopy(row.content)
        content["timeline"][0]["end_seconds"] += 1
        row.content = content
        row.version += 1
    with pytest.raises(VideoParseSnapshotConflictError, match="voiceprint"):
        repo.commit_interval_refinement(refine, interval_content={}, member_updates={},
                                        voiceprint_references=references,
                                        **_refinement_inputs(repo, refine, now=NOW), now=NOW)


def test_voiceprint_retry_budget_survives_new_coordinator_and_discards_partial_results(repository):
    factory, queue, repo, task_id, parent, _prepared = _prepare(repository, chunk_count=1)
    _enable(factory, task_id)
    _commit(queue, repo, ["A"], 0)
    snapshot = repo.begin_voiceprint_preparation(parent, now=NOW)
    _register(repo, parent, snapshot, "A")
    failed_at = NOW
    with factory() as session:
        retries = session.get(VideoParseTask, task_id).parse_config["execution"]["max_retry_count"]
    for attempt in range(retries + 1):
        result = queue.retry_video(parent, error="encoder failed", now=failed_at)
        assert result is not None
        with factory() as session:
            task = session.get(VideoParseTask, task_id)
            assert task.detail["_worker_runtime_v2"]["voiceprint_preparation"]["failures"] == attempt + 1
        if attempt < retries:
            assert result.next_execute_time is not None
            failed_at = result.next_execute_time
            parent = queue.claim_video("resumed", lease_duration=timedelta(minutes=10), now=failed_at)
            assert parent is not None
            snapshot = repo.begin_voiceprint_preparation(parent, now=failed_at)
            assert snapshot["voiceprint"]["voiceprintList"] == []
        else:
            assert result.next_execute_time is None
    with factory() as session:
        assert session.get(VideoParseTask, task_id).status == "PARSING_FAILED"
    assert queue.claim_video("again", lease_duration=timedelta(minutes=1), now=failed_at + timedelta(hours=1)) is None


def test_voiceprint_crash_is_charged_once_but_waiting_and_cancellation_are_free(repository):
    factory, queue, repo, task_id, parent, _prepared = _prepare(repository, chunk_count=1)
    _enable(factory, task_id)
    _commit(queue, repo, ["A"], 0)
    expired_at = NOW + timedelta(minutes=11)
    assert queue.reap_expired_videos(now=expired_at) == 1
    parent = queue.claim_video("resumed", lease_duration=timedelta(minutes=1), now=expired_at)
    repo.begin_voiceprint_preparation(parent, now=expired_at)
    assert queue.reap_expired_videos(now=expired_at + timedelta(minutes=2)) == 1
    assert queue.reap_expired_videos(now=expired_at + timedelta(minutes=2)) == 0
    with factory() as session:
        task = session.get(VideoParseTask, task_id)
        assert task.detail["_worker_runtime_v2"]["voiceprint_preparation"]["failures"] == 1
        resumed_at = task.next_execute_time
    parent = queue.claim_video("cancelled", lease_duration=timedelta(minutes=1), now=resumed_at)
    repo.begin_voiceprint_preparation(parent, now=resumed_at)
    repo.stop_voiceprint_preparation(parent, now=resumed_at)
    assert queue.release_execution(parent, now=resumed_at)
    with factory() as session:
        assert session.get(VideoParseTask, task_id).detail["_worker_runtime_v2"]["voiceprint_preparation"]["failures"] == 1


def test_pending_timeout_release_charges_failure_but_completed_phase_does_not(repository):
    factory, queue, repo, task_id, parent, _prepared = _prepare(repository, chunk_count=1)
    _enable(factory, task_id)
    _commit(queue, repo, [], 0)
    repo.begin_voiceprint_preparation(parent, now=NOW)
    assert queue.release_execution(parent, now=NOW)
    with factory() as session:
        task = session.get(VideoParseTask, task_id)
        resumed_at = task.next_execute_time
        assert task.detail["_worker_runtime_v2"]["voiceprint_preparation"]["failures"] == 1
    parent = queue.claim_video("complete", lease_duration=timedelta(minutes=1), now=resumed_at)
    repo.begin_voiceprint_preparation(parent, now=resumed_at)
    repo.finish_voiceprint_preparation(parent, now=resumed_at)
    result = queue.retry_video(parent, error="text response failed after durable completion", now=resumed_at)
    assert result is not None
    with factory() as session:
        task = session.get(VideoParseTask, task_id)
        assert task.detail["voiceprint"]["ready"] is True
        assert task.detail["_worker_runtime_v2"]["voiceprint_preparation"]["failures"] == 1


def test_reference_data_cannot_cross_video_or_task_boundary(repository):
    factory, queue, repo, task_id, parent, _prepared = _prepare(repository, chunk_count=1)
    _enable(factory, task_id)
    _commit(queue, repo, ["A"], 0)
    snapshot = repo.begin_voiceprint_preparation(parent, now=NOW)
    _register(repo, parent, snapshot, "A")
    repo.finish_voiceprint_preparation(parent, now=NOW)
    with factory() as session:
        foreign_voiceprint = deepcopy(session.get(VideoParseTask, task_id).detail["voiceprint"])
    factory, queue, repo, second_id, second_parent, _prepared = _prepare(repository, chunk_count=1)
    _enable(factory, second_id)
    _commit(queue, repo, [], 0)
    second = repo.begin_voiceprint_preparation(second_parent, now=NOW)
    assert second["voiceprint"] == {"ready": False, "voiceprintList": []}
    repo.finish_voiceprint_preparation(second_parent, now=NOW)
    with factory.begin() as session:
        task = session.get(VideoParseTask, second_id)
        task.detail = {**task.detail, "voiceprint": foreign_voiceprint}
    refine = queue.claim_interval_refine("second", video_parse_task_id=second_id,
                                          lease_duration=timedelta(minutes=1), now=NOW)
    assert repo.load_voiceprint_references(refine, now=NOW) == []


def test_manual_retry_resets_voice_budget_after_topology_is_already_prepared(repository):
    factory, queue, repo, task_id, parent, _prepared = _prepare(repository, chunk_count=1)
    _enable(factory, task_id)
    _commit(queue, repo, ["A"], 0)
    repo.begin_voiceprint_preparation(parent, now=NOW)
    upload_id = str(uuid4())
    with factory.begin() as session:
        task = session.get(VideoParseTask, task_id)
        video = session.get(Video, task.video_id)
        config = deepcopy(task.parse_config)
        config["execution"]["max_retry_count"] = 0
        task.parse_config = config
        session.add(UploadSession(
            id=upload_id, tenant_id="mine", series_id=video.series_id,
            episode_order=1, upload_sequence=1, filename=video.filename,
            media_type="video/mp4", size_bytes=100, segment_size_bytes=100,
            segment_count=1, status="COMPLETED", idempotency_key=upload_id,
            expire_time=NOW + timedelta(days=1), video_id=video.id,
        ))
    queue.retry_video(parent, error="encoder failed", now=NOW)
    with factory() as session:
        task = session.get(VideoParseTask, task_id)
        assert task.status == "PARSING_FAILED"
        lease_version, version = task.lease_version, task.version
    retried = UploadRepository(factory).retry_parse(
        upload_id, tenant_id="mine", expected_job_version=version,
        expected_lease_version=lease_version, now=NOW,
    )
    phase = retried.detail["_worker_runtime_v2"]["voiceprint_preparation"]
    assert retried.status == "PARSING" and phase["failures"] == 0 and phase["active"] is False


def test_snapshot_transaction_failure_before_active_marker_has_finite_budget(repository):
    factory, queue, repo, task_id, parent, _prepared = _prepare(repository, chunk_count=1)
    _enable(factory, task_id)
    with factory.begin() as session:
        task = session.get(VideoParseTask, task_id)
        config = deepcopy(task.parse_config)
        config["execution"]["max_retry_count"] = 0
        task.parse_config = config
    result = queue.retry_video(parent, error="database error before snapshot commit", now=NOW)
    assert result is not None and result.next_execute_time is None
    with factory() as session:
        task = session.get(VideoParseTask, task_id)
        assert task.status == "PARSING_FAILED"
        assert task.detail["_worker_runtime_v2"]["voiceprint_preparation"] == {"failures": 1, "active": False}
