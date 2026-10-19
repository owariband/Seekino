from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from math import sqrt
import hashlib
import json
from uuid import uuid4

import pytest
from sqlalchemy import event, select
from engine.parsev2.voiceprint import Voiceprint

from server.constants.parse import VOICEPRINT_B0_METHOD_VERSION, VOICEPRINT_REFS_PER_SPEAKER
from server.model.job import ChunkParseContent, ChunkParseTask, IntervalParseTask, VideoParseTask
from server.model.upload import UploadSession
from server.model.video import Video
from server.repository.upload import UploadRepository
from server.repository.video_parse import (
    VideoParseConflictError,
    VideoParseLeaseLostError,
    VideoParseSnapshotConflictError,
    VoiceprintRegistrationRejectedError,
    _voiceprint_consistency,
)
from tests.server.test_video_parse_repository import NOW, _prepare, _refinement_inputs
from agent.video_parse_v2.timelineEvent_canvas import ParseContextCanvas
from server.service.video_parse import VideoParseRuntimeService


def _enable(factory, task_id):
    with factory.begin() as session:
        task = session.get(VideoParseTask, task_id)
        task.parse_config = {**task.parse_config, "voiceprint": {
            "execution_timeout_seconds": 120.0, "max_tool_calls": 32,
        }}


def _enable_scoring(factory, task_id, *, mode="flag"):
    _enable(factory, task_id)
    with factory.begin() as session:
        task = session.get(VideoParseTask, task_id)
        task.parse_config = {**task.parse_config, "voiceprint_mode": mode}


def _scoring_preparation(snapshot):
    model = {"name": "campplus", "encoder_revision": "sha256:frozen-task"}
    samples = [{**{key: value for key, value in source.items() if key != "text_variants"},
                "id": source_id, "label": source["speaker"],
                "model": model["name"], "encoder_revision": model["encoder_revision"],
                "vector": [1.0] + [0.0] * 191, "pcm_sha256": source["event_fingerprint"]}
               for source_id, source in snapshot["sources"].items()]
    return {"method_version": "B0-trim20-v1", "parameters": {"trim_fraction": 0.2},
            "model": model, "samples": samples,
            "lines": [{**{key: value for key, value in source.items() if key != "text_variants"},
                       "candidates": [], "gap": None,
                       "duration_ms": source["end_ms"] - source["start_ms"],
                       "band": "NO_VOICE", "reason": "no_candidates"}
                      for source in snapshot["sources"].values()],
            "failed_chunks": deepcopy(snapshot["failed_chunks"]), "timing": snapshot["timing"]}


