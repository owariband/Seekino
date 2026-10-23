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


def _voice_snapshot(mode="prefill", band="ACCEPT", *, source=None):
    event = {
        **_result().timeline[0].model_dump(mode="json"),
        "event_type": "DIALOGUE", "speaker": "爱音",
        "participants": ["爱音", "立希", "睦"],
        "text_variants": [{"language": "ja", "role": "ORIGINAL", "text": "声の証拠"}],
    }
    if source:
        event["speaker_source"] = source
    return {
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
