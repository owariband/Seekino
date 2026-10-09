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


def test_role_scope_and_operation_types_cannot_delete_or_clear_events():
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
    asyncio.run(IntervalRefine(runtime=runtime).run("interval"))
    assert runtime.generate_interval_refinement.await_count == 2
    assert len(runtime.commit_interval_refinement.await_args.args[1].updates[0].timeline) == 2
    runtime.transcribe_chunk.assert_not_awaited()
    runtime.understand_chunk.assert_not_awaited()
    runtime.notify_interval_committed.assert_awaited_once()
