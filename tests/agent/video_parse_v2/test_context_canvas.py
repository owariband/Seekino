from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from agent.utils.context_canvas import CanvasRevisionConflict, ContextCanvas
from agent.utils.context_canvas_tools import build_context_canvas_tools
from agent.utils.errors import AgentToolFeedbackError
from agent.video_parse_v2.timelineEvent_canvas import CanvasCompletion, ParseCanvasWrite, ParseContextCanvas
from agent.video_parse_v2.refine.agent import IntervalRefine
from engine.parsev2.schemas import VideoParseResult
from tests.agent.video_parse_v2.test_framework import _inputs, _lease, _result


def _canvas():
    return ParseContextCanvas(
        scope="refine:test", members={"member": {
            "sequence_no": 0, "version": 2, "bounds": (0, 20_000),
            "content": _result(),
        }}, evidence={"member": {"asr": {"transcript": [{
            "start_seconds": 1000, "end_seconds": 3000, "text": "original speech",
        }]}, "ocr": {"lines": []}}},
    )


def _voice_snapshot(mode="prefill", band="ACCEPT", *, source=None, doubtful=False):
    event = {
        **_result().timeline[0].model_dump(mode="json"),
        "event_type": "DIALOGUE", "speaker": "爱音",
        "participants": ["爱音", "立希", "睦"],
        "text_variants": [{"language": "ja", "role": "ORIGINAL", "text": "声の証拠"}],
    }
    if source:
        event["speaker_source"] = source
    snapshot = {
        "interval_parse_task_id": "voice-interval", "video_parse_task_id": "video",
        "start_sequence_no": 0, "chunk_count": 1,
        "member_sequence_nos": {"member": 0}, "member_versions": {"member": 1},
        "member_bounds": {"member": (0, 20_000)}, "member_scopes": {"member": [0]},
        "member_contents": {"member": {"persons": ["爱音", "立希", "睦"], "timeline": [event]}},
        "member_evidence": {"member": {"asr": {"transcript": []}, "voiceprint": {
            "mode": mode, "method_version": "B0-v1", "lines": [{
                "id": "line-1", "position": 0, "start_ms": 1000, "end_ms": 3000,
                "understanding_speaker": "爱音", "duration_ms": 2000,
                "band": band, "conflict": band == "CONFLICT", "gap": 0.2,
                "candidates": [
                    {"speaker": "爽世", "similarity": 0.8, "reference_ids": ["source-1"],
                     "src": [{"id": "source-1", "start_ms": 5000, "end_ms": 7000}]},
                    {"speaker": "立希", "similarity": 0.6},
                ],
            }], "conflicts": ["line-1"] if band == "CONFLICT" else [],
            "accept_disagreements": ["line-1"] if band == "ACCEPT" else [],
        }}},
    }
    if doubtful:
        snapshot["member_evidence"]["member"]["voiceprint"]["lines"][0].update(
            label_score=0.2, label_rank=3, label_count=5, label_margin=0.6,
            label_ref_count=5, label_ref_consistency=0.8, label_doubtful=True,
        )
    return snapshot


def _understanding_canvas(*, stage="understanding", evidence=None, initial=False):
    content = _voice_snapshot("off")["member_contents"]["member"]
    if not initial:
        content["timeline"] = []
    return ParseContextCanvas(
        scope="chunk:test", stage=stage, members={"member": {
            "sequence_no": 0, "version": 0, "bounds": (0, 20_000), "content": content,
        }}, evidence=evidence if evidence is not None else {"member": {"asr": {"transcript": [
            {"start_seconds": 1000, "end_seconds": 3000, "text": "first utterance"},
            {"start_seconds": 4000, "end_seconds": 6000, "text": "second utterance"},
        ]}}},
    )


@pytest.mark.parametrize("start,end", [(1000, 6000), (1100, 2900), (900, 3000)])
@pytest.mark.parametrize("operation", ["add_event", "set_event"])
def test_understanding_dialogue_cannot_merge_split_or_extend_asr_cues(start, end, operation):
    canvas = _understanding_canvas()
    event = _voice_snapshot("off")["member_contents"]["member"]["timeline"][0]
    if operation == "set_event":
        receipt = canvas.write(role="understanding", batch_id="action", expected_revision=canvas.revision, operations=[
            {"op": "add_event", "member_ref": "member", "key": "action", "event": _result().timeline[0]},
        ])
        target = {"event_ref": receipt["results"][0]["reference"]}
    else:
        target = {"member_ref": "member", "key": "dialogue"}
    before = canvas.snapshot()
    with pytest.raises(AgentToolFeedbackError) as error:
        canvas.write(role="understanding", batch_id="invalid-range", expected_revision=canvas.revision, operations=[
            {"op": "persons", "member_ref": "member", "action": "add", "values": ["新人物"]},
            {"op": operation, **target,
             "event": {**event, "start_seconds": start, "end_seconds": end}},
        ])
    assert error.value.code == "ASR_DIALOGUE_RANGE_REQUIRED"
    assert canvas.snapshot() == before


def test_understanding_dialogue_uses_only_its_own_members_asr_cues():
    canvas = _understanding_canvas(evidence={
        "member": {"asr": {"transcript": [
            {"start_seconds": 1000, "end_seconds": 3000, "text": "member speech"},
        ]}},
        "other": {"asr": {"transcript": [
            {"start_seconds": 4000, "end_seconds": 6000, "text": "other member speech"},
        ]}},
    })
    event = _voice_snapshot("off")["member_contents"]["member"]["timeline"][0]
    before = canvas.snapshot()
    with pytest.raises(AgentToolFeedbackError) as error:
        canvas.write(role="understanding", batch_id="other-member-cue", expected_revision=canvas.revision, operations=[
            {"op": "add_event", "member_ref": "member", "key": "dialogue",
             "event": {**event, "start_seconds": 4000, "end_seconds": 6000}},
        ])
    assert error.value.code == "ASR_DIALOGUE_RANGE_REQUIRED" and canvas.snapshot() == before


@pytest.mark.parametrize("stage", ["understanding", "prefix"])
@pytest.mark.parametrize("operation", ["patch_event", "set_event"])
@pytest.mark.parametrize("start,end", [(1100, 3000), (1000, 3200), (4000, 6000)])
def test_only_refine_can_change_existing_dialogue_times(stage, operation, start, end):
    canvas = _understanding_canvas(stage=stage)
    event = _voice_snapshot("off")["member_contents"]["member"]["timeline"][0]
    receipt = canvas.write(role=stage, batch_id="initial", expected_revision=canvas.revision, operations=[
        {"op": "add_event", "member_ref": "member", "key": "dialogue", "event": event},
    ])
    reference = receipt["results"][0]["reference"]
    payload = {"op": operation, "event_ref": reference}
    payload["fields" if operation == "patch_event" else "event"] = (
        {"start_seconds": start, "end_seconds": end} if operation == "patch_event"
        else {**event, "start_seconds": start, "end_seconds": end}
    )
    before = canvas.snapshot()
    with pytest.raises(AgentToolFeedbackError) as error:
        canvas.write(role=stage, batch_id="retime", expected_revision=canvas.revision, operations=[
            {"op": "patch_event", "event_ref": reference, "fields": {"description": "先改描述"}},
            payload,
        ])
    assert error.value.code == "DIALOGUE_TIMING_READ_ONLY" and canvas.snapshot() == before


@pytest.mark.parametrize("stage", ["understanding", "prefix"])
def test_non_refine_initial_dialogue_times_are_locked_before_any_write(stage):
    canvas = _understanding_canvas(stage=stage, initial=True)
    reference = canvas.items("events")[0][0]
    before = canvas.snapshot()
    with pytest.raises(AgentToolFeedbackError) as error:
        canvas.write(role=stage, batch_id="retime-initial", expected_revision=canvas.revision, operations=[
            {"op": "patch_event", "event_ref": reference, "fields": {"start_seconds": 4000, "end_seconds": 6000}},
        ])
    assert error.value.code == "DIALOGUE_TIMING_READ_ONLY" and canvas.snapshot() == before


@pytest.mark.parametrize("operation", ["patch_event", "set_event"])
def test_understanding_same_times_and_content_speaker_edits_remain_allowed(operation):
    canvas = _understanding_canvas()
    event = _voice_snapshot("off")["member_contents"]["member"]["timeline"][0]
    receipt = canvas.write(role="understanding", batch_id="initial", expected_revision=canvas.revision, operations=[
        {"op": "add_event", "member_ref": "member", "key": "dialogue", "event": event},
    ])
    reference = receipt["results"][0]["reference"]
    fields = {"start_seconds": 1000, "end_seconds": 3000, "description": "修正描述", "speaker": "睦"}
    canvas.write(role="understanding", batch_id="interpretation", expected_revision=canvas.revision, operations=[
        {"op": operation, "event_ref": reference,
         "fields" if operation == "patch_event" else "event": fields if operation == "patch_event" else {**event, **fields}},
        {"op": "event_array", "event_ref": reference, "field": "text_variants", "action": "set",
         "values": [{"language": "ja", "role": "ORIGINAL", "text": "訂正された台詞"}]},
    ])
    final = canvas.result("member").timeline[0]
    assert (final.start_seconds, final.end_seconds) == (1000, 3000)
    assert final.speaker == "睦" and final.description == "修正描述"
    assert final.text_variants[0].text == "訂正された台詞"


@pytest.mark.parametrize("batched", [False, True])
def test_understanding_cannot_bypass_dialogue_time_lock_by_changing_event_type(batched):
    canvas = _understanding_canvas()
    event = _voice_snapshot("off")["member_contents"]["member"]["timeline"][0]
    receipt = canvas.write(role="understanding", batch_id="initial", expected_revision=canvas.revision, operations=[
        {"op": "add_event", "member_ref": "member", "key": "dialogue", "event": event},
    ])
    reference = receipt["results"][0]["reference"]
    action = {"op": "set_event", "event_ref": reference,
              "event": _result().timeline[0].model_dump(mode="json")}
    if not batched:
        canvas.write(role="understanding", batch_id="action", expected_revision=canvas.revision, operations=[action])
    before = canvas.snapshot()
    with pytest.raises(AgentToolFeedbackError) as error:
        canvas.write(role="understanding", batch_id="retime-action", expected_revision=canvas.revision, operations=[
            *([action] if batched else []),
            {"op": "patch_event", "event_ref": reference, "fields": {"start_seconds": 4000, "end_seconds": 6000}},
            {"op": "set_event", "event_ref": reference, "event": {**event, "start_seconds": 4000, "end_seconds": 6000}},
        ])
    assert error.value.code == "DIALOGUE_TIMING_READ_ONLY" and canvas.snapshot() == before
    canvas.write(role="understanding", batch_id="restore-dialogue", expected_revision=canvas.revision, operations=[
        {"op": "set_event", "event_ref": reference, "event": event},
    ])
    assert canvas.result("member").timeline[0].event_type == "DIALOGUE"