def _artifact_descriptor(lease, preparation):
    encoded = json.dumps(preparation, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode()
    checksum = hashlib.sha256(encoded).hexdigest()
    return {"location": f"artifact://test/voiceprints/{lease.video_parse_task_id}-{checksum}.json",
            "checksum": checksum}


def _label_scoring_preparation(snapshot):
    from engine.parsev2.voiceprint_matching import score_voiceprint_batch

    preparation = _scoring_preparation(snapshot)
    parameters = VideoParseRuntimeService._voiceprint_parameters()
    batch = score_voiceprint_batch(
        preparation["samples"], model=preparation["model"]["name"],
        encoder_revision=preparation["model"]["encoder_revision"],
        **{key: parameters[key] for key in (
            "dimensions", "min_duration_ms", "trim_fraction", "label_min_rank", "label_min_margin",
            "label_min_duration_ms", "label_min_ref_count", "label_min_consistency",
        )},
    )
    preparation.update(method_version=VOICEPRINT_B0_METHOD_VERSION, parameters=parameters,
                       hygiene=batch["hygiene"], lines=[
        VideoParseRuntimeService._score_voiceprint_sample(
            {**sample, "understanding_speaker": sample["label"]}, preparation["samples"],
            parameters=parameters, score=score,
        ) for sample, score in zip(preparation["samples"], batch["scores"])
    ])
    return preparation


def _finish_scoring(repo, lease, *, preparation, **options):
    return repo.finish_voiceprint_scoring(
        lease, preparation=preparation, artifact=_artifact_descriptor(lease, preparation), **options,
    )


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
    source_ref, source = next((key, value) for key, value in snapshot["sources"].items()
                              if value["speaker"] == speaker
                              and (overrides.get("source_ref") not in snapshot["sources"]
                                   or key == overrides["source_ref"]))
    args = {"source_ref": source_ref, "speaker": speaker,
            "start_ms": source["start_ms"] + 100, "end_ms": source["end_ms"] - 100,
            "selection_basis": "Independent attribution and clean single-speaker audio",
            "confidence": 0.9, "model": "campplus", "encoder_revision": "v1.0.0",
            "vector": [1.0] + [0.0] * 191,
            "pcm_sha256": source["event_fingerprint"], "duration_ms": 800}
    args.update(overrides)
    return repo.register_voiceprint(lease, **args, now=NOW)


def _content_row(session, chunk_id):
    return session.scalar(select(ChunkParseContent).join(
        ChunkParseTask, ChunkParseTask.id == ChunkParseContent.chunk_task_id,
    ).where(ChunkParseTask.chunk_id == chunk_id))


def test_preparation_keeps_all_committed_sources_and_freezes_snapshot(repository):
    factory, queue, repo, task_id, parent, _prepared = _prepare(repository, chunk_count=8)
    _enable(factory, task_id)
    for sequence, name in enumerate("ABCDE"):
        _commit(queue, repo, [name, None], sequence)
    with pytest.raises(VideoParseConflictError, match="three quarters"):
        repo.begin_voiceprint_preparation(parent, now=NOW)
    _commit(queue, repo, ["F"], 5)
    _commit(queue, repo, ["G"], 6)
    snapshot = repo.begin_voiceprint_preparation(parent, now=NOW)
    assert snapshot["speaker_list"] == list("ABCDEFG")
    assert set(snapshot["member_sequence_nos"].values()) == set(range(7))
    assert snapshot["source_media_ref"].startswith("artifact://")
    assert repo.begin_voiceprint_preparation(parent, now=NOW) == snapshot
    _commit(queue, repo, ["later NPC"], 7)
    again = repo.begin_voiceprint_preparation(parent, now=NOW)
    assert again["speaker_list"] == snapshot["speaker_list"]
    assert again["member_sequence_nos"] == snapshot["member_sequence_nos"]
    assert again["sources"] == snapshot["sources"]


def test_collection_target_allows_seventh_reference_and_completion(repository):
    factory, queue, repo, task_id, parent, _prepared = _prepare(repository, chunk_count=2)
    _enable(factory, task_id)
    _commit(queue, repo, ["A"] * 6, 0)
    _commit(queue, repo, ["A"], 1)
    snapshot = repo.begin_voiceprint_preparation(parent, now=NOW)
    sources = list(snapshot["sources"].items())
    for count, (source_ref, source) in enumerate(sources[:6], 1):
        receipt = _register(repo, parent, snapshot, "A", source_ref=source_ref)
        assert receipt["added"] is True and receipt["refs_count"] == count
        assert receipt["target_refs_count"] == VOICEPRINT_REFS_PER_SPEAKER == 6
        assert receipt["reference"]["source_ref"] == source_ref
    source_ref, source = sources[0]
    duplicate = repo.precheck_voiceprint_registration(
        parent, source_ref=source_ref, speaker="A", start_ms=source["start_ms"] + 100,
        end_ms=source["end_ms"] - 100, model="campplus", encoder_revision="v1.0.0", now=NOW,
    )
    assert duplicate["added"] is False and duplicate["refs_count"] == 6
    assert _register(repo, parent, snapshot, "A") == duplicate
    source_ref, source = sources[6]
    assert repo.precheck_voiceprint_registration(
        parent, source_ref=source_ref, speaker="A", start_ms=source["start_ms"] + 100,
        end_ms=source["end_ms"] - 100, model="campplus", encoder_revision="v1.0.0", now=NOW,
    ) is None
    seventh = _register(repo, parent, snapshot, "A", source_ref=source_ref)
    assert seventh["added"] is True and seventh["refs_count"] == 7
    assert seventh["target_refs_count"] == VOICEPRINT_REFS_PER_SPEAKER
    duplicate = _register(repo, parent, snapshot, "A", source_ref=source_ref)
    assert duplicate["added"] is False and duplicate["refs_count"] == 7
    progress = repo.get_voiceprint_speaker_list(parent, now=NOW)["speaker_list"][0]
    assert progress["refs_count"] == 7 and progress["target_refs_count"] == VOICEPRINT_REFS_PER_SPEAKER
    assert len(progress["references"]) == 7
    assert all(set(ref) == {"source_ref", "start_ms", "end_ms"} for ref in progress["references"])
    assert repo.finish_voiceprint_preparation(parent, now=NOW)["ready"] is True


@pytest.mark.parametrize(("start_ms", "end_ms"), [(201, 1000), (100, 200)])
def test_same_source_different_subrange_does_not_add_another_case(repository, start_ms, end_ms):
    factory, queue, repo, task_id, parent, _prepared = _prepare(repository, chunk_count=1)
    _enable(factory, task_id)
    _commit(queue, repo, ["A"], 0)
    snapshot = repo.begin_voiceprint_preparation(parent, now=NOW)
    first = _register(repo, parent, snapshot, "A")
    ref = first["reference"]
    kwargs = {"source_ref": ref["source_ref"], "speaker": "A", "start_ms": start_ms,
              "end_ms": end_ms, "model": "campplus", "encoder_revision": "v1.0.0"}
    with pytest.raises(VoiceprintRegistrationRejectedError) as rejected:
        repo.precheck_voiceprint_registration(parent, **kwargs, now=NOW)
    assert rejected.value.code == "VOICEPRINT_SOURCE_ALREADY_REGISTERED"
    assert "different source_ref" in rejected.value.retry_hint
    with pytest.raises(VoiceprintRegistrationRejectedError, match="already has a reference"):
        _register(repo, parent, snapshot, "A", start_ms=start_ms, end_ms=end_ms, duration_ms=end_ms - start_ms)
    assert repo.get_voiceprint_speaker_list(parent, now=NOW)["speaker_list"][0]["refs_count"] == 1


@pytest.mark.parametrize("speaker", ["A", "B"])
def test_identical_pcm_from_another_source_is_not_an_independent_case(repository, speaker):
    factory, queue, repo, task_id, parent, _prepared = _prepare(repository, chunk_count=1)
    _enable(factory, task_id)
    _commit(queue, repo, ["A", speaker], 0)
    snapshot = repo.begin_voiceprint_preparation(parent, now=NOW)
    first = _register(repo, parent, snapshot, "A")
    source_ref, source = list(snapshot["sources"].items())[1]
    assert repo.precheck_voiceprint_registration(
        parent, source_ref=source_ref, speaker=speaker, start_ms=source["start_ms"] + 100,
        end_ms=source["end_ms"] - 100, model="campplus", encoder_revision="v1.0.0", now=NOW,
    ) is None
    with pytest.raises(VoiceprintRegistrationRejectedError) as rejected:
        _register(repo, parent, snapshot, speaker, source_ref=source_ref,
                  pcm_sha256=first["reference"]["pcm_sha256"])
    assert rejected.value.code == "VOICEPRINT_DUPLICATE_AUDIO"
    assert _register(repo, parent, snapshot, "A") == {**first, "added": False}
    counts = {item["speaker"]: item["refs_count"]
              for item in repo.get_voiceprint_speaker_list(parent, now=NOW)["speaker_list"]}
    assert counts == ({"A": 1} if speaker == "A" else {"A": 1, "B": 0})


@pytest.mark.parametrize("speaker", ["A", "B"])
@pytest.mark.parametrize(("start_ms", "end_ms", "overlaps"), [
    (150, 350, True), (350, 600, True), (150, 1150, True), (1000, 1500, False),
])
def test_registration_rechecks_all_speaker_ranges_after_precheck(
    repository, speaker, start_ms, end_ms, overlaps,
):
    factory, queue, repo, task_id, parent, _prepared = _prepare(repository, chunk_count=1)
    _enable(factory, task_id)
    child = _commit(queue, repo, ["A", speaker], 0)
    with factory.begin() as session:
        row = _content_row(session, child.video_chunk_id)
        content = deepcopy(row.content)
        content["timeline"][1].update(start_seconds=100, end_seconds=1800)
        row.content = content
        row.version += 1
    snapshot = repo.begin_voiceprint_preparation(parent, now=NOW)
    source_ref = list(snapshot["sources"])[1]
    kwargs = {"source_ref": source_ref, "speaker": speaker, "start_ms": start_ms,
              "end_ms": end_ms, "model": "campplus", "encoder_revision": "v1.0.0"}
    assert repo.precheck_voiceprint_registration(parent, **kwargs, now=NOW) is None
    _register(repo, parent, snapshot, "A")
    if overlaps:
        with pytest.raises(VoiceprintRegistrationRejectedError) as rejected:
            _register(repo, parent, snapshot, speaker, source_ref=source_ref,
                      start_ms=start_ms, end_ms=end_ms, duration_ms=end_ms - start_ms)
        assert rejected.value.code == "VOICEPRINT_REFERENCE_OVERLAP"
        assert "200–1000" in str(rejected.value)
        with pytest.raises(VoiceprintRegistrationRejectedError):
            repo.precheck_voiceprint_registration(parent, **kwargs, now=NOW)
    else:
        assert repo.precheck_voiceprint_registration(parent, **kwargs, now=NOW) is None
        receipt = _register(repo, parent, snapshot, speaker, source_ref=source_ref,
                            start_ms=start_ms, end_ms=end_ms, duration_ms=end_ms - start_ms)
        assert receipt["added"] is True


def test_finish_requires_shortfall_reasons_including_speakers_without_references(repository):
    factory, queue, repo, task_id, parent, _prepared = _prepare(repository, chunk_count=1)
    _enable(factory, task_id)
    _commit(queue, repo, ["A", "B", "NPC"], 0)
    snapshot = repo.begin_voiceprint_preparation(parent, now=NOW)
    for name in ("A", "B"):
        _register(repo, parent, snapshot, name)
    incomplete = repo.finish_voiceprint_preparation(
        parent, skip_reasons={"A": "one clean line", "NPC": "  ", "unknown": "ignored"}, now=NOW,
    )
    assert incomplete["ready"] is False
    assert incomplete["reference_counts"] == {"A": 1, "B": 1, "NPC": 0}
    assert incomplete["missing_reason_speakers"] == ["B", "NPC"]
    assert incomplete["skip_reasons"] == {"A": "one clean line"}
    assert queue.claim_interval_refine("refine", lease_duration=timedelta(minutes=1), now=NOW) is None
    reasons = {"A": "one clean line", "B": "no other independent line", "NPC": "too short"}
    assert repo.finish_voiceprint_preparation(parent, skip_reasons=reasons, now=NOW)["ready"] is True
    repeated = repo.finish_voiceprint_preparation(parent, now=NOW)
    assert repeated["ready"] is True and repeated["missing_reason_speakers"] == []
    with factory() as session:
        voiceprint = session.get(VideoParseTask, task_id).detail["voiceprint"]
        assert set(voiceprint) == {"ready", "voiceprintList"}


def test_refine_is_not_claimed_until_atomic_majority_finish(repository):
    factory, queue, repo, task_id, parent, prepared = _prepare(repository, chunk_count=2)
    _enable(factory, task_id)
    _commit(queue, repo, ["A", "B", "NPC"], 0)
    _commit(queue, repo, [], 1)
    snapshot = repo.begin_voiceprint_preparation(parent, now=NOW)
    first = _register(repo, parent, snapshot, "A")
    assert first["added"] is True
    assert _register(repo, parent, snapshot, "A") == {**first, "added": False}
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
    reasons = {"A": "only one clean source", "B": "only one clean source", "NPC": "too short"}
    result = repo.finish_voiceprint_preparation(parent, skip_reasons=reasons, now=NOW)
    assert result.pop("suspect_references") == []
    assert result.pop("consistency")["insufficient_speakers"] == ["A", "B", "NPC"]
    assert result == {"ready": True, "registered_speaker_count": 2, "speaker_list_size": 3,
                      "missing_speakers": ["NPC"], "skip_reasons": reasons, "minimum_speaker_count": 2,
                      "reference_counts": {"A": 1, "B": 1, "NPC": 0},
                      "target_refs_count": VOICEPRINT_REFS_PER_SPEAKER,
                      "missing_reason_speakers": [], "message": "Voiceprint preparation is ready."}
    assert repo.begin_voiceprint_preparation(parent, now=NOW) is None
    assert repo.finish_voiceprint_preparation(parent, now=NOW)["ready"] is True
    assert repo.get_voiceprint_speaker_list(parent, now=NOW)["registered_speaker_count"] == 2
    refine = queue.claim_interval_refine("refine", lease_duration=timedelta(minutes=1), now=NOW)
    assert refine is not None
    references = repo.load_voiceprint_references(refine, now=NOW)
    assert {ref["speaker"] for ref in references} == {"A", "B"}
    assert len(references) == 2


def test_finish_reports_outlier_without_removal_and_rechecks_after_replacement(repository):
    factory, queue, repo, task_id, parent, _prepared = _prepare(repository, chunk_count=2)
    _enable(factory, task_id)
    _commit(queue, repo, ["A"] * 6, 0)
    _commit(queue, repo, [], 1)
    snapshot = repo.begin_voiceprint_preparation(parent, now=NOW)
    sources = list(snapshot["sources"])
    for index, source_ref in enumerate(sources[:5]):
        _register(repo, parent, snapshot, "A", source_ref=source_ref,
                  vector=([0.0, 1.0] + [0.0] * 190) if index == 4 else [1.0] + [0.0] * 191)
    with factory() as session:
        before = deepcopy(session.get(VideoParseTask, task_id).detail)
    result = repo.finish_voiceprint_preparation(parent, skip_reasons={"A": "do not bypass"}, now=NOW)
    assert result["ready"] is False
    assert result["consistency"]["checked_speakers"] == ["A"]
    assert len(result["suspect_references"]) == 1
    suspect = result["suspect_references"][0]
    assert (suspect["source_ref"], suspect["candidate_mean"]) == (sources[4], 0.0)
    assert "replace or unregister" in result["message"]
    assert queue.claim_interval_refine("refine", lease_duration=timedelta(minutes=1), now=NOW) is None
    with factory() as session:
        assert session.get(VideoParseTask, task_id).detail == before
    source = snapshot["sources"][sources[5]]
    assert repo.precheck_voiceprint_registration(
        parent, source_ref=sources[5], speaker="A", start_ms=source["start_ms"] + 100,
        end_ms=source["end_ms"] - 100, model="campplus", encoder_revision="v1.0.0",
        replace_source_ref=sources[4], now=NOW,
    ) is None
    with factory() as session:
        assert session.get(VideoParseTask, task_id).detail == before
    receipt = _register(repo, parent, snapshot, "A", source_ref=sources[5], replace_source_ref=sources[4])
    assert receipt["refs_count"] == 5 and receipt["replaced_source_ref"] == sources[4]
    duplicate = _register(repo, parent, snapshot, "A", source_ref=sources[5], replace_source_ref=sources[4])
    assert duplicate == {**receipt, "added": False}
    incomplete = repo.finish_voiceprint_preparation(parent, now=NOW)
    assert not incomplete["ready"] and incomplete["missing_reason_speakers"] == ["A"]
    completed = repo.finish_voiceprint_preparation(
        parent, skip_reasons={"A": "only five reliable independent sources"}, now=NOW,
    )
    assert completed["ready"] is True and completed["suspect_references"] == []
    assert queue.claim_interval_refine("refine", lease_duration=timedelta(minutes=1), now=NOW) is not None
    assert "consistency" not in repo.finish_voiceprint_preparation(parent, now=NOW)


@pytest.mark.parametrize("count", [1, 4, 5, 6])
def test_consistency_does_not_force_an_outlier_or_require_five_references(repository, count):
    factory, queue, repo, task_id, parent, _prepared = _prepare(repository, chunk_count=1)
    _enable(factory, task_id)
    _commit(queue, repo, ["A"] * count, 0)
    snapshot = repo.begin_voiceprint_preparation(parent, now=NOW)
    for index, source_ref in enumerate(snapshot["sources"]):
        # Below the diagnostic minimum, even different voices cannot establish which reference is wrong.
        vector = [0.0, 1.0] + [0.0] * 190 if count < 5 and index == 0 else [1.0] + [0.0] * 191
        _register(repo, parent, snapshot, "A", source_ref=source_ref, vector=vector)
    result = repo.finish_voiceprint_preparation(parent, skip_reasons={"A": "only available sources"}, now=NOW)
    assert result["ready"] is True and result["suspect_references"] == []
    assert result["consistency"]["insufficient_speakers"] == (["A"] if count < 5 else [])


def test_unregister_outlier_can_finish_below_target_and_is_idempotent(repository):
    factory, queue, repo, task_id, parent, _prepared = _prepare(repository, chunk_count=1)
    _enable(factory, task_id)
    _commit(queue, repo, ["A"] * 5, 0)
    snapshot = repo.begin_voiceprint_preparation(parent, now=NOW)
    sources = list(snapshot["sources"])
    for index, source_ref in enumerate(sources):
        _register(repo, parent, snapshot, "A", source_ref=source_ref,
                  vector=([0.0, 1.0] + [0.0] * 190) if index == 4 else [1.0] + [0.0] * 191)
    assert not repo.finish_voiceprint_preparation(parent, now=NOW)["ready"]
    removed = repo.unregister_voiceprint(parent, source_ref=sources[4], now=NOW)
    assert removed["removed"] is True and removed["refs_count"] == 4
    assert repo.unregister_voiceprint(parent, source_ref=sources[4], now=NOW)["removed"] is False
    incomplete = repo.finish_voiceprint_preparation(parent, now=NOW)
    assert not incomplete["ready"] and incomplete["missing_reason_speakers"] == ["A"]
    assert repo.finish_voiceprint_preparation(parent, skip_reasons={"A": "no reliable replacement"}, now=NOW)["ready"]
    with pytest.raises(VideoParseLeaseLostError):
        repo.unregister_voiceprint(parent, source_ref=sources[0], now=NOW)
    with pytest.raises(VideoParseLeaseLostError):
        _register(repo, parent, snapshot, "A", source_ref=sources[4], replace_source_ref=sources[0])


@pytest.mark.parametrize("operation", ["replace", "unregister"])
def test_reference_changes_roll_back_on_database_failure(repository, operation):
    factory, queue, repo, task_id, parent, _prepared = _prepare(repository, chunk_count=1)
    _enable(factory, task_id)
    _commit(queue, repo, ["A", "A"], 0)
    snapshot = repo.begin_voiceprint_preparation(parent, now=NOW)
    old, new = list(snapshot["sources"])
    _register(repo, parent, snapshot, "A", source_ref=old)
    with factory() as session:
        before = deepcopy(session.get(VideoParseTask, task_id).detail)

    def fail_commit(session):
        if any(isinstance(item, VideoParseTask) for item in session.dirty):
            raise RuntimeError("database commit failed")

    event.listen(factory, "before_commit", fail_commit)
    try:
        with pytest.raises(RuntimeError, match="commit failed"):
            if operation == "replace":
                _register(repo, parent, snapshot, "A", source_ref=new, replace_source_ref=old)
            else:
                repo.unregister_voiceprint(parent, source_ref=old, now=NOW)
    finally:
        event.remove(factory, "before_commit", fail_commit)
    with factory() as session:
        assert session.get(VideoParseTask, task_id).detail == before


def test_replacement_can_retrim_same_source_but_preserves_other_reference_checks(repository):
    factory, queue, repo, task_id, parent, _prepared = _prepare(repository, chunk_count=1)
    _enable(factory, task_id)
    _commit(queue, repo, ["A"] * 3, 0)
    snapshot = repo.begin_voiceprint_preparation(parent, now=NOW)
    old, other, new = list(snapshot["sources"])
    _register(repo, parent, snapshot, "A", source_ref=old)
    other_receipt = _register(repo, parent, snapshot, "A", source_ref=other)
    receipt = _register(repo, parent, snapshot, "A", source_ref=old, start_ms=300,
                        duration_ms=700, replace_source_ref=old)
    assert receipt["refs_count"] == 2 and receipt["reference"]["start_ms"] == 300
    assert _register(repo, parent, snapshot, "A", source_ref=old, start_ms=300,
                     duration_ms=700, replace_source_ref=old)["added"] is False
    for target, pcm, code in [(other, None, "VOICEPRINT_SOURCE_ALREADY_REGISTERED"),
                              (new, other_receipt["reference"]["pcm_sha256"], "VOICEPRINT_DUPLICATE_AUDIO")]:
        kwargs = {"pcm_sha256": pcm} if pcm else {}
        with pytest.raises(VoiceprintRegistrationRejectedError) as rejected:
            _register(repo, parent, snapshot, "A", source_ref=target, replace_source_ref=old, **kwargs)
        assert rejected.value.code == code
    refs = repo.get_voiceprint_speaker_list(parent, now=NOW)["speaker_list"][0]["references"]
    assert {item["source_ref"] for item in refs} == {old, other}


@pytest.mark.parametrize("operation", ["replace", "unregister"])
def test_reference_edits_reject_foreign_sources_and_old_leases(repository, operation):
    factory, queue, repo, task_id, parent, _prepared = _prepare(repository, chunk_count=1)
    _enable(factory, task_id)
    _commit(queue, repo, ["A", "A"], 0)
    snapshot = repo.begin_voiceprint_preparation(parent, now=NOW)
    old, new = list(snapshot["sources"])
    _register(repo, parent, snapshot, "A", source_ref=old)
    for lease, source, error in [(parent, "other-video:invalid", VoiceprintRegistrationRejectedError),
                                  (replace(parent, lease_version=parent.lease_version + 1), old,
                                   VideoParseLeaseLostError)]:
        with pytest.raises(error):
            if operation == "replace":
                _register(repo, lease, snapshot, "A", source_ref=new, replace_source_ref=source)
            else:
                repo.unregister_voiceprint(lease, source_ref=source, now=NOW)


@pytest.mark.parametrize(("core", "candidate", "flagged"), [
    (0.65, 0.45, True), (0.58, 0.35, True), (0.80, 0.52, False), (0.65, 0.48, True),
    (0.40, 0.30, True),
])
def test_consistency_uses_candidate_mean_without_core_or_gap_conditions(core, candidate, flagged):
    refs = []
    for index in range(5):
        vector = [0.0] * 192
        if index < 4:
            vector[0], vector[index + 1] = sqrt(core), sqrt(1 - core)
        else:
            vector[0] = candidate / sqrt(core)
            vector[5] = sqrt(1 - vector[0] ** 2)
        refs.append({"speaker": "A", "model": {"name": "campplus", "encoder_revision": "v1.0.0"},
                     "vector": [value * (index + 1) for value in vector], "chunk_id": "chunk",
                     "event_fingerprint": str(index), "start_ms": index * 1000,
                     "end_ms": index * 1000 + 800, "duration_ms": 800})
    result = _voiceprint_consistency(refs, {"A"})
    target = next((item for item in result["suspect_references"] if item["source_ref"] == "chunk:4"), None)
    assert (target is not None) is flagged
    if flagged:
        assert target["candidate_mean"] == pytest.approx(candidate)
        assert "core_mean" not in target and "gap" not in target
    assert result["consistency"]["thresholds"] == {"minimum_references": 5, "max_candidate_mean": 0.50}
    refs[-1]["model"] = {"name": "campplus", "encoder_revision": "other-revision"}
    assert _voiceprint_consistency(refs, {"A"})["consistency"]["insufficient_speakers"] == ["A"]


def test_replacement_excludes_only_old_audio_from_overlap_check(repository):
    factory, queue, repo, task_id, parent, _prepared = _prepare(repository, chunk_count=1)
    _enable(factory, task_id)
    child = _commit(queue, repo, ["A"] * 3, 0)
    with factory.begin() as session:
        row = _content_row(session, child.video_chunk_id)
        content = deepcopy(row.content)
        content["timeline"][2].update(start_seconds=100, end_seconds=3000)
        row.content = content
    snapshot = repo.begin_voiceprint_preparation(parent, now=NOW)
    old, other, new = list(snapshot["sources"])
    _register(repo, parent, snapshot, "A", source_ref=old)
    _register(repo, parent, snapshot, "A", source_ref=other)
    with pytest.raises(VoiceprintRegistrationRejectedError) as rejected:
        _register(repo, parent, snapshot, "A", source_ref=new, replace_source_ref=old,
                  start_ms=1900, end_ms=2300, duration_ms=400)
    assert rejected.value.code == "VOICEPRINT_REFERENCE_OVERLAP"
    receipt = _register(repo, parent, snapshot, "A", source_ref=new, replace_source_ref=old,
                        start_ms=300, end_ms=900, duration_ms=600)
    assert receipt["refs_count"] == 2 and receipt["replaced_source_ref"] == old


@pytest.mark.parametrize("change", [{"speaker": "B"}, {"encoder_revision": "other-revision"}])
def test_replacement_cannot_transfer_old_reference_to_another_identity_or_encoder(repository, change):
    factory, queue, repo, task_id, parent, _prepared = _prepare(repository, chunk_count=1)
    _enable(factory, task_id)
    _commit(queue, repo, ["A", "A", "B"], 0)
    snapshot = repo.begin_voiceprint_preparation(parent, now=NOW)
    old, new, other = list(snapshot["sources"])
    _register(repo, parent, snapshot, "A", source_ref=old)
    with pytest.raises(VoiceprintRegistrationRejectedError) as rejected:
        _register(repo, parent, snapshot, change.get("speaker", "A"),
                  source_ref=other if "speaker" in change else new, replace_source_ref=old,
                  **{key: value for key, value in change.items() if key != "speaker"})
    assert rejected.value.code == "VOICEPRINT_REPLACEMENT_MISMATCH"
    assert repo.get_voiceprint_speaker_list(parent, now=NOW)["registered_speaker_count"] == 1


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
    assert repo.finish_voiceprint_preparation(
        parent, skip_reasons={name: "no other reliable source" for name in names}, now=NOW,
    )["ready"] is ready


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
            repo.finish_voiceprint_preparation(parent, skip_reasons={"A": "only one clean source"}, now=NOW)
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
    source_ref, source = next(iter(snapshot["sources"].items()))
    with pytest.raises(VideoParseLeaseLostError):
        repo.precheck_voiceprint_registration(
            replace(parent, lease_version=parent.lease_version + 1), source_ref=source_ref, speaker="A",
            start_ms=source["start_ms"], end_ms=source["end_ms"], model="campplus", encoder_revision="v1.0.0", now=NOW,
        )
    with pytest.raises(VideoParseLeaseLostError):
        repo.finish_voiceprint_preparation(replace(parent, worker_id="other"), now=NOW)


def test_prepared_references_remain_static_after_timeline_edits(repository):
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
    assert repo.finish_voiceprint_preparation(
        parent, skip_reasons={"A": "only one clean source"}, now=NOW,
    )["ready"] is True
    refine = queue.claim_interval_refine("refine", lease_duration=timedelta(minutes=1), now=NOW)
    references = repo.load_voiceprint_references(refine, now=NOW)
    assert len(references) == 1
    with factory.begin() as session:
        row = _content_row(session, chunk_id)
        content = deepcopy(row.content)
        content["timeline"][0]["speaker"] = None
        row.content = content
        row.version += 1
    assert repo.load_voiceprint_references(refine, now=NOW) == references
    assert references[0]["speaker"] == "A"
    assert repo.get_voiceprint_speaker_list(parent, now=NOW)["speaker_list"][0]["refs_count"] == 1
    # Prepared labels are trusted; timeline corrections never reopen preparation.
    assert repo.begin_voiceprint_preparation(parent, now=NOW) is None
    repeated = repo.finish_voiceprint_preparation(parent, now=NOW)
    assert repeated["ready"] is True and repeated["reference_counts"] == {"A": 1}


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
    source_ref, source = next(iter(snapshot["sources"].items()))
    with pytest.raises(VideoParseSnapshotConflictError, match="identity"):
        repo.precheck_voiceprint_registration(
            parent, source_ref=source_ref, speaker="A", start_ms=source["start_ms"],
            end_ms=source["end_ms"], model="campplus", encoder_revision="v1.0.0", now=NOW,
        )
    with factory() as session:
        assert session.get(VideoParseTask, task_id).detail["voiceprint"]["ready"] is False


@pytest.mark.parametrize("change", ["time", "translation", "speaker", "removed_event"])
def test_refine_uses_prepared_references_without_source_revalidation(repository, change):
    factory, queue, repo, task_id, parent, _prepared = _prepare(repository, chunk_count=6)
    _enable(factory, task_id)
    for sequence in range(5):
        _commit(queue, repo, ["A"] if sequence == 0 else [], sequence)
    snapshot = repo.begin_voiceprint_preparation(parent, now=NOW)
    _register(repo, parent, snapshot, "A")
    repo.finish_voiceprint_preparation(parent, skip_reasons={"A": "only one clean source"}, now=NOW)
    _commit(queue, repo, [], 5)
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
        if change == "time":
            content["timeline"][0]["end_seconds"] += 1
        elif change == "translation":
            content["timeline"][0]["text_variants"].append({
                "language": "zh-Hans", "role": "TRANSLATION", "text": "translation unrelated to audio",
            })
        elif change == "speaker":
            content["timeline"][0]["speaker"] = None
        else:
            content["timeline"] = []
        row.content = content
        row.version += 1
    assert repo.load_voiceprint_references(refine, now=NOW) == references
    repo.commit_interval_refinement(refine, interval_content={}, member_updates={},
                                    **_refinement_inputs(repo, refine, now=NOW), now=NOW)
    with factory() as session:
        assert session.get(IntervalParseTask, refine.interval_parse_task_id).status == "COMPLETE"


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
    repo.finish_voiceprint_preparation(parent, skip_reasons={"A": "only one clean source"}, now=NOW)
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


def test_scoring_waits_for_all_terminal_and_keeps_null_targets_with_extra_gate_time(repository):
    factory, queue, repo, task_id, parent, _prepared = _prepare(repository, chunk_count=4)
    _enable_scoring(factory, task_id)
    for sequence, second in enumerate((0, 1, 2)):
        at = NOW + timedelta(seconds=second)
        child = queue.claim_chunk("understand", lease_duration=timedelta(minutes=5), now=at)
        repo.commit_chunk_result(child, content=_dialogues(["A", None], sequence), now=at)
    with pytest.raises(VideoParseConflictError, match="every Understanding chunk"):
        repo.begin_voiceprint_preparation(parent, now=NOW + timedelta(seconds=3))
    at = NOW + timedelta(seconds=5)
    child = queue.claim_chunk("last", lease_duration=timedelta(minutes=5), now=at)
    repo.commit_chunk_result(child, content=_dialogues(["B"], 3), now=at)
    snapshot = repo.begin_voiceprint_preparation(parent, now=at)
    assert len(snapshot["sources"]) == 7
    assert sum(source["speaker"] is None for source in snapshot["sources"].values()) == 3
    assert snapshot["failed_chunks"] == []
    assert snapshot["timing"]["extra_gate_wait_ms"] == 3000
    assert {source["position"] for source in snapshot["sources"].values()} == {0, 1}
    with factory() as session:
        assert "scoring" not in session.get(VideoParseTask, task_id).detail["voiceprint"]


def test_scoring_publishes_all_results_atomically_and_loads_only_with_current_lease(repository):
    factory, queue, repo, task_id, parent, _prepared = _prepare(repository, chunk_count=1)
    _enable_scoring(factory, task_id, mode="prefill")
    _commit(queue, repo, ["A", None], 0)
    snapshot = repo.begin_voiceprint_preparation(parent, now=NOW)
    preparation = _scoring_preparation(snapshot)
    preparation["timing"] = {**preparation["timing"], "preparation_elapsed_ms": 1234}
    assert repo.load_voiceprint_scoring(parent, now=NOW) is None
    with pytest.raises(VideoParseConflictError, match="every fixed dialogue"):
        _finish_scoring(repo, parent, preparation={**preparation, "lines": []}, now=NOW)
    with factory() as session:
        assert session.get(VideoParseTask, task_id).detail["voiceprint"] == {"ready": False, "voiceprintList": []}
    original = deepcopy(preparation)
    result = _finish_scoring(repo, parent, preparation=preparation, now=NOW)
    assert preparation == original
    assert set(result) == {"ready", "method_version", "model", "parameters", "line_count", "sample_count",
                           "checksum", "location"}
    assert result["line_count"] == result["sample_count"] == 2
    with factory() as session:
        task = session.get(VideoParseTask, task_id)
        assert task.detail["voiceprint"] == result
        timing = task.detail["_worker_runtime_v2"]["voiceprint_preparation"]["timing"]
        assert timing["ready_at_ms"] == int(NOW.timestamp() * 1000)
        assert timing["preparation_elapsed_ms"] == 1234
    assert repo.load_voiceprint_scoring(parent, now=NOW) == result
    refine = queue.claim_interval_refine("refine", lease_duration=timedelta(minutes=1), now=NOW)
    assert refine is not None and repo.load_voiceprint_scoring(refine, now=NOW) == result
    with pytest.raises(VideoParseLeaseLostError):
        repo.load_voiceprint_scoring(replace(refine, lease_version=refine.lease_version + 1), now=NOW)


def test_artifact_manifest_round_trips_without_inline_arrays_and_preserves_historical_shapes():
    manifest = {"ready": True, "method_version": "B0-trimmed-centroid-v1",
                "model": {"name": "campplus", "encoder_revision": "sha256:frozen-task"},
                "parameters": {"trim_fraction": 0.2}, "line_count": 339, "sample_count": 335,
                "checksum": "a" * 64, "location": "artifact://test/voiceprints/task-checksum.json"}
    assert Voiceprint.model_validate(manifest).model_dump(mode="json") == manifest
    assert Voiceprint().model_dump(mode="json") == {"ready": False, "voiceprintList": []}
    legacy = {"ready": True, "voiceprintList": [], "scoring": {"lines": [], "samples": []}}
    assert Voiceprint.model_validate(legacy).model_dump(mode="json") == legacy
    for changes in ({"checksum": None}, {"ready": False}, {"sample_count": 340}, {"scoring": {}}):
        with pytest.raises(ValueError):
            Voiceprint.model_validate({**manifest, **changes})


def test_label_diagnostics_and_hygiene_publish_atomically_without_expanding_detail(repository):
    factory, queue, repo, task_id, parent, _prepared = _prepare(repository, chunk_count=1)
    _enable_scoring(factory, task_id)
    _commit(queue, repo, ["A", "A", "A", "A", "B", None], 0)
    preparation = _label_scoring_preparation(repo.begin_voiceprint_preparation(parent, now=NOW))
    original = deepcopy(preparation)
    assert preparation["lines"][0]["label_ref_count"] == 3
    assert preparation["lines"][0]["label_count"] == 2
    assert preparation["lines"][-1]["label_count"] is None
    manifest = _finish_scoring(repo, parent, preparation=preparation, now=NOW)
    assert preparation == original
    assert manifest["method_version"] == VOICEPRINT_B0_METHOD_VERSION
    assert "hygiene" not in manifest and "lines" not in manifest and "samples" not in manifest
    with factory() as session:
        assert session.get(VideoParseTask, task_id).detail["voiceprint"] == manifest


@pytest.mark.parametrize("invalid", [
    "missing_metric", "partial_metrics", "all_null", "rank", "count", "score", "margin", "consistency",
    "label", "flag", "policy", "missing_hygiene", "hygiene_ids", "hygiene_counts", "excluded_reference",
])
def test_label_artifact_rejects_invalid_diagnostics_or_hygiene_before_ready(repository, invalid):
    factory, queue, repo, task_id, parent, _prepared = _prepare(repository, chunk_count=1)
    _enable_scoring(factory, task_id)
    _commit(queue, repo, ["A", "A", "A", "A", "B", None], 0)
    preparation = _label_scoring_preparation(repo.begin_voiceprint_preparation(parent, now=NOW))
    line = preparation["lines"][0]
    if invalid == "missing_metric":
        del line["label_score"]
    elif invalid == "partial_metrics":
        line["label_score"] = None
    elif invalid == "all_null":
        line.update({key: None for key in ("label_score", "label_rank", "label_count", "label_margin",
                                          "label_ref_count", "label_ref_consistency")})
    elif invalid == "rank":
        line["label_rank"] = 3
    elif invalid == "count":
        line["label_count"] = 3
    elif invalid == "score":
        line.update(label_score=0.7, label_margin=0.3)
    elif invalid == "margin":
        line["label_margin"] = 0.2
    elif invalid == "consistency":
        line["label_ref_consistency"] = 1.5
    elif invalid == "label":
        line["understanding_speaker"] = "B"
    elif invalid == "flag":
        line["label_doubtful"] = True
    elif invalid == "policy":
        preparation["parameters"]["label_min_rank"] = 0
    elif invalid == "missing_hygiene":
        del preparation["hygiene"]
    elif invalid == "hygiene_ids":
        preparation["hygiene"]["excluded_reference_ids"] = ["unknown"]
    elif invalid == "hygiene_counts":
        preparation["hygiene"]["pass2_flag_count"] = 1
    else:
        excluded = line["candidates"][0]["reference_ids"][0]
        preparation["hygiene"].update(excluded_reference_ids=[excluded], cleared_flag_ids=[excluded],
                                      pass1_flag_count=1, changed_flag_count=1)
    with pytest.raises(VideoParseConflictError, match="voiceprint"):
        _finish_scoring(repo, parent, preparation=preparation, now=NOW)
    assert repo.load_voiceprint_scoring(parent, now=NOW) is None


@pytest.mark.parametrize("invalid", ["checksum", "mutated_payload", "location"])
def test_scoring_artifact_descriptor_must_match_the_exact_immutable_payload(repository, invalid):
    factory, queue, repo, task_id, parent, _prepared = _prepare(repository, chunk_count=1)
    _enable_scoring(factory, task_id)
    _commit(queue, repo, ["A"], 0)
    snapshot = repo.begin_voiceprint_preparation(parent, now=NOW)
    preparation = _scoring_preparation(snapshot)
    artifact = _artifact_descriptor(parent, preparation)
    if invalid == "checksum":
        artifact["checksum"] = "0" * 64
    elif invalid == "mutated_payload":
        preparation["lines"][0]["reason"] = "changed after artifact publication"
    else:
        artifact["location"] = ""
    with pytest.raises(VideoParseConflictError, match="artifact"):
        repo.finish_voiceprint_scoring(parent, preparation=preparation, artifact=artifact, now=NOW)
    with factory() as session:
        assert session.get(VideoParseTask, task_id).detail["voiceprint"] == {"ready": False, "voiceprintList": []}


@pytest.mark.parametrize("invalid", [
    "sample_text", "line_vector", "line_text", "candidate_src", "unknown_reference",
    "duplicate_reference", "reference_count",
])
def test_scoring_artifact_uses_one_shared_sample_pool_without_repeated_payloads(repository, invalid):
    factory, queue, repo, task_id, parent, _prepared = _prepare(repository, chunk_count=1)
    _enable_scoring(factory, task_id)
    _commit(queue, repo, ["A"], 0)
    snapshot = repo.begin_voiceprint_preparation(parent, now=NOW)
    preparation = _scoring_preparation(snapshot)
    line = preparation["lines"][0]
    sample = preparation["samples"][0]
    if invalid == "sample_text":
        sample["text_variants"] = []
    elif invalid == "line_vector":
        line["vector"] = sample["vector"]
    elif invalid == "line_text":
        line["text_variants"] = []
    else:
        candidate = {"speaker": "A", "similarity": 0.8, "reference_ids": [sample["id"]],
                     "reference_count": 1}
        line["candidates"] = [candidate]
        if invalid == "candidate_src":
            candidate["src"] = [deepcopy(sample)]
        elif invalid == "unknown_reference":
            candidate["reference_ids"] = ["not-in-sample-pool"]
        elif invalid == "duplicate_reference":
            candidate["reference_ids"] *= 2
        else:
            candidate["reference_count"] = 2
    with pytest.raises(VideoParseConflictError):
        _finish_scoring(repo, parent, preparation=preparation, now=NOW)
    assert repo.load_voiceprint_scoring(parent, now=NOW) is None


@pytest.mark.parametrize("stop", ["cancel", "commit_failure"])
def test_scoring_cancellation_or_commit_failure_publishes_nothing(repository, stop):
    factory, queue, repo, task_id, parent, _prepared = _prepare(repository, chunk_count=1)
    _enable_scoring(factory, task_id)
    _commit(queue, repo, ["A"], 0)
    snapshot = repo.begin_voiceprint_preparation(parent, now=NOW)
    calls = []

    def check_active():
        calls.append(True)
        if stop == "cancel" and len(calls) == 3:
            raise RuntimeError("cancelled before ready commit")

    def reject_commit(_session):
        if stop == "commit_failure":
            raise RuntimeError("transaction rejected")

    event.listen(factory, "before_commit", reject_commit)
    try:
        with pytest.raises(RuntimeError):
            _finish_scoring(repo,
                parent, preparation=_scoring_preparation(snapshot), check_active=check_active, now=NOW,
            )
    finally:
        event.remove(factory, "before_commit", reject_commit)
    with factory() as session:
        assert session.get(VideoParseTask, task_id).detail["voiceprint"] == {"ready": False, "voiceprintList": []}


@pytest.mark.parametrize("change", ["source", "sample"])
def test_scoring_rejects_changed_source_snapshot_or_sample_identity(repository, change):
    factory, queue, repo, task_id, parent, _prepared = _prepare(repository, chunk_count=1)
    _enable_scoring(factory, task_id)
    _commit(queue, repo, ["A"], 0)
    snapshot = repo.begin_voiceprint_preparation(parent, now=NOW)
    preparation = _scoring_preparation(snapshot)
    if change == "source":
        with factory.begin() as session:
            content = _content_row(session, next(iter(snapshot["member_contents"])))
            content.version += 1
    else:
        preparation["samples"][0]["label"] = "B"
    with pytest.raises(VideoParseSnapshotConflictError):
        _finish_scoring(repo, parent, preparation=preparation, now=NOW)
    assert repo.load_voiceprint_scoring(parent, now=NOW) is None


def test_scoring_encoder_binding_survives_retry_and_rejects_different_revision(repository):
    factory, queue, repo, task_id, parent, _prepared = _prepare(repository, chunk_count=1)
    _enable_scoring(factory, task_id)
    _commit(queue, repo, ["A"], 0)
    snapshot = repo.begin_voiceprint_preparation(parent, now=NOW)
    preparation = _scoring_preparation(snapshot)
    repo.bind_voiceprint_encoder(parent, model=preparation["model"], now=NOW)
    changed = {**preparation, "model": {**preparation["model"], "encoder_revision": "new-deployment"}}
    with pytest.raises(VideoParseSnapshotConflictError, match="encoder"):
        _finish_scoring(repo, parent, preparation=changed, now=NOW)
    retry = queue.retry_video(parent, error="media read failed", now=NOW)
    assert retry.next_execute_time is not None
    resumed = queue.claim_video("new coordinator", lease_duration=timedelta(minutes=5), now=retry.next_execute_time)
    assert resumed is not None
    snapshot = repo.begin_voiceprint_preparation(resumed, now=retry.next_execute_time)
    with pytest.raises(VideoParseSnapshotConflictError, match="encoder revision"):
        repo.bind_voiceprint_encoder(resumed, model=changed["model"], now=retry.next_execute_time)
    repo.bind_voiceprint_encoder(resumed, model=preparation["model"], now=retry.next_execute_time)
    assert snapshot["voiceprint"] == {"ready": False, "voiceprintList": []}


@pytest.mark.parametrize("vector", [[1.0] * 191, [1.0] * 192])
def test_scoring_rejects_incompatible_or_unnormalized_vectors_without_publishing(repository, vector):
    factory, queue, repo, task_id, parent, _prepared = _prepare(repository, chunk_count=1)
    _enable_scoring(factory, task_id)
    _commit(queue, repo, ["A"], 0)
    snapshot = repo.begin_voiceprint_preparation(parent, now=NOW)
    preparation = _scoring_preparation(snapshot)
    preparation["samples"][0]["vector"] = vector
    with pytest.raises(VideoParseConflictError, match="embedding"):
        _finish_scoring(repo, parent, preparation=preparation, now=NOW)
    assert repo.load_voiceprint_scoring(parent, now=NOW) is None


def test_scoring_keeps_under_400_ms_line_with_reason_without_embedding(repository):
    factory, queue, repo, task_id, parent, _prepared = _prepare(repository, chunk_count=1)
    _enable_scoring(factory, task_id)
    child = queue.claim_chunk("short", lease_duration=timedelta(minutes=1), now=NOW)
    content = _dialogues([None], 0)
    content["timeline"][0]["end_seconds"] = content["timeline"][0]["start_seconds"] + 300
    repo.commit_chunk_result(child, content=content, now=NOW)
    snapshot = repo.begin_voiceprint_preparation(parent, now=NOW)
    preparation = _scoring_preparation(snapshot)
    preparation["samples"] = []
    preparation["lines"][0]["reason"] = "duration_below_encoder_minimum_400_ms"
    result = _finish_scoring(repo, parent, preparation=preparation, now=NOW)
    assert result["sample_count"] == 0 and result["line_count"] == 1
    assert preparation["lines"][0]["duration_ms"] == 300
    assert preparation["lines"][0]["candidates"] == []
    assert preparation["lines"][0]["reason"] == "duration_below_encoder_minimum_400_ms"


def test_scoring_keeps_duplicate_dialogue_events_as_separate_targets(repository):
    factory, queue, repo, task_id, parent, _prepared = _prepare(repository, chunk_count=1)
    _enable_scoring(factory, task_id)
    child = queue.claim_chunk("duplicates", lease_duration=timedelta(minutes=1), now=NOW)
    content = _dialogues(["A"], 0)
    duplicate = deepcopy(content["timeline"][0])
    duplicate["description"] = "another annotation for the same audio"
    content["timeline"].append(duplicate)
    repo.commit_chunk_result(child, content=content, now=NOW)
    snapshot = repo.begin_voiceprint_preparation(parent, now=NOW)
    assert len(snapshot["sources"]) == 2
    assert {source["position"] for source in snapshot["sources"].values()} == {0, 1}
    assert len({source["event_fingerprint"] for source in snapshot["sources"].values()}) == 1
    result = _finish_scoring(repo, parent, preparation=_scoring_preparation(snapshot), now=NOW)
    assert result["line_count"] == 2


@pytest.mark.parametrize("changes", [
    {"candidates": [{"speaker": "A", "similarity": 0.8}, {"speaker": "A", "similarity": 0.7}], "gap": 0.1},
    {"candidates": [{"speaker": "A", "similarity": 0.8}, {"speaker": "B", "similarity": 0.7}], "gap": 0.2},
    {"reason": None},
])
def test_scoring_rejects_incomplete_or_invalid_ranked_lines_without_publishing(repository, changes):
    factory, queue, repo, task_id, parent, _prepared = _prepare(repository, chunk_count=1)
    _enable_scoring(factory, task_id)
    _commit(queue, repo, ["A"], 0)
    snapshot = repo.begin_voiceprint_preparation(parent, now=NOW)
    preparation = _scoring_preparation(snapshot)
    preparation["lines"][0].update(changes)
    with pytest.raises(VideoParseConflictError):
        _finish_scoring(repo, parent, preparation=preparation, now=NOW)
    assert repo.load_voiceprint_scoring(parent, now=NOW) is None


def test_scoring_permanent_failure_is_excluded_and_task_fails_after_ready_without_deadlock(repository):
    factory, queue, repo, task_id, parent, _prepared = _prepare(repository, chunk_count=2)
    _enable_scoring(factory, task_id)
    with factory.begin() as session:
        task = session.get(VideoParseTask, task_id)
        config = deepcopy(task.parse_config)
        config["execution"]["max_retry_count"] = 0
        task.parse_config = config
    failed = queue.claim_chunk("failed", lease_duration=timedelta(minutes=1), now=NOW)
    assert queue.retry_chunk(failed, error="permanent ASR failure", retry_at=NOW, now=NOW)
    assert queue.load_chunk_schedule(task_id, now=NOW).terminal_error is None
    _commit(queue, repo, ["A"], 1)
    snapshot = repo.begin_voiceprint_preparation(parent, now=NOW)
    assert len(snapshot["failed_chunks"]) == 1
    assert snapshot["failed_chunks"][0]["chunk_task_id"] == failed.chunk_parse_task_id
    assert len(snapshot["member_contents"]) == len(snapshot["sources"]) == 1
    assert snapshot["timing"]["extra_gate_wait_ms"] is None
    _finish_scoring(repo, parent, preparation=_scoring_preparation(snapshot), now=NOW)
    assert repo.aggregate_task(parent, now=NOW).status == "PARSING_FAILED"


def test_mode_only_snapshot_failure_has_a_finite_retry_budget(repository):
    factory, _queue, _repo, task_id, parent, _prepared = _prepare(repository, chunk_count=1)
    _enable_scoring(factory, task_id)
    with factory.begin() as session:
        task = session.get(VideoParseTask, task_id)
        config = deepcopy(task.parse_config)
        config.pop("voiceprint")
        config["execution"]["max_retry_count"] = 0
        task.parse_config = config
    result = _queue.retry_video(parent, error="snapshot transaction failed", now=NOW)
    assert result.next_execute_time is None
    with factory() as session:
        task = session.get(VideoParseTask, task_id)
        assert task.status == "PARSING_FAILED"
        assert task.detail["_worker_runtime_v2"]["voiceprint_preparation"] == {"failures": 1, "active": False}


@pytest.mark.parametrize(("mode", "edit", "speaker", "speaker_source"), [
    ("prefill", "unchanged", "B", "voice_default"),
    ("prefill", None, None, "refine_edit"),
    ("prefill", "C", "C", "refine_edit"),
    ("flag", "unchanged", "A", "understanding"),
])
def test_ready_b0_evidence_canvas_export_and_transaction_persist_speaker_source(
    repository, mode, edit, speaker, speaker_source,
):
    factory, queue, repo, task_id, parent, _prepared = _prepare(repository, chunk_count=1)
    _enable_scoring(factory, task_id, mode=mode)
    child = queue.claim_chunk("understanding", lease_duration=timedelta(minutes=1), now=NOW)
    content = _dialogues(["A", "B", "B", "B", "A"], 0)
    content["persons"].append("C")
    for position, event in enumerate(content["timeline"]):
        event.update(start_seconds=100 + position * 2000, end_seconds=1900 + position * 2000)
    content["timeline"][0]["participants"] = ["A", "B", "C"]
    repo.commit_chunk_result(child, content=content, now=NOW)
    preparation_snapshot = repo.begin_voiceprint_preparation(parent, now=NOW)
    preparation = _scoring_preparation(preparation_snapshot)
    for position, sample in enumerate(preparation["samples"]):
        sample["vector"] = ([0.0, 1.0] if position == 4 else [1.0, 0.0]) + [0.0] * 190
        sample["cue_ids"] = [sample["source_id"]]
        sample["understanding_speaker"] = sample["label"]
    preparation["parameters"] = VideoParseRuntimeService._voiceprint_parameters()
    preparation["lines"] = [VideoParseRuntimeService._score_voiceprint_sample(sample, preparation["samples"])
                            for sample in preparation["samples"]]
    assert preparation["lines"][0]["band"] == "ACCEPT"
    assert preparation["lines"][0]["candidates"][0]["speaker"] == "B"
    repo.bind_voiceprint_encoder(parent, model=preparation["model"], now=NOW)
    _finish_scoring(repo, parent, preparation=preparation, now=NOW)
    refine = queue.claim_interval_refine("refine", lease_duration=timedelta(minutes=1), now=NOW)
    assert refine is not None
    members = repo.load_interval_members(refine, now=NOW)
    manifest = repo.load_voiceprint_scoring(refine, now=NOW)
    assert manifest["checksum"] == _artifact_descriptor(parent, preparation)["checksum"]
    # ArtifactStore I/O is tested at the service boundary; use those immutable bytes here.
    stored = preparation
    member = members.members[0]
    evidence = {"mode": mode, "method_version": stored["method_version"], "lines": stored["lines"]}
    snapshot = {
        "interval_parse_task_id": refine.interval_parse_task_id,
        "member_sequence_nos": {member.chunk_parse_task_id: 0},
        "member_versions": {member.chunk_parse_task_id: member.version},
        "member_bounds": {member.chunk_parse_task_id: (0, 10000)},
        "member_scopes": {member.chunk_parse_task_id: [0]},
        "member_contents": {member.chunk_parse_task_id: deepcopy(member.content)},
        "member_evidence": {member.chunk_parse_task_id: {"voiceprint": evidence}},
    }
    canvas = ParseContextCanvas.from_refine(snapshot)
    if edit != "unchanged":
        canvas.write(role="refine", batch_id="speaker-correction", expected_revision=canvas.revision, operations=[
            {"op": "patch_event", "event_ref": canvas.items("events")[0][0], "fields": {"speaker": edit}},
        ])
    result = canvas.interval_result()
    updates = {member.chunk_parse_task_id: result.updates[0].model_dump(
        mode="json", exclude={"sequence_no"}, exclude_unset=True,
    )}
    repo.commit_interval_refinement(
        refine, interval_content=result.model_dump(mode="json"), member_updates=updates,
        **_refinement_inputs(repo, refine, now=NOW), now=NOW,
    )
    with factory() as session:
        committed = session.scalar(select(ChunkParseContent).where(
            ChunkParseContent.chunk_task_id == member.chunk_parse_task_id,
        )).content
        assert committed["timeline"][0]["speaker"] == speaker
        assert committed["timeline"][0]["speaker_source"] == speaker_source
        assert all(event.get("speaker_source") in {"understanding", "voice_default", "refine_edit"}
                   for event in committed["timeline"])
        assert session.get(VideoParseTask, task_id).detail["voiceprint"] == manifest
        assert stored["lines"][0]["candidates"][0]["speaker"] == "B"
    assert "speaker_source" not in snapshot["member_contents"][member.chunk_parse_task_id]["timeline"][0]