def test_understanding_non_dialogue_timing_stays_editable_until_converted_to_dialogue():
    canvas = _understanding_canvas()
    receipt = canvas.write(role="understanding", batch_id="action", expected_revision=canvas.revision, operations=[
        {"op": "add_event", "member_ref": "member", "key": "action", "event": _result().timeline[0]},
    ])
    reference = receipt["results"][0]["reference"]
    canvas.write(role="understanding", batch_id="retime-action", expected_revision=canvas.revision, operations=[
        {"op": "patch_event", "event_ref": reference, "fields": {"start_seconds": 4000, "end_seconds": 6000}},
    ])
    event = {**_voice_snapshot("off")["member_contents"]["member"]["timeline"][0],
             "start_seconds": 4000, "end_seconds": 6000}
    canvas.write(role="understanding", batch_id="dialogue", expected_revision=canvas.revision, operations=[
        {"op": "set_event", "event_ref": reference, "event": event},
    ])
    before = canvas.snapshot()
    with pytest.raises(AgentToolFeedbackError) as error:
        canvas.write(role="understanding", batch_id="retime-dialogue", expected_revision=canvas.revision, operations=[
            {"op": "patch_event", "event_ref": reference, "fields": {"start_seconds": 1000, "end_seconds": 3000}},
        ])
    assert error.value.code == "DIALOGUE_TIMING_READ_ONLY" and canvas.snapshot() == before


def test_understanding_repeated_dialogue_add_keeps_idempotence_and_time_lock():
    canvas = _understanding_canvas()
    operation = {"op": "add_event", "member_ref": "member", "key": "dialogue",
                 "event": _voice_snapshot("off")["member_contents"]["member"]["timeline"][0]}
    first = canvas.write(role="understanding", batch_id="initial", expected_revision=canvas.revision, operations=[operation])
    before = canvas.snapshot()
    repeated = canvas.write(role="understanding", batch_id="same-facts", expected_revision=canvas.revision, operations=[operation])
    replayed = canvas.write(role="understanding", batch_id="initial", expected_revision=0, operations=[operation])
    assert repeated["results"] == first["results"] and replayed["replayed"]
    assert canvas.snapshot() == before
    with pytest.raises(AgentToolFeedbackError) as error:
        canvas.write(role="understanding", batch_id="retime", expected_revision=canvas.revision, operations=[
            {"op": "patch_event", "event_ref": first["results"][0]["reference"], "fields": {"end_seconds": 3200}},
        ])
    assert error.value.code == "DIALOGUE_TIMING_READ_ONLY" and canvas.snapshot() == before


@pytest.mark.parametrize("evidence", [
    {}, {"member": {"asr": None}}, {"member": {"asr": {"transcript": []}}},
    {"member": {"asr": {"transcript": [{"start_seconds": 1000, "end_seconds": 3000, "text": "  "}]}}},
])
def test_understanding_without_usable_asr_preserves_initial_media_dialogue_creation(evidence):
    canvas = _understanding_canvas(evidence=evidence)
    event = {**_voice_snapshot("off")["member_contents"]["member"]["timeline"][0],
             "start_seconds": 1700, "end_seconds": 3500}
    receipt = canvas.write(role="understanding", batch_id="media-dialogue", expected_revision=canvas.revision, operations=[
        {"op": "add_event", "member_ref": "member", "key": "dialogue", "event": event},
    ])
    assert canvas.result("member").timeline[0].start_seconds == 1700
    before = canvas.snapshot()
    with pytest.raises(AgentToolFeedbackError) as error:
        canvas.write(role="understanding", batch_id="retime", expected_revision=canvas.revision, operations=[
            {"op": "patch_event", "event_ref": receipt["results"][0]["reference"], "fields": {"end_seconds": 3600}},
        ])
    assert error.value.code == "DIALOGUE_TIMING_READ_ONLY" and canvas.snapshot() == before


def test_refine_can_create_and_retime_dialogue_independently_of_asr_cue_bounds():
    canvas = _understanding_canvas(stage="refine")
    event = {**_voice_snapshot("off")["member_contents"]["member"]["timeline"][0], "end_seconds": 6000}
    receipt = canvas.write(role="refine", batch_id="dialogue", expected_revision=canvas.revision, operations=[
        {"op": "add_event", "member_ref": "member", "key": "dialogue", "event": event},
    ])
    canvas.write(role="refine", batch_id="retime", expected_revision=canvas.revision, operations=[
        {"op": "patch_event", "event_ref": receipt["results"][0]["reference"],
         "fields": {"start_seconds": 2000, "end_seconds": 7000}},
    ])
    final = canvas.result("member").timeline[0]
    assert (final.start_seconds, final.end_seconds) == (2000, 7000)


@pytest.mark.parametrize("band", ["ACCEPT", "CONFLICT", "NO_VOICE", "REVIEW"])
def test_flag_preserves_speaker_and_original_snapshot_for_every_band(band):
    snapshot = _voice_snapshot("flag", band)
    original = deepcopy(snapshot)
    canvas = ParseContextCanvas.from_refine(snapshot)
    event = canvas.result("member").timeline[0]
    assert event.speaker == "爱音" and event.speaker_source == "understanding"
    assert canvas._initial["member"].model_dump(mode="json") == original["member_contents"]["member"]
    assert snapshot == original
    assert canvas.interval_result().updates[0].timeline[0].speaker_source == "understanding"


@pytest.mark.parametrize("band,expected", [
    ("ACCEPT", "爽世"), ("CONFLICT", "爱音"), ("NO_VOICE", "爱音"), ("REVIEW", "爱音"),
])
def test_prefill_only_accept_exports_default_without_polluting_initial(band, expected):
    snapshot = _voice_snapshot(band=band)
    original = deepcopy(snapshot)
    canvas = ParseContextCanvas.from_refine(snapshot)
    event = canvas.interval_result().updates[0].timeline[0]
    assert event.speaker == expected
    assert event.speaker_source == ("voice_default" if band == "ACCEPT" else "understanding")
    if band == "ACCEPT":
        assert "爽世" in event.participants and "爽世" in canvas.result("member").persons
    assert snapshot == original
    assert canvas._initial["member"].model_dump(mode="json") == original["member_contents"]["member"]


def test_voice_off_preserves_historical_draft_and_exports_no_changes():
    snapshot = _voice_snapshot("off")
    canvas = ParseContextCanvas.from_refine(snapshot)
    assert canvas.voiceprint_review() is None
    assert canvas.result("member").model_dump(mode="json") == snapshot["member_contents"]["member"]
    assert canvas.interval_result().updates == []


@pytest.mark.parametrize("candidates", [[], [{"speaker": "爽世", "similarity": 0.8}]])
def test_no_voice_preserves_unknown_speaker_with_empty_or_single_candidate(candidates):
    snapshot = _voice_snapshot(band="NO_VOICE")
    snapshot["member_contents"]["member"]["timeline"][0]["speaker"] = None
    line = snapshot["member_evidence"]["member"]["voiceprint"]["lines"][0]
    line.update(understanding_speaker=None, candidates=candidates, gap=None)
    canvas = ParseContextCanvas.from_refine(snapshot)
    event = canvas.interval_result().updates[0].timeline[0]
    assert event.speaker is None and event.speaker_source == "understanding"


@pytest.mark.parametrize("speaker", ["爽世", "立希", "睦", None])
def test_explicit_speaker_edits_including_null_and_outside_candidates_survive(speaker):
    canvas = ParseContextCanvas.from_refine(_voice_snapshot())
    reference, value = canvas.items("events")[0]
    original_evidence = canvas.get("evidence", "member")
    ranking = deepcopy(value["voice_evidence"])
    assert ranking["line_id"] == "line-1" and ranking["evidence_ref"] == "member"
    assert "reference_ids" not in ranking["candidates"][0]
    assert "src" not in ranking["candidates"][0]
    canvas.write(role="refine", batch_id="speaker", expected_revision=canvas.revision, operations=[
        {"op": "patch_event", "event_ref": reference, "fields": {"speaker": speaker}},
    ])
    result = canvas.interval_result().updates[0].timeline[0]
    assert result.speaker == speaker
    assert result.speaker_source == ("voice_default" if speaker == "爽世" else "refine_edit")
    assert canvas.get("events", reference)["voice_evidence"] == ranking
    assert canvas.get("evidence", "member") == original_evidence


def test_prior_refine_edit_is_not_overwritten_by_default_in_another_window():
    snapshot = _voice_snapshot(source="refine_edit")
    canvas = ParseContextCanvas.from_refine(snapshot)
    assert canvas.result("member").timeline[0].speaker == "爱音"
    assert canvas.interval_result().updates == []


@pytest.mark.parametrize("explicit", [False, True])
def test_changed_audio_interval_invalidates_scores_and_unreviewed_default(explicit):
    canvas = ParseContextCanvas.from_refine(_voice_snapshot())
    reference = canvas.items("events")[0][0]
    original_evidence = canvas.get("evidence", "member")
    fields = {"start_seconds": 1100}
    if explicit:
        fields["speaker"] = None
    canvas.write(role="refine", batch_id="new-audio-range", expected_revision=canvas.revision, operations=[
        {"op": "patch_event", "event_ref": reference, "fields": fields},
    ])
    value = canvas.get("events", reference)
    assert value["voice_evidence"]["valid"] is False
    assert value["voice_evidence"]["invalid_reason"] == "audio_interval_changed"
    assert value["event"]["speaker"] == (None if explicit else "爱音")
    assert value["event"]["speaker_source"] == ("refine_edit" if explicit else "understanding")
    assert canvas.get("evidence", "member") == original_evidence


def test_text_only_edit_keeps_voice_evidence_valid_and_source_host_owned():
    canvas = ParseContextCanvas.from_refine(_voice_snapshot())
    reference, value = canvas.items("events")[0]
    canvas.write(role="refine", batch_id="wording", expected_revision=canvas.revision, operations=[
        {"op": "event_array", "event_ref": reference, "field": "text_variants", "action": "set",
         "values": [{"language": "ja", "role": "ORIGINAL", "text": "訂正文"}]},
    ])
    assert canvas.get("events", reference)["voice_evidence"] == value["voice_evidence"]
    assert canvas.result("member").timeline[0].speaker_source == "voice_default"
    event = canvas.result("member").timeline[0].model_dump(mode="json")
    event["speaker_source"] = "understanding"
    event["text_variants"][0]["text"] = "再び訂正された文"
    canvas.write(role="refine", batch_id="complete-event", expected_revision=canvas.revision, operations=[
        {"op": "set_event", "event_ref": reference, "event": event},
    ])
    assert canvas.result("member").timeline[0].speaker_source == "voice_default"


@pytest.mark.parametrize("operation", ["patch_event", "set_event"])
@pytest.mark.parametrize("source", ["understanding", "voice_default", "refine_edit"])
@pytest.mark.parametrize("speaker", ["爱音", None])
def test_same_speaker_value_preserves_source_including_previously_null(operation, source, speaker):
    snapshot = _voice_snapshot("flag", source=source)
    snapshot["member_contents"]["member"]["timeline"][0]["speaker"] = speaker
    canvas = ParseContextCanvas.from_refine(snapshot)
    reference, value = canvas.items("events")[0]
    payload = {"op": operation, "event_ref": reference}
    if operation == "patch_event":
        payload["fields"] = {"speaker": speaker}
    else:
        payload["event"] = {**value["event"], "description": "text-only factual correction",
                            "speaker_source": "understanding"}
    canvas.write(role="refine", batch_id="same-speaker", expected_revision=canvas.revision, operations=[payload])
    result = canvas.result("member").timeline[0]
    assert result.speaker == speaker and result.speaker_source == source


@pytest.mark.parametrize("speaker", ["立希", None])
def test_set_event_actual_speaker_change_records_refine_edit(speaker):
    canvas = ParseContextCanvas.from_refine(_voice_snapshot())
    reference, value = canvas.items("events")[0]
    canvas.write(role="refine", batch_id="change-speaker", expected_revision=canvas.revision, operations=[
        {"op": "set_event", "event_ref": reference,
         "event": {**value["event"], "speaker": speaker, "speaker_source": "understanding"}},
    ])
    result = canvas.result("member").timeline[0]
    assert result.speaker == speaker and result.speaker_source == "refine_edit"


@pytest.mark.parametrize("operation", ["patch_event", "set_event"])
def test_same_speaker_value_does_not_accept_default_after_interval_change(operation):
    canvas = ParseContextCanvas.from_refine(_voice_snapshot())
    reference, value = canvas.items("events")[0]
    payload = {"op": operation, "event_ref": reference}
    if operation == "patch_event":
        payload["fields"] = {"start_seconds": 1100, "speaker": "爽世"}
    else:
        payload["event"] = {**value["event"], "start_seconds": 1100}
    canvas.write(role="refine", batch_id="changed-audio", expected_revision=canvas.revision, operations=[payload])
    result = canvas.result("member").timeline[0]
    assert result.speaker == "爱音" and result.speaker_source == "understanding"
    assert canvas.get("events", reference)["voice_evidence"]["valid"] is False


@pytest.mark.parametrize("speaker,source", [("立希", "refine_edit"), (None, "understanding")])
def test_add_dialogue_source_uses_null_prior_and_repeated_add_preserves_it(speaker, source):
    canvas = ParseContextCanvas.from_refine(_voice_snapshot("flag"))
    event = {**canvas.items("events")[0][1]["event"], "start_seconds": 5000,
             "end_seconds": 7000, "speaker": speaker, "speaker_source": "voice_default"}
    payload = {"op": "add_event", "member_ref": "member", "key": "new-dialogue", "event": event}
    receipt = canvas.write(role="refine", batch_id="new", expected_revision=canvas.revision, operations=[payload])
    reference = receipt["results"][0]["reference"]
    assert canvas.get("events", reference)["event"]["speaker_source"] == source
    revision = canvas.revision
    canvas.write(role="refine", batch_id="repeat", expected_revision=revision, operations=[payload])
    assert canvas.revision == revision
    assert canvas.get("events", reference)["event"]["speaker_source"] == source


def test_prefix_edit_of_source_bearing_event_records_new_source():
    snapshot = _voice_snapshot(source="voice_default")
    canvas = ParseContextCanvas(scope="prefix:source", stage="prefix", members={"member": {
        "sequence_no": 0, "version": 1, "bounds": (0, 20_000),
        "content": snapshot["member_contents"]["member"],
    }})
    reference = canvas.items("events")[0][0]
    canvas.write(role="prefix", batch_id="speaker", expected_revision=canvas.revision, operations=[
        {"op": "patch_event", "event_ref": reference, "fields": {"speaker": None}},
    ])
    assert canvas.result("member").timeline[0].speaker_source == "refine_edit"


def test_off_mode_ignores_model_supplied_source_on_complete_event_write():
    canvas = ParseContextCanvas.from_refine(_voice_snapshot("off"))
    reference, value = canvas.items("events")[0]
    canvas.write(role="refine", batch_id="untrusted-source", expected_revision=canvas.revision, operations=[
        {"op": "set_event", "event_ref": reference,
         "event": {**value["event"], "speaker_source": "voice_default"}},
    ])
    assert canvas.result("member").timeline[0].speaker_source is None
    assert canvas.interval_result().updates == []


@pytest.mark.parametrize("mode,expected", [("flag", "爱音"), ("prefill", "爽世")])
def test_normal_refine_reads_prepared_draft_and_commits_default_once(mode, expected):
    snapshot = _voice_snapshot(mode)
    original = deepcopy(snapshot)

    async def provider(_snapshot, *, canvas, prompt):
        assert canvas.result("member").timeline[0].speaker == expected
        review = json.loads(next(line for line in prompt.splitlines()
                                 if line.startswith("Required voice review lists: ")).split(": ", 1)[1])
        assert review["accept_disagreements"][0]["event_ref"] == canvas.items("events")[0][0]
        canvas.write(role="refine", batch_id="voice-review", expected_revision=canvas.revision, operations=[{
            "op": "voice_decision", "event_ref": canvas.items("events")[0][0],
            "decision": "voice_top1" if mode == "prefill" else "keep", "reason": "Verified the visible speaker.",
        }])
        assert canvas.finish_voiceprint_review()["ready"]
        return CanvasCompletion(complete=True)

    runtime = SimpleNamespace(
        load_refine_input=AsyncMock(return_value=snapshot),
        generate_interval_refinement=AsyncMock(side_effect=provider),
        commit_interval_refinement=AsyncMock(return_value=True), notify_interval_committed=AsyncMock(),
    )
    asyncio.run(IntervalRefine(runtime=runtime).InvokeRefine("voice-interval"))
    runtime.commit_interval_refinement.assert_awaited_once()
    result = runtime.commit_interval_refinement.await_args.args[1].updates[0].timeline[0]
    assert result.speaker == expected
    assert result.speaker_source == ("voice_default" if mode == "prefill" else "understanding")
    assert snapshot == original


@pytest.mark.parametrize("mode", ["flag", "prefill"])
@pytest.mark.parametrize("band", ["CONFLICT", "ACCEPT"])
def test_voice_review_reports_missing_decision_and_requires_finish(mode, band):
    canvas = ParseContextCanvas.from_refine(_voice_snapshot(mode, band))
    reference = canvas.items("events")[0][0]
    feedback = canvas.finish_voiceprint_review()
    assert feedback == {
        "ready": False, "missing": [reference], "inconsistent": [],
        "retry_hint": "Record a voice_decision for each listed event (edit the speaker only if your decision changes it), then call finish_voiceprint_review again.",
        "revision": canvas.revision,
    }
    assert not canvas.voiceprint_review_passed()
    before = canvas.snapshot()
    diagnostics = canvas.voiceprint_review_diagnostics()
    assert diagnostics["missing"] == [reference] and diagnostics["inconsistent"] == []
    assert diagnostics["gate_failure"] == "decisions_unresolved" and not diagnostics["gate_accepted"]
    assert diagnostics["events"][0]["problem"] == "missing_decision"
    assert diagnostics["events"][0]["decision"] is diagnostics["events"][0]["reason"] is None
    assert canvas.snapshot() == before
    with pytest.raises(AgentToolFeedbackError) as error:
        canvas.voiceprint_decisions()
    assert error.value.code == "VOICEPRINT_REVIEW_REQUIRED"


@pytest.mark.parametrize("decision,speaker,problem", [
    ("keep", "爽世", "keep_speaker_mismatch"), ("voice_top1", "爱音", "voice_top1_speaker_mismatch"),
    ("other", "爱音", "other_matches_understanding_or_voice_top1"),
    ("other", "爽世", "other_matches_understanding_or_voice_top1"),
    ("other", None, "other_named_speaker_required"), ("null", "爽世", "null_speaker_mismatch"),
])
def test_voice_review_reports_each_inconsistent_decision(decision, speaker, problem):
    canvas = ParseContextCanvas.from_refine(_voice_snapshot())
    reference = canvas.items("events")[0][0]
    canvas.write(role="refine", batch_id="inconsistent", expected_revision=canvas.revision, operations=[
        {"op": "patch_event", "event_ref": reference, "fields": {"speaker": speaker}},
        {"op": "voice_decision", "event_ref": reference, "decision": decision,
         "reason": "Another named person is visibly speaking this line." if decision == "null" else "Specific visible evidence.",
         **({"evidence_type": "contradiction"} if decision == "null" else {})},
    ])
    status = canvas.finish_voiceprint_review()
    assert status["ready"] is False and status["missing"] == [] and status["inconsistent"] == [reference]
    assert not canvas.voiceprint_review_passed()
    before = canvas.snapshot()
    diagnostics = canvas.voiceprint_review_diagnostics()
    assert diagnostics == {
        "canvas_id": canvas.id, "revision": canvas.revision, "ready": False, "gate_accepted": False,
        "gate_failure": "decisions_unresolved", "missing": [], "inconsistent": [reference],
        "events": [{
            "event_ref": reference, "member_chunk_id": "member", "start_ms": 1000, "end_ms": 3000,
            "understanding_speaker": "爱音", "voice_top1": "爽世", "gap": 0.2, "band": "ACCEPT",
            "label_doubtful": None, "decision": decision,
            "reason": ("Another named person is visibly speaking this line." if decision == "null"
                       else "Specific visible evidence."),
            "evidence_type": "contradiction" if decision == "null" else None, "draft_speaker": speaker,
            "speaker_source": canvas.get("events", reference)["event"]["speaker_source"],
            "expected_speaker": "爱音" if decision == "keep" else "爽世" if decision == "voice_top1" else None,
            "problem": problem,
        }],
    }
    assert canvas.snapshot() == before


@pytest.mark.parametrize("decision,speaker", [
    ("keep", "爱音"), ("voice_top1", "爽世"), ("other", "睦"), ("null", None),
])
def test_voice_review_accepts_four_consistent_decisions_and_exports_fields(decision, speaker):
    snapshot = _voice_snapshot()
    original = deepcopy(snapshot)
    canvas = ParseContextCanvas.from_refine(snapshot)
    reference = canvas.items("events")[0][0]
    reason = "Two simultaneous speakers overlap in this target audio." if decision == "null" else "Visible mouth movement."
    evidence = {"evidence_type": "contradiction"} if decision == "null" else {}
    canvas.write(role="refine", batch_id="resolve", expected_revision=canvas.revision, operations=[
        {"op": "patch_event", "event_ref": reference, "fields": {"speaker": speaker}},
        {"op": "voice_decision", "event_ref": reference, "decision": decision, "reason": reason, **evidence},
    ])
    assert canvas.voiceprint_review_status()["ready"] and not canvas.voiceprint_review_passed()
    pending = canvas.voiceprint_review_diagnostics()
    assert pending["ready"] and not pending["gate_accepted"] and pending["events"] == []
    assert pending["gate_failure"] == "gate_not_accepted_for_current_draft"
    receipt = canvas.finish_voiceprint_review()
    assert receipt["ready"] and receipt["missing"] == receipt["inconsistent"] == []
    assert receipt["revision"] == canvas.revision and canvas.voiceprint_review_passed()
    accepted = canvas.voiceprint_review_diagnostics()
    assert accepted["ready"] and accepted["gate_accepted"] and accepted["gate_failure"] is None
    assert accepted["missing"] == accepted["inconsistent"] == accepted["events"] == []
    assert canvas.voiceprint_decisions() == [{
        "event_ref": reference, "member_chunk_id": "member", "start_ms": 1000, "end_ms": 3000,
        "understanding_speaker": "爱音", "voice_top1": "爽世", "gap": 0.2, "band": "ACCEPT",
        "decision": decision, "reason": reason, **evidence, "final_speaker": speaker,
    }]
    assert snapshot == original
    assert canvas._initial["member"].model_dump(mode="json") == original["member_contents"]["member"]


def test_voice_review_diagnostics_identify_unavailable_top1_without_exposing_source_payloads():
    snapshot = _voice_snapshot("flag", "CONFLICT")
    snapshot["member_evidence"]["member"]["voiceprint"]["lines"][0]["candidates"] = []
    canvas = ParseContextCanvas.from_refine(snapshot)
    reference = canvas.items("events")[0][0]
    canvas.write(role="refine", batch_id="no-candidate", expected_revision=canvas.revision, operations=[{
        "op": "voice_decision", "event_ref": reference, "decision": "voice_top1", "reason": "Visible speaker.",
    }])
    before = canvas.snapshot()
    details = canvas.voiceprint_review_diagnostics()
    assert details["inconsistent"] == [reference] and not details["gate_accepted"]
    event = details["events"][0]
    assert event["problem"] == "voice_top1_unavailable"
    assert event["voice_top1"] is event["expected_speaker"] is None
    assert not {"text", "text_variants", "candidates", "src", "reference_ids", "embedding", "vector", "prompt"}.intersection(event)
    assert canvas.snapshot() == before


@pytest.mark.parametrize("reason", ["", " \n\t", "x" * 301])
def test_voice_decision_rejects_empty_blank_and_oversized_reasons(reason):
    canvas = ParseContextCanvas.from_refine(_voice_snapshot())
    before = canvas.snapshot()
    with pytest.raises(ValidationError):
        canvas.write(role="refine", batch_id="bad-reason", expected_revision=canvas.revision, operations=[{
            "op": "voice_decision", "event_ref": canvas.items("events")[0][0],
            "decision": "voice_top1", "reason": reason,
        }])
    assert canvas.snapshot() == before


@pytest.mark.parametrize("reason", ["x", "x" * 300])
def test_voice_decision_accepts_reason_length_boundaries(reason):
    canvas = ParseContextCanvas.from_refine(_voice_snapshot())
    reference = canvas.items("events")[0][0]
    canvas.write(role="refine", batch_id="reason-boundary", expected_revision=canvas.revision, operations=[{
        "op": "voice_decision", "event_ref": reference, "decision": "voice_top1", "reason": reason,
    }])
    assert canvas.finish_voiceprint_review()["ready"]
    assert canvas.voiceprint_decisions()[0]["reason"] == reason


def test_voice_decision_schema_forbids_unknown_values_and_extra_fields():
    from agent.video_parse_v2.timelineEvent_canvas import FinishVoiceprintReview

    canvas = ParseContextCanvas.from_refine(_voice_snapshot())
    reference = canvas.items("events")[0][0]
    for fields in ({"decision": "accept"}, {"final_speaker": "爽世"}, {"op": "resolve_voice"}):
        with pytest.raises(ValidationError):
            canvas.write(role="refine", batch_id="bad-schema", expected_revision=canvas.revision, operations=[{
                "op": "voice_decision", "event_ref": reference, "decision": "voice_top1", "reason": "Visible speaker.", **fields,
            }])
    assert FinishVoiceprintReview(complete=True).complete
    for values in ({"complete": False}, {"complete": True, "ready": True}):
        with pytest.raises(ValidationError):
            FinishVoiceprintReview.model_validate(values)


@pytest.mark.parametrize("operation", ["patch_event", "set_event"])
def test_later_speaker_edit_invalidates_record_and_gate_but_text_edit_keeps_them(operation):
    canvas = ParseContextCanvas.from_refine(_voice_snapshot())
    reference = canvas.items("events")[0][0]
    canvas.write(role="refine", batch_id="decide", expected_revision=canvas.revision, operations=[{
        "op": "voice_decision", "event_ref": reference, "decision": "voice_top1", "reason": "Visible speaker.",
    }])
    assert canvas.finish_voiceprint_review()["ready"]
    record = canvas.get("events", reference)["voice_decision"]
    for label, fields in (("text", {"description": "Corrected description."}), ("speaker", {"speaker": None})):
        payload = {"op": operation, "event_ref": reference}
        payload["fields" if operation == "patch_event" else "event"] = (
            fields if operation == "patch_event" else {**canvas.get("events", reference)["event"], **fields}
        )
        canvas.write(role="refine", batch_id=label, expected_revision=canvas.revision, operations=[payload])
        if label == "text":
            assert canvas.get("events", reference)["voice_decision"] == record
            assert canvas.voiceprint_review_passed()
        else:
            assert "voice_decision" not in canvas.get("events", reference)
            assert not canvas.voiceprint_review_passed()
            assert canvas.voiceprint_review_status()["missing"] == [reference]


def test_changed_interval_removes_decision_and_requirement_and_requires_fresh_gate():
    canvas = ParseContextCanvas.from_refine(_voice_snapshot())
    reference = canvas.items("events")[0][0]
    canvas.write(role="refine", batch_id="decide", expected_revision=canvas.revision, operations=[{
        "op": "voice_decision", "event_ref": reference, "decision": "voice_top1", "reason": "Visible speaker.",
    }])
    assert canvas.finish_voiceprint_review()["ready"]
    canvas.write(role="refine", batch_id="interval", expected_revision=canvas.revision, operations=[{
        "op": "patch_event", "event_ref": reference, "fields": {"start_seconds": 1100},
    }])
    value = canvas.get("events", reference)
    assert value["voice_evidence"]["valid"] is False and "voice_decision" not in value
    assert canvas.voiceprint_review()["conflicts"] == canvas.voiceprint_review()["accept_disagreements"] == []
    assert canvas.voiceprint_review_status()["ready"] and not canvas.voiceprint_review_passed()
    diagnostics = canvas.voiceprint_review_diagnostics()
    assert diagnostics["events"] == [] and diagnostics["gate_failure"] == "gate_not_accepted_for_current_draft"
    assert canvas.finish_voiceprint_review()["ready"] and canvas.voiceprint_decisions() == []
    assert "voice" not in canvas.catalog("events", role="refine")[0]


def test_restoring_a_prior_decision_cannot_reuse_its_invalidated_gate():
    canvas = ParseContextCanvas.from_refine(_voice_snapshot())
    reference = canvas.items("events")[0][0]
    record = {"op": "voice_decision", "event_ref": reference, "decision": "voice_top1", "reason": "Visible speaker."}
    canvas.write(role="refine", batch_id="initial", expected_revision=canvas.revision, operations=[record])
    assert canvas.finish_voiceprint_review()["ready"]
    canvas.write(role="refine", batch_id="change", expected_revision=canvas.revision, operations=[{
        "op": "patch_event", "event_ref": reference, "fields": {"speaker": None},
    }])
    canvas.write(role="refine", batch_id="restore", expected_revision=canvas.revision, operations=[
        {"op": "patch_event", "event_ref": reference, "fields": {"speaker": "爽世"}}, record,
    ])
    assert canvas.voiceprint_review_status()["ready"] and not canvas.voiceprint_review_passed()
    assert canvas.finish_voiceprint_review()["ready"] and canvas.voiceprint_review_passed()


def test_reason_only_decision_replacement_requires_a_new_gate():
    canvas = ParseContextCanvas.from_refine(_voice_snapshot())
    reference = canvas.items("events")[0][0]
    operation = {"op": "voice_decision", "event_ref": reference, "decision": "voice_top1", "reason": "Visible speaker."}
    canvas.write(role="refine", batch_id="initial", expected_revision=canvas.revision, operations=[operation])
    assert canvas.finish_voiceprint_review()["ready"] and canvas.voiceprint_review_passed()
    speaker = canvas.result("member").timeline[0].speaker
    canvas.write(role="refine", batch_id="new-basis", expected_revision=canvas.revision, operations=[{
        **operation, "reason": "The named person is visibly speaking in the inspected frame.",
    }])
    assert canvas.result("member").timeline[0].speaker == speaker
    assert canvas.voiceprint_review_status()["ready"] and not canvas.voiceprint_review_passed()
    with pytest.raises(AgentToolFeedbackError) as error:
        canvas.voiceprint_decisions()
    assert error.value.code == "VOICEPRINT_REVIEW_REQUIRED"
    assert canvas.finish_voiceprint_review()["ready"] and canvas.voiceprint_review_passed()
    assert canvas.voiceprint_decisions()[0]["reason"] == "The named person is visibly speaking in the inspected frame."


@pytest.mark.parametrize("write_bounds", [(0, 2000), (2000, 20_000), (3000, 20_000)])
def test_voice_review_excludes_conflicts_outside_write_bounds(write_bounds):
    snapshot = _voice_snapshot("flag", "CONFLICT")
    snapshot["write_bounds"] = write_bounds
    canvas = ParseContextCanvas.from_refine(snapshot)
    assert canvas.voiceprint_review()["conflicts"]
    assert canvas.voiceprint_review_status()["missing"] == []
    assert canvas.finish_voiceprint_review()["ready"] and canvas.voiceprint_decisions() == []
    with pytest.raises(AgentToolFeedbackError) as error:
        canvas.write(role="refine", batch_id="outside", expected_revision=canvas.revision, operations=[{
            "op": "voice_decision", "event_ref": canvas.items("events")[0][0],
            "decision": "keep", "reason": "Outside current window.",
        }])
    assert error.value.code == "INVALID_VOICEPRINT_DECISION"


def test_voice_review_off_preserves_catalog_state_and_exports_nothing():
    canvas = ParseContextCanvas.from_refine(_voice_snapshot("off"))
    before = canvas.snapshot()
    assert canvas.voiceprint_review_status() == {"ready": True, "missing": [], "inconsistent": [], "retry_hint": ""}
    assert canvas.voiceprint_review_passed() and canvas.voiceprint_decisions() == []
    diagnostics = canvas.voiceprint_review_diagnostics()
    assert diagnostics["gate_accepted"] and diagnostics["gate_failure"] is None and diagnostics["events"] == []
    assert canvas.finish_voiceprint_review()["ready"] and canvas.snapshot() == before
    assert canvas.catalog("events", role="refine") == [{
        "reference": canvas.items("events")[0][0], "member_ref": "member", "event_type": "DIALOGUE",
    }]


def test_checkpoint_shows_only_compact_voice_summary_for_enabled_dialogue():
    canvas = ParseContextCanvas.from_refine(_voice_snapshot("flag", "CONFLICT"))
    event = json.loads(canvas.checkpoint())["collections"]["events"][0]
    assert event == {
        "reference": canvas.items("events")[0][0], "member_ref": "member", "event_type": "DIALOGUE",
        "speaker": "爱音", "voice": {"top1": "爽世", "gap": 0.2, "band": "CONFLICT", "conflict": True,
                                  "label_doubtful": False, "label_rank": None, "label_margin": None},
    }
    assert len(json.dumps(event, ensure_ascii=False).encode()) < 300
    assert not {"candidates", "src", "reference_ids", "reason", "understanding_speaker"}.intersection(event)
    canvas.write(role="refine", batch_id="scene", expected_revision=canvas.revision, operations=[{
        "op": "set_event", "event_ref": event["reference"],
        "event": {**_result().timeline[0].model_dump(mode="json"), "event_type": "SCENE"},
    }])
    assert canvas.catalog("events", role="refine") == [{
        "reference": event["reference"], "member_ref": "member", "event_type": "SCENE",
    }]


def test_enabled_dialogue_without_evidence_still_shows_current_speaker():
    snapshot = _voice_snapshot("flag")
    snapshot["member_evidence"]["member"]["voiceprint"]["lines"] = []
    canvas = ParseContextCanvas.from_refine(snapshot)
    assert canvas.catalog("events", role="refine") == [{
        "reference": canvas.items("events")[0][0], "member_ref": "member", "event_type": "DIALOGUE", "speaker": "爱音",
    }]
    assert not canvas.voiceprint_review_passed()
    assert canvas.finish_voiceprint_review()["ready"] and canvas.voiceprint_decisions() == []


@pytest.mark.parametrize("mode", ["flag", "prefill"])
@pytest.mark.parametrize("band", ["NO_VOICE", None])
def test_doubtful_label_requires_review_without_a_voice_conflict_or_prefill(mode, band):
    canvas = ParseContextCanvas.from_refine(_voice_snapshot(mode, band, doubtful=True))
    reference, value = canvas.items("events")[0]
    review = canvas.voiceprint_review()
    assert review["conflicts"] == review["accept_disagreements"] == []
    assert review["doubtful_labels"][0]["event_ref"] == reference
    assert value["event"]["speaker"] == "爱音" and value["event"]["speaker_source"] == "understanding"
    assert canvas.finish_voiceprint_review()["missing"] == [reference]
    assert json.loads(canvas.checkpoint())["collections"]["events"][0]["voice"] == {
        "top1": "爽世", "gap": 0.2, "band": band, "conflict": False,
        "label_doubtful": True, "label_rank": "3/5", "label_margin": 0.6,
    }


@pytest.mark.parametrize("evidence_type", [None, "named_address", "visible_speaking", "text_identity"])
def test_doubtful_keep_requires_declared_identity_evidence_and_exports_metrics(evidence_type):
    snapshot = _voice_snapshot("flag", None, doubtful=True)
    original = deepcopy(snapshot)
    canvas = ParseContextCanvas.from_refine(snapshot)
    reference = canvas.items("events")[0][0]
    reasons = {
        "named_address": "The speaker is addressed by name as 爱音.",
        "visible_speaking": "爱音 is visibly speaking in the inspected frame.",
        "text_identity": "The utterance explicitly identifies its speaker as 爱音.",
    }
    operation = {
        "op": "voice_decision", "event_ref": reference, "decision": "keep",
        "reason": reasons.get(evidence_type, "Turn order suggests 爱音."),
    }
    if evidence_type is not None:
        operation["evidence_type"] = evidence_type
    before = canvas.snapshot()
    if evidence_type is None:
        with pytest.raises(AgentToolFeedbackError) as error:
            canvas.write(role="refine", batch_id="keep", expected_revision=canvas.revision, operations=[operation])
        assert error.value.code == "VOICEPRINT_KEEP_EVIDENCE_REQUIRED"
        assert "cite the observation" in error.value.retry_hint
        assert canvas.snapshot() == before
        assert canvas.finish_voiceprint_review()["missing"] == [reference]
    else:
        canvas.write(role="refine", batch_id="keep", expected_revision=canvas.revision, operations=[operation])
        assert canvas.finish_voiceprint_review()["ready"]
        assert canvas.voiceprint_decisions() == [{
            "event_ref": reference, "member_chunk_id": "member", "start_ms": 1000, "end_ms": 3000,
            "understanding_speaker": "爱音", "voice_top1": "爽世", "gap": 0.2, "band": None,
            "label_score": 0.2, "label_rank": 3, "label_count": 5, "label_margin": 0.6,
            "label_ref_count": 5, "label_ref_consistency": 0.8, "label_doubtful": True,
            "decision": "keep", "reason": operation["reason"], "evidence_type": evidence_type,
            "final_speaker": "爱音",
        }]
    assert snapshot == original
    assert canvas._initial["member"].model_dump(mode="json") == original["member_contents"]["member"]


@pytest.mark.parametrize("decision,speaker", [("voice_top1", "爽世"), ("other", "睦"), ("null", None)])
def test_doubtful_non_keep_decisions_need_no_identity_evidence(decision, speaker):
    canvas = ParseContextCanvas.from_refine(_voice_snapshot("prefill", "ACCEPT", doubtful=True))
    reference = canvas.items("events")[0][0]
    reason = "Two simultaneous speakers overlap in this target audio." if decision == "null" else "The inspected frame identifies this speaker."
    evidence = {"evidence_type": "contradiction"} if decision == "null" else {}
    canvas.write(role="refine", batch_id="resolve", expected_revision=canvas.revision, operations=[
        {"op": "patch_event", "event_ref": reference, "fields": {"speaker": speaker}},
        {"op": "voice_decision", "event_ref": reference, "decision": decision, "reason": reason, **evidence},
    ])
    assert canvas.finish_voiceprint_review()["ready"]
    record = canvas.voiceprint_decisions()[0]
    assert record["final_speaker"] == speaker and record["label_doubtful"] is True
    if decision == "null":
        assert record["evidence_type"] == "contradiction" and record["reason"] == reason
    else:
        assert "evidence_type" not in record


def test_doubtful_gate_checks_stored_keep_evidence_and_type_changes_invalidate_it():
    canvas = ParseContextCanvas.from_refine(_voice_snapshot("flag", None, doubtful=True))
    reference = canvas.items("events")[0][0]

    def legacy_record(draft):
        draft["events"][reference]["voice_decision"] = {"decision": "keep", "reason": "Earlier turn order."}

    canvas.mutate(legacy_record)
    assert canvas.finish_voiceprint_review()["inconsistent"] == [reference]
    assert canvas.voiceprint_review_diagnostics()["events"][0]["problem"] == "keep_identity_evidence_required"
    operation = {
        "op": "voice_decision", "event_ref": reference, "decision": "keep", "reason": "爱音 is visibly speaking.",
        "evidence_type": "visible_speaking",
    }
    canvas.write(role="refine", batch_id="visible", expected_revision=canvas.revision, operations=[operation])
    assert canvas.finish_voiceprint_review()["ready"] and canvas.voiceprint_review_passed()
    canvas.write(role="refine", batch_id="identity", expected_revision=canvas.revision, operations=[{
        **operation, "evidence_type": "text_identity",
    }])
    assert canvas.voiceprint_review_status()["ready"] and not canvas.voiceprint_review_passed()
    assert canvas.finish_voiceprint_review()["ready"]


def test_doubtful_evidence_type_rejects_unknown_values_and_keep_still_requires_a_nonblank_reason():
    canvas = ParseContextCanvas.from_refine(_voice_snapshot("flag", None, doubtful=True))
    before = canvas.snapshot()
    for fields in ({"evidence_type": "turn_order"}, {"evidence_type": "named_address", "reason": " \n"}):
        with pytest.raises(ValidationError):
            canvas.write(role="refine", batch_id="invalid", expected_revision=canvas.revision, operations=[{
                "op": "voice_decision", "event_ref": canvas.items("events")[0][0],
                "decision": "keep", "reason": "The speaker is addressed by name.", **fields,
            }])
    assert canvas.snapshot() == before


def test_changed_interval_removes_doubtful_requirement_and_catalog_evidence():
    canvas = ParseContextCanvas.from_refine(_voice_snapshot("flag", "NO_VOICE", doubtful=True))
    reference = canvas.items("events")[0][0]
    canvas.write(role="refine", batch_id="range", expected_revision=canvas.revision, operations=[{
        "op": "patch_event", "event_ref": reference, "fields": {"end_seconds": 2900},
    }])
    assert canvas.voiceprint_review()["doubtful_labels"] == []
    assert canvas.finish_voiceprint_review()["ready"] and canvas.voiceprint_decisions() == []
    assert "voice" not in canvas.catalog("events", role="refine")[0]


def test_doubtful_requirement_respects_write_bounds_and_previous_window_decisions_are_information():
    snapshot = _voice_snapshot("flag", None, doubtful=True)
    snapshot["write_bounds"] = (3000, 20_000)
    outside = ParseContextCanvas.from_refine(snapshot)
    assert outside.voiceprint_review()["doubtful_labels"]
    assert outside.finish_voiceprint_review()["ready"] and outside.voiceprint_decisions() == []
    snapshot["write_bounds"] = (0, 20_000)
    previous = {"event_ref": outside.items("events")[0][0], "decision": "keep", "reason": "Earlier window.",
                "evidence_type": "named_address", "final_speaker": "爱音"}
    snapshot["member_evidence"]["member"]["voiceprint"]["previous_decisions"] = [previous]
    current = ParseContextCanvas.from_refine(snapshot)
    reference = current.items("events")[0][0]
    assert reference != previous["event_ref"]
    assert current.get("evidence", "member")["voiceprint"]["previous_decisions"] == [previous]
    assert current.finish_voiceprint_review()["missing"] == [reference]


def test_unscored_label_metrics_stay_null_without_creating_a_requirement():
    snapshot = _voice_snapshot("flag", None)
    snapshot["member_contents"]["member"]["timeline"][0]["speaker"] = None
    line = snapshot["member_evidence"]["member"]["voiceprint"]["lines"][0]
    metrics = {name: None for name in (
        "label_score", "label_rank", "label_count", "label_margin", "label_ref_count", "label_ref_consistency",
        "label_ref_higher_count", "label_ref_higher_fraction",
    )}
    line.update(**metrics, understanding_speaker=None, label_doubtful=False, label_doubtful_basis=None)
    canvas = ParseContextCanvas.from_refine(snapshot)
    evidence = canvas.items("events")[0][1]["voice_evidence"]
    assert {key: evidence[key] for key in metrics} == metrics
    assert evidence["label_doubtful_basis"] is None
    assert canvas.voiceprint_review()["doubtful_labels"] == []
    assert canvas.finish_voiceprint_review()["ready"]
    assert canvas.catalog("events", role="refine")[0]["voice"]["label_rank"] is None


def test_doubtful_metadata_is_ignored_when_voice_mode_is_off():
    canvas = ParseContextCanvas.from_refine(_voice_snapshot("off", None, doubtful=True))
    before = canvas.snapshot()
    assert canvas.voiceprint_review() is None and canvas.voiceprint_review_passed()
    assert canvas.voiceprint_decisions() == [] and canvas.snapshot() == before
    assert "voice_evidence" not in canvas.items("events")[0][1]
    assert set(canvas.catalog("events", role="refine")[0]) == {"reference", "member_ref", "event_type"}


def test_label_tail_diagnostics_bind_review_and_export_without_changing_decision_policy():
    snapshot = _voice_snapshot("flag", None, doubtful=True)
    line = snapshot["member_evidence"]["member"]["voiceprint"]["lines"][0]
    line.update(label_ref_higher_count=5, label_ref_higher_fraction=1)
    original = deepcopy(snapshot)
    canvas = ParseContextCanvas.from_refine(snapshot)
    reference, value = canvas.items("events")[0]
    for evidence in (value["voice_evidence"], canvas.voiceprint_review()["doubtful_labels"][0]):
        assert evidence["label_ref_higher_count"] == 5 and evidence["label_ref_higher_fraction"] == 1
    with pytest.raises(AgentToolFeedbackError) as error:
        canvas.write(role="refine", batch_id="missing-evidence", expected_revision=canvas.revision, operations=[{
            "op": "voice_decision", "event_ref": reference, "decision": "keep", "reason": "Turn order.",
        }])
    assert error.value.code == "VOICEPRINT_KEEP_EVIDENCE_REQUIRED"
    canvas.write(role="refine", batch_id="unresolved", expected_revision=canvas.revision, operations=[
        {"op": "patch_event", "event_ref": reference, "fields": {"speaker": None}},
        {"op": "voice_decision", "event_ref": reference, "decision": "null",
         "evidence_type": "contradiction", "reason": "Two simultaneous speakers overlap in this target audio."},
    ])
    assert canvas.finish_voiceprint_review()["ready"]
    record = canvas.voiceprint_decisions()[0]
    assert record["label_ref_higher_count"] == 5 and record["label_ref_higher_fraction"] == 1
    assert record["label_ref_count"] == 5 and record["final_speaker"] is None
    assert snapshot == original


@pytest.mark.parametrize("basis", ["tail", "rank_margin_only_few_refs"])
def test_doubtful_basis_is_visible_in_catalog_and_persisted_in_decisions(basis):
    snapshot = _voice_snapshot("flag", None, doubtful=True)
    line = snapshot["member_evidence"]["member"]["voiceprint"]["lines"][0]
    reference_count = 8 if basis == "tail" else 5
    line.update(label_ref_count=reference_count, label_ref_higher_count=reference_count,
                label_ref_higher_fraction=1, label_doubtful_basis=basis)
    canvas = ParseContextCanvas.from_refine(snapshot)
    reference, value = canvas.items("events")[0]
    assert value["voice_evidence"]["label_doubtful_basis"] == basis
    assert canvas.voiceprint_review()["doubtful_labels"][0]["label_doubtful_basis"] == basis
    assert json.loads(canvas.checkpoint())["collections"]["events"][0]["voice"]["label_doubtful_basis"] == basis
    canvas.write(role="refine", batch_id="unresolved", expected_revision=canvas.revision, operations=[
        {"op": "patch_event", "event_ref": reference, "fields": {"speaker": None}},
        {"op": "voice_decision", "event_ref": reference, "decision": "null",
         "evidence_type": "contradiction", "reason": "Two simultaneous speakers overlap in this target audio."},
    ])
    assert canvas.finish_voiceprint_review()["ready"]
    record = canvas.voiceprint_decisions()[0]
    assert record["label_doubtful_basis"] == basis and record["final_speaker"] is None
    canvas.write(role="refine", batch_id="changed-audio", expected_revision=canvas.revision, operations=[{
        "op": "patch_event", "event_ref": reference, "fields": {"end_seconds": 2900},
    }])
    assert "voice" not in canvas.catalog("events", role="refine")[0]
    assert canvas.voiceprint_review()["doubtful_labels"] == []


@pytest.mark.parametrize("mode", ["flag", "prefill"])
@pytest.mark.parametrize("band,gap,doubtful,needs_evidence", [
    ("CONFLICT", 0.049999, False, False), ("CONFLICT", 0.05, False, True),
    ("CONFLICT", 0.050001, False, True), ("CONFLICT", 0.099999, False, True),
    ("CONFLICT", 0.10, False, True),
    ("CONFLICT", 0.100001, False, True), ("CONFLICT", None, False, False),
    ("ACCEPT", 0.01, False, True), ("ACCEPT", None, False, True),
    ("NO_VOICE", 0.10, True, True), (None, 0.10, True, True),
    ("NO_VOICE", 0.099999, True, True), (None, None, True, False),
    ("NO_VOICE", 0.049999, True, False), (None, 0.049999, True, False),
])
@pytest.mark.parametrize("evidence_type", [None, "named_address", "contradiction"])
def test_required_null_decision_checks_voice_lead_and_accept_band(mode, band, gap, doubtful, needs_evidence, evidence_type):
    snapshot = _voice_snapshot(mode, band, doubtful=doubtful)
    snapshot["member_evidence"]["member"]["voiceprint"]["lines"][0]["gap"] = gap
    original = deepcopy(snapshot)
    canvas = ParseContextCanvas.from_refine(snapshot)
    reference = canvas.items("events")[0][0]
    record = {"op": "voice_decision", "event_ref": reference, "decision": "null",
              "reason": "The target is sung over a second simultaneous voice."}
    if evidence_type is not None:
        record["evidence_type"] = evidence_type
    before = canvas.snapshot()
    operations = [{"op": "patch_event", "event_ref": reference, "fields": {"speaker": None}}, record]
    if needs_evidence and evidence_type != "contradiction":
        with pytest.raises(AgentToolFeedbackError) as error:
            canvas.write(role="refine", batch_id="abstain", expected_revision=canvas.revision, operations=operations)
        assert error.value.code == "VOICEPRINT_NULL_EVIDENCE_REQUIRED"
        assert "evidence_type=contradiction" in error.value.retry_hint and "reason" in error.value.retry_hint
        assert canvas.snapshot() == before
        assert canvas.finish_voiceprint_review()["missing"] == [reference]
        assert not canvas.voiceprint_review_passed()
    else:
        canvas.write(role="refine", batch_id="abstain", expected_revision=canvas.revision, operations=operations)
        assert canvas.finish_voiceprint_review()["ready"] and canvas.voiceprint_review_passed()
        exported = canvas.voiceprint_decisions()[0]
        assert exported["decision"] == "null" and exported["final_speaker"] is None
        assert exported["reason"] == record["reason"]
        if evidence_type is None:
            assert "evidence_type" not in exported
        else:
            assert exported["evidence_type"] == evidence_type
    assert snapshot == original
    assert canvas._initial["member"].model_dump(mode="json") == original["member_contents"]["member"]


@pytest.mark.parametrize("evidence_type", [None, "named_address", "contradiction"])
def test_voice_gate_rechecks_stored_null_evidence_and_new_evidence_invalidates_gate(evidence_type):
    canvas = ParseContextCanvas.from_refine(_voice_snapshot("flag", "CONFLICT"))
    reference = canvas.items("events")[0][0]

    def stored_record(draft):
        value = draft["events"][reference]
        value["event"]["speaker"] = None
        value["voice_decision"] = {"decision": "null", "reason": "A second simultaneous voice overlaps this audio."}
        if evidence_type is not None:
            value["voice_decision"]["evidence_type"] = evidence_type

    canvas.mutate(stored_record)
    status = canvas.finish_voiceprint_review()
    assert status["ready"] == (evidence_type == "contradiction")
    assert status["inconsistent"] == ([] if evidence_type == "contradiction" else [reference])
    assert canvas.voiceprint_review_passed() == (evidence_type == "contradiction")
    diagnostics = canvas.voiceprint_review_diagnostics()
    if evidence_type != "contradiction":
        assert diagnostics["events"][0]["problem"] == "null_contradiction_evidence_required"
        assert diagnostics["events"][0]["draft_speaker"] is None
        assert diagnostics["events"][0]["evidence_type"] == evidence_type
    else:
        assert diagnostics["events"] == [] and diagnostics["gate_accepted"]
    if evidence_type == "contradiction":
        canvas.write(role="refine", batch_id="new-observation", expected_revision=canvas.revision, operations=[{
            "op": "voice_decision", "event_ref": reference, "decision": "null", "evidence_type": "contradiction",
            "reason": "The inspected frames show another named person speaking this line.",
        }])
        assert canvas.voiceprint_review_status()["ready"] and not canvas.voiceprint_review_passed()
        assert canvas.finish_voiceprint_review()["ready"]


def test_contradiction_is_not_identity_evidence_for_doubtful_keep_at_write_or_gate():
    canvas = ParseContextCanvas.from_refine(_voice_snapshot("flag", None, doubtful=True))
    reference = canvas.items("events")[0][0]
    record = {"op": "voice_decision", "event_ref": reference, "decision": "keep",
              "evidence_type": "contradiction", "reason": "Another simultaneous voice overlaps the top candidate."}
    before = canvas.snapshot()
    with pytest.raises(AgentToolFeedbackError) as error:
        canvas.write(role="refine", batch_id="not-identity", expected_revision=canvas.revision, operations=[record])
    assert error.value.code == "VOICEPRINT_KEEP_EVIDENCE_REQUIRED" and canvas.snapshot() == before

    def stored_record(draft):
        draft["events"][reference]["voice_decision"] = {key: value for key, value in record.items()
                                                        if key not in {"op", "event_ref"}}

    canvas.mutate(stored_record)
    assert canvas.finish_voiceprint_review()["inconsistent"] == [reference]
    assert not canvas.voiceprint_review_passed()


@pytest.mark.parametrize("reason", ["", " \n\t", "x" * 301])
def test_null_contradiction_still_requires_a_nonblank_bounded_reason(reason):
    canvas = ParseContextCanvas.from_refine(_voice_snapshot("flag", "CONFLICT"))
    reference = canvas.items("events")[0][0]
    before = canvas.snapshot()
    with pytest.raises(ValidationError):
        canvas.write(role="refine", batch_id="invalid-reason", expected_revision=canvas.revision, operations=[{
            "op": "voice_decision", "event_ref": reference, "decision": "null",
            "evidence_type": "contradiction", "reason": reason,
        }])
    assert canvas.snapshot() == before


def test_new_canvas_binds_new_event_references_and_explicit_conflicts_in_prompt():
    from agent.video_parse_v2.prompts import build_refine_prompt

    snapshot = _voice_snapshot("flag", "CONFLICT")
    first, second = ParseContextCanvas.from_refine(snapshot), ParseContextCanvas.from_refine(snapshot)
    reference, value = second.items("events")[0]
    assert reference != first.items("events")[0][0]
    assert value["voice_evidence"]["event_ref"] == reference
    prompt = build_refine_prompt(start_sequence_no=0, chunk_count=1, canvas=second)
    review = json.loads(next(line for line in prompt.splitlines()
                             if line.startswith("Required voice review lists: ")).split(": ", 1)[1])
    assert review["conflicts"][0]["event_ref"] == reference
    assert "爱音" in prompt and "unchanged candidate ranking" in prompt
    assert json.loads(prompt.splitlines()[-1])["revision"] == second.revision


@pytest.mark.parametrize("error", [RuntimeError("failed"), TimeoutError("expired"), asyncio.CancelledError()])
def test_failed_refine_never_commits_prefilled_draft(error):
    runtime = SimpleNamespace(
        load_refine_input=AsyncMock(return_value=_voice_snapshot()),
        generate_interval_refinement=AsyncMock(side_effect=error),
        commit_interval_refinement=AsyncMock(), notify_interval_committed=AsyncMock(),
    )
    with pytest.raises(type(error)):
        asyncio.run(IntervalRefine(runtime=runtime).InvokeRefine("voice-interval"))
    runtime.commit_interval_refinement.assert_not_awaited()
    runtime.notify_interval_committed.assert_not_awaited()


def test_original_36_events_keep_every_unmentioned_event_after_two_corrections():
    fixture = json.loads((Path(__file__).parent / "fixtures/context_canvas_refine_36.json").read_text())
    canvas = ParseContextCanvas(scope="refine:original-36", members={
        f"member-{member['sequence_no']}": {
            "sequence_no": member["sequence_no"], "version": 1,
            "bounds": tuple(member["bounds"]), "content": member["content"],
        } for member in fixture["members"]
    })
    issued = {(value["member_ref"], value["source"]["position"]): reference
              for reference, value in canvas.items("events")}
    receipt = canvas.write(
        role="refine", batch_id="two-local-corrections", expected_revision=canvas.revision,
        operations=[{
            "op": "event_array", "event_ref": issued[(f"member-{correction['sequence_no']}", correction["position"])],
            "field": "participants", "action": "add", "values": correction["participants_add"],
        } for correction in fixture["corrections"]],
    )
    assert isinstance(canvas, ContextCanvas)
    assert len(receipt["results"]) == 2
    assert sum(len(canvas.result(f"member-{index}").timeline) for index in range(2)) == 36
    expected = {f"member-{member['sequence_no']}": deepcopy(member["content"]) for member in fixture["members"]}
    for correction in fixture["corrections"]:
        expected[f"member-{correction['sequence_no']}"]["timeline"][correction["position"]] = correction["expected_event"]
    for reference, content in expected.items():
        assert canvas.result(reference).model_dump(mode="json") == content
    assert [len(update.timeline) for update in canvas.interval_result().updates] == [17, 19]
    with pytest.raises(ValidationError):
        CanvasCompletion.model_validate({"complete": True, "timeline": []})


def test_invalid_later_operation_keeps_entire_batch_and_revision_unchanged():
    canvas = _canvas()
    before = canvas.snapshot()
    event_ref = canvas.items("events")[0][0]
    with pytest.raises(ValueError, match="outside"):
        canvas.write(role="refine", batch_id="invalid", expected_revision=canvas.revision, operations=[
            {"op": "patch_event", "event_ref": event_ref, "fields": {"description": "valid correction"}},
            {"op": "add_event", "member_ref": "outside", "key": "bad", "event": _result().timeline[0]},
        ])
    assert canvas.snapshot() == before

    with pytest.raises(ValueError, match="authorized"):
        canvas.write(role="refine", batch_id="invalid-time", expected_revision=canvas.revision, operations=[
            {"op": "patch_event", "event_ref": event_ref, "fields": {"end_seconds": 20_001}},
        ])
    assert canvas.snapshot() == before


def test_explicit_single_event_set_changes_classification_and_preserves_identity():
    canvas = _canvas()
    canvas.write(role="refine", batch_id="unrelated-event", expected_revision=canvas.revision, operations=[
        {"op": "add_event", "member_ref": "member", "key": "untouched", "event": {
            **_result("untouched event").timeline[0].model_dump(mode="json"),
            "start_seconds": 5000, "end_seconds": 7000,
        }},
    ])
    untouched_ref, untouched = canvas.items("events")[1]
    original_ref, original = canvas.items("events")[0]
    canvas.write(role="refine", batch_id="scene", expected_revision=canvas.revision, operations=[
        {"op": "set_event", "event_ref": original_ref,
         "event": {**original["event"], "event_type": "SCENE"}},
    ])
    scene = canvas.get("events", original_ref)
    canvas.write(role="refine", batch_id="dialogue", expected_revision=canvas.revision, operations=[
        {"op": "set_event", "event_ref": original_ref, "event": {
            **scene["event"], "event_type": "DIALOGUE", "speaker": "爱音",
            "text_variants": [{"language": "ja", "role": "ORIGINAL", "text": "こんにちは"}],
        }},
    ])
    assert canvas.get("events", original_ref)["source"] == original["source"]
    assert canvas.get("events", untouched_ref) == untouched
    assert len(canvas.result("member").timeline) == 2
    assert canvas.result("member").timeline[0].event_type == "DIALOGUE"
    before = canvas.snapshot()
    with pytest.raises(ValueError):
        canvas.write(role="refine", batch_id="invalid-complete-event", expected_revision=canvas.revision, operations=[
            {"op": "patch_event", "event_ref": original_ref, "fields": {"description": "must roll back"}},
            {"op": "set_event", "event_ref": original_ref, "event": {**scene["event"], "event_type": "DIALOGUE"}},
        ])
    assert canvas.snapshot() == before
    canvas.write(role="refine", batch_id="back-to-scene", expected_revision=canvas.revision, operations=[
        {"op": "set_event", "event_ref": original_ref, "event": scene["event"]},
    ])
    assert canvas.result("member").timeline[0].event_type == "SCENE"
    assert canvas.get("events", original_ref)["source"] == original["source"]


def test_replayed_batch_and_business_key_do_not_append_duplicate_events():
    canvas = _canvas()
    revision = canvas.revision
    operations = [{"op": "add_event", "member_ref": "member", "key": "missed-scene", "event": _result("new event").timeline[0]}]
    first = canvas.write(role="refine", batch_id="append", expected_revision=revision, operations=operations)
    replay = canvas.write(role="refine", batch_id="append", expected_revision=revision, operations=operations)
    assert replay == {**first, "replayed": True}
    canvas.write(role="refine", batch_id="same-business-key", expected_revision=canvas.revision, operations=operations)
    assert len(canvas.result("member").timeline) == 2
    with pytest.raises(CanvasRevisionConflict):
        canvas.write(role="refine", batch_id="stale", expected_revision=revision, operations=operations)


def test_original_evidence_and_state_remain_readable_after_checkpoint_and_edits():
    canvas = _canvas()
    tools = {tool.name: tool for tool in build_context_canvas_tools(
        canvas, role="refine", read_collections=("members", "events", "evidence", "background"),
        write_args_schema=ParseCanvasWrite, maximum_result_bytes=2048,
        maximum_read_chars=512, maximum_catalog_items=10,
    )}
    navigation = json.loads(canvas.checkpoint())
    assert "original speech" not in str(navigation)
    read = tools["get_context_canvas"].execute(collection="evidence", reference="member", section="asr")
    assert read.success and "original speech" in read.model_content()
    before = canvas.get("evidence", "member")
    copy = canvas.get("evidence", "member")
    copy["asr"]["transcript"].clear()
    event_ref = canvas.items("events")[0][0]
    canvas.write(role="refine", batch_id="description", expected_revision=canvas.revision, operations=[
        {"op": "patch_event", "event_ref": event_ref, "fields": {"description": "corrected"}},
    ])
    assert canvas.get("evidence", "member") == before
    with pytest.raises(ValueError):
        canvas.put("evidence", "member", {"asr": None})
    assert canvas.get("evidence", "member") == before


@pytest.mark.parametrize("origin", ["initial", "added"])
def test_explicit_remove_only_deletes_the_named_editable_event(origin):
    canvas = _canvas()
    initial_ref = canvas.items("events")[0][0]
    evidence = canvas.get("evidence", "member")
    event_ref = initial_ref
    if origin == "added":
        receipt = canvas.write(role="refine", batch_id="add", expected_revision=canvas.revision, operations=[
            {"op": "add_event", "member_ref": "member", "key": "duplicate",
             "event": _result("duplicate event").timeline[0]},
        ])
        event_ref = receipt["results"][0]["reference"]
    expected = [value["event"] for reference, value in canvas.items("events") if reference != event_ref]

    canvas.write(role="refine", batch_id="remove", expected_revision=canvas.revision, operations=[
        {"op": "remove_event", "event_ref": event_ref},
    ])

    assert canvas.result("member").model_dump(mode="json")["timeline"] == expected
    assert event_ref not in {item["reference"] for item in canvas.catalog("events", role="refine")}
    with pytest.raises(ValueError, match="Unknown Canvas reference"):
        canvas.get("events", event_ref)
    assert canvas.get("evidence", "member") == evidence
    if origin == "initial":
        assert canvas.interval_result().updates[0].timeline == []
    else:
        assert canvas.interval_result().updates == []


def test_remove_batch_replay_is_idempotent_but_new_missing_reference_fails():
    canvas = _canvas()
    reference = canvas.items("events")[0][0]
    revision = canvas.revision
    operations = [{"op": "remove_event", "event_ref": reference}]
    first = canvas.write(role="refine", batch_id="remove", expected_revision=revision, operations=operations)
    after = canvas.snapshot()

    replay = canvas.write(role="refine", batch_id="remove", expected_revision=revision, operations=operations)

    assert replay == {**first, "replayed": True}
    assert canvas.snapshot() == after
    with pytest.raises(ValueError, match="unknown event reference"):
        canvas.write(role="refine", batch_id="another-remove", expected_revision=canvas.revision, operations=operations)
    assert canvas.snapshot() == after


def test_remove_and_patch_deleted_event_rolls_back_whole_batch():
    canvas = _canvas()
    reference = canvas.items("events")[0][0]
    before = canvas.snapshot()

    with pytest.raises(ValueError, match="unknown event reference"):
        canvas.write(role="refine", batch_id="remove-then-patch", expected_revision=canvas.revision, operations=[
            {"op": "remove_event", "event_ref": reference},
            {"op": "patch_event", "event_ref": reference, "fields": {"description": "must not commit"}},
        ])

    assert canvas.snapshot() == before


def test_surviving_initial_event_keeps_its_source_identity():
    canvas = _canvas()
    draft = canvas.snapshot().collections
    reference = next(iter(draft["events"]))
    draft["events"][reference]["source"]["position"] = 99

    with pytest.raises(ValueError, match="event source identity changed"):
        canvas._validate_draft(draft)


def test_role_scope_and_unknown_operations_cannot_mutate_other_events():
    canvas, other = _canvas(), _canvas()
    before = canvas.snapshot()
    with pytest.raises(ValueError):
        canvas.write(role="prefix", batch_id="wrong-role", expected_revision=canvas.revision, operations=[
            {"op": "persons", "member_ref": "member", "action": "set", "values": []},
        ])
    with pytest.raises(ValueError):
        canvas.write(role="refine", batch_id="other-execution", expected_revision=canvas.revision, operations=[
            {"op": "patch_event", "event_ref": other.items("events")[0][0], "fields": {"description": "wrong window"}},
        ])
    with pytest.raises(ValueError, match="unknown event reference"):
        canvas.write(role="refine", batch_id="remove-other-execution", expected_revision=canvas.revision, operations=[
            {"op": "remove_event", "event_ref": other.items("events")[0][0]},
        ])
    for op in ("delete_event", "clear_timeline"):
        with pytest.raises(ValidationError):
            canvas.write(role="refine", batch_id=op, expected_revision=canvas.revision, operations=[{"op": op}])
    assert canvas.snapshot() == before


def test_history_scope_preserves_existing_events_but_cannot_expand_timing_writes():
    canvas = ParseContextCanvas(scope="refine:next-window", write_bounds=(10_000, 30_000), members={
        "member-1": {"sequence_no": 1, "version": 2, "bounds": (0, 30_000), "scope": [0, 1, 2],
                     "content": _result("earlier window fact")},
        "member-2": {"sequence_no": 2, "version": 1, "bounds": (20_000, 30_000), "scope": [2],
                     "content": _result("current window fact", offset_ms=20_000)},
    })
    first_ref, first = canvas.items("events")[0]
    second_ref = canvas.items("events")[1][0]
    canvas.write(role="refine", batch_id="in-window-correction", expected_revision=canvas.revision, operations=[
        {"op": "patch_event", "event_ref": second_ref, "fields": {"description": "reviewed current window fact"}},
    ])
    assert canvas.get("events", first_ref) == first
    before = canvas.snapshot()
    for batch_id, operation in (
        ("out-of-window-add", {"op": "add_event", "member_ref": "member-1", "key": "outside", "event": _result().timeline[0]}),
        ("out-of-window-time", {"op": "patch_event", "event_ref": first_ref, "fields": {"start_seconds": 2000}}),
    ):
        with pytest.raises(ValueError, match="authorized write window"):
            canvas.write(role="refine", batch_id=batch_id, expected_revision=canvas.revision, operations=[operation])
        assert canvas.snapshot() == before


@pytest.mark.parametrize("field,value", [
    ("participants", {"language": "ja", "role": "ORIGINAL", "text": "speech"}),
    ("visible_persons", ""), ("text_variants", "plain string"),
])
def test_array_remove_rejects_incompatible_element_types(field, value):
    canvas = _canvas()
    before = canvas.snapshot()
    with pytest.raises(ValidationError):
        canvas.write(role="refine", batch_id="invalid-element", expected_revision=canvas.revision, operations=[
            {"op": "event_array", "event_ref": canvas.items("events")[0][0],
             "field": field, "action": "remove", "values": [value]},
        ])
    assert canvas.snapshot() == before


def test_conflict_refreshes_only_refinement_and_retains_intervening_events():
    original = _result()
    changed = VideoParseResult(persons=original.persons, timeline=[*original.timeline, *_result("other window addition").timeline])
    base = {
        "interval_parse_task_id": "interval", "video_parse_task_id": "video", "inputs": _inputs(), "lease": _lease(),
        "start_sequence_no": 0, "chunk_count": 1, "member_sequence_nos": {"member": 0},
        "member_bounds": {"member": (0, 20_000)}, "member_scopes": {"member": [0]},
    }
    snapshots = [
        {**base, "member_contents": {"member": original}, "member_versions": {"member": 1}},
        {**base, "member_contents": {"member": changed}, "member_versions": {"member": 2}},
    ]

    async def refine(snapshot, *, canvas, prompt):
        reference = canvas.items("events")[0][0]
        canvas.write(role="refine", batch_id="correction", expected_revision=canvas.revision, operations=[
            {"op": "patch_event", "event_ref": reference, "fields": {"description": "locally corrected"}},
        ])
        return CanvasCompletion(complete=True)

    runtime = SimpleNamespace(
        load_refine_input=AsyncMock(side_effect=snapshots), generate_interval_refinement=AsyncMock(side_effect=refine),
        commit_interval_refinement=AsyncMock(side_effect=[False, True]), notify_interval_committed=AsyncMock(),
        transcribe_chunk=AsyncMock(), understand_chunk=AsyncMock(),
    )
    asyncio.run(IntervalRefine(runtime=runtime).InvokeRefine("interval"))
    assert runtime.generate_interval_refinement.await_count == 2
    assert len(runtime.commit_interval_refinement.await_args.args[1].updates[0].timeline) == 2
    runtime.transcribe_chunk.assert_not_awaited()
    runtime.understand_chunk.assert_not_awaited()
    runtime.notify_interval_committed.assert_awaited_once()


@pytest.mark.parametrize("correct_events", [False, True], ids=["no-changes", "two-corrections"])
def test_refine_batches_original_event_details_and_commits_once(correct_events):
    fixture = json.loads((Path(__file__).parent / "fixtures/context_canvas_refine_36.json").read_text())
    members = {f"member-{member['sequence_no']}": member for member in fixture["members"]}
    snapshot = {
        "interval_parse_task_id": "interval", "video_parse_task_id": "video",
        "inputs": _inputs(), "lease": _lease(), "start_sequence_no": 0, "chunk_count": 2,
        "member_sequence_nos": {ref: member["sequence_no"] for ref, member in members.items()},
        "member_bounds": {ref: tuple(member["bounds"]) for ref, member in members.items()},
        "member_scopes": {ref: [member["sequence_no"]] for ref, member in members.items()},
        "member_contents": {ref: member["content"] for ref, member in members.items()},
        "member_versions": {ref: 1 for ref in members},
    }
    calls = []

    async def fake_provider(_snapshot, *, canvas, prompt):
        assert "references=[...]" in prompt and "Skip overview" in prompt
        assert "remaining_references" in prompt and "returned items are complete" in prompt
        navigation = json.loads(prompt.splitlines()[-1])
        references = [entry["reference"] for entry in navigation["collections"]["events"]]
        tools = {tool.name: tool for tool in build_context_canvas_tools(
            canvas, role="refine", read_collections=("members", "events"),
            write_args_schema=ParseCanvasWrite, maximum_result_bytes=32_000,
            maximum_read_chars=12_000, maximum_catalog_items=100, on_call=calls.append,
        )}
        remaining, reviewed = references, {}
        while True:
            raw = tools["get_context_canvas"].invoke({
                "collection": "events", "references": remaining,
            })
            page = json.loads(raw)
            assert "error" not in page and len(raw.encode("utf-8")) <= 32_000
            assert page["revision"] == canvas.revision
            assert "next_cursor" not in page
            for item in page["items"]:
                body = json.loads(item["content"])
                source = body["source"]
                assert body["event"] == members[body["member_ref"]]["content"]["timeline"][source["position"]]
                assert item["reference"] not in reviewed
                reviewed[item["reference"]] = body
            assert page["items"]
            remaining = page["remaining_references"]
            assert len(calls) < len(references)
            if not remaining:
                break
        assert len(reviewed) == 36
        if correct_events:
            issued = {(body["member_ref"], body["source"]["position"]): ref for ref, body in reviewed.items()}
            receipt = json.loads(tools["write_context_canvas"].invoke({
                "batch_id": "two-local-corrections", "expected_revision": page["revision"],
                "operations": [{
                    "op": "event_array", "event_ref": issued[(f"member-{change['sequence_no']}", change["position"])],
                    "field": "participants", "action": "add", "values": change["participants_add"],
                } for change in fixture["corrections"]],
            }))
            assert len(receipt["results"]) == 2
        return CanvasCompletion(complete=True)

    runtime = SimpleNamespace(
        load_refine_input=AsyncMock(return_value=snapshot),
        generate_interval_refinement=AsyncMock(side_effect=fake_provider),
        commit_interval_refinement=AsyncMock(return_value=True), notify_interval_committed=AsyncMock(),
    )
    asyncio.run(IntervalRefine(runtime=runtime).InvokeRefine("interval"))
    runtime.generate_interval_refinement.assert_awaited_once()
    runtime.commit_interval_refinement.assert_awaited_once()
    runtime.notify_interval_committed.assert_awaited_once()
    result = runtime.commit_interval_refinement.await_args.args[1]
    if not correct_events:
        assert calls.count("write_context_canvas") == 0 and result.updates == []
    else:
        expected = {ref: deepcopy(member["content"]) for ref, member in members.items()}
        for change in fixture["corrections"]:
            expected[f"member-{change['sequence_no']}"]["timeline"][change["position"]] = change["expected_event"]
        assert calls.count("write_context_canvas") == 1
        for update in result.updates:
            assert [event.model_dump(mode="json") for event in update.timeline] == expected[f"member-{update.sequence_no}"]["timeline"]
