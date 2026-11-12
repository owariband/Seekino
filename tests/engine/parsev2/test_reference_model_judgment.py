"""Frozen model responses and source-ownership checks for editing references."""

import asyncio
import json
from copy import deepcopy
from pathlib import Path

import pytest
from PIL import Image
from pydantic import ValidationError

from agent.video_parse_v2 import edit_reference as harness
from engine.parsev2.edit_reference import (
    AnchoredStyleCard, PickNote, ReferenceChunk, ReferenceRefinement, ReferenceSegment, StyleCard,
    TechniqueObservation,
)

FIXTURE = Path(__file__).with_name("fixtures") / "reference_model_outputs.json"
CASES = json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"]


def _step(start_ms, end_ms, *, framing="CU", enter="CUT", subject="人物"):
    return {"start_ms": start_ms, "end_ms": end_ms, "framing": framing, "enter": enter, "subject": subject}


def _source(segment_id, start_ms, end_ms, *, name, technique_key=None, role="INTRO", camera="STATIC", steps=None):
    frames = [start_ms, start_ms + 33, start_ms + 67]
    return {
        "segment": {"segment_id": segment_id, "start_ms": start_ms, "end_ms": end_ms, "first_frame": 3,
                    "frame_ms": frames, "fine_starts_ms": [frames[-1]], "flashes_ms": [], "palette": []},
        "technique_key": technique_key,
        "note": {"name": name, "summary": f"{name}摘要", "role": role,
                 "look": {"ground": "单色", "subject_treatment": "剪影", "texture": None, "type_treatment": None},
                 "slots": [], "signals": [],
                 "motion": {"steps": [_step(start_ms, end_ms)] if steps is None else steps,
                            "easing": None, "camera": camera}},
    }


def _update(segment_id, *, technique_key="cut", name="更新", summary="更新摘要", role="INTRO", camera="STATIC"):
    return {"segment_id": segment_id, "technique_key": technique_key, "name": name, "summary": summary,
            "role": role, "camera": camera}


def _card(segment_id="seg-0000100", at_ms=150):
    return AnchoredStyleCard.model_validate({"scope": "测试", "signals": [{
        "name": "硬切", "description": "景别变化",
        "examples": [{"segment_id": segment_id, "at_ms": at_ms, "detail": "此处的变化"}]}]})


def test_fixture_keeps_all_frozen_failures():
    assert len(CASES) == 12
    assert sum(case["failureKind"] == "detector_boundary" for case in CASES) == 9
    assert sum(case["failureKind"] == "missing_root_delimiter" for case in CASES) == 3


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["caseId"])
def test_frozen_model_outputs_are_accepted_without_rewriting(case, tmp_path):
    segment = ReferenceSegment.model_validate(case["segment"])
    expected = json.loads(case["rawResponse"] + ("}" if case["failureKind"] == "missing_root_delimiter" else ""))
    image = tmp_path / "frame.jpg"
    Image.new("RGB", (16, 16), "#333333").save(image)

    async def generate(inputs, schema):
        return schema.model_validate_json(case["rawResponse"])

    observed = asyncio.run(harness.observe_segments(
        [segment], {stamp: image for stamp in segment.frame_ms}, generate, 1))
    assert len(observed.observations) == 1
    note = observed.observations[0].note
    assert note.name == expected["name"] and note.summary == expected["summary"]
    assert note.role == expected["role"] and note.look.model_dump() == expected["look"]
    assert [slot if isinstance(slot, str) else slot.model_dump() for slot in note.slots] == expected["slots"]
    assert note.signals == expected["signals"]
    assert [(step.start_ms, step.end_ms) for step in note.motion.steps] == [
        (step["start_ms"], step["end_ms"]) for step in expected["motion"]["steps"]]
    assert [step.model_dump() for step in note.motion.steps] == expected["motion"]["steps"]


def test_actions_allow_no_candidates_gaps_overlap_and_unsorted_steps():
    data = _source("seg-0000100", 100, 200, name="多步动作",
                   steps=[_step(120, 150), _step(175, 195), _step(140, 180)])
    data["segment"]["fine_starts_ms"] = []
    observation = TechniqueObservation.model_validate(data)
    assert [(step.start_ms, step.end_ms) for step in observation.note.motion.steps] == [
        (120, 150), (175, 195), (140, 180)]
    empty = _source("seg-0000100", 100, 200, name="无步骤", steps=[])
    assert TechniqueObservation.model_validate(empty).note.motion.steps == []


@pytest.mark.parametrize("steps", [
    [_step(99, 150)], [_step(150, 201)], [_step(150, 150)], [_step(170, 150)],
], ids=["before-start", "after-end", "zero-length", "reversed"])
def test_steps_outside_source_or_reversed_are_rejected(steps):
    with pytest.raises(ValidationError, match="source interval"):
        TechniqueObservation.model_validate(_source("seg-0000100", 100, 200, name="越界", steps=steps))


@pytest.mark.parametrize("at_ms", [100, 150, 199])
def test_card_anchor_accepts_any_position_inside_its_source(at_ms):
    observation = TechniqueObservation.model_validate(_source("seg-0000100", 100, 200, name="锚点"))
    card = _card(at_ms=at_ms)
    assert card.validate_sources([observation]) == card


def test_card_allows_empty_signals_and_rejects_foreign_or_out_of_range_anchors():
    observation = TechniqueObservation.model_validate(_source("seg-0000100", 100, 200, name="锚点"))
    empty = AnchoredStyleCard(scope="无信号", signals=[])
    assert empty.validate_sources([observation]) == empty
    assert empty.validate_sources([]) == empty
    for at_ms in (99, 200):
        with pytest.raises(ValueError, match="own segment"):
            _card(at_ms=at_ms).validate_sources([observation])
    with pytest.raises(ValueError, match="own segment"):
        _card(segment_id="seg-0000200").validate_sources([observation])
    with pytest.raises(ValueError, match="own segment"):
        _card().validate_sources([])


def test_refine_applies_partial_and_duplicate_updates_in_source_order():
    first = TechniqueObservation.model_validate(
        _source("seg-0000100", 100, 200, name="原始一", technique_key=None))
    second = TechniqueObservation.model_validate(
        _source("seg-0000200", 200, 300, name="原始二", technique_key="wipe", camera="PAN_RIGHT"))
    sources = {0: ReferenceChunk(observations=[first]), 1: ReferenceChunk(observations=[second])}
    draft = ReferenceRefinement.model_validate({
        "members": [
            {"sequence_no": 0, "observations": [
                _update("seg-0000100", name="更新一"), _update("seg-0000100", name="最终一")]},
            {"sequence_no": 1, "observations": []},
        ],
        "style_card": {"scope": "测试", "signals": []},
    })
    refined = draft.for_sources(sources)
    assert [member.sequence_no for member in refined.members] == [0, 1]
    assert [[note.segment_id for note in member.observations] for member in refined.members] == [
        ["seg-0000100"], ["seg-0000200"]]
    assert refined.members[0].observations[0].name == "最终一"
    preserved = refined.members[1].observations[0]
    assert (preserved.technique_key, preserved.name, preserved.summary, preserved.role, preserved.camera) == (
        "wipe", "原始二", "原始二摘要", "INTRO", "PAN_RIGHT")

    updates = {note.segment_id: note for member in refined.members for note in member.observations}
    committed = {seq: ReferenceChunk(observations=[updates[o.segment.segment_id].apply(o) for o in chunk.observations])
                 for seq, chunk in sources.items()}
    assert committed[1].observations[0] == second
    published = {o.segment.segment_id: o for chunk in committed.values() for o in chunk.observations}
    assert set(updates) == set(published)
    # The persisted complete set passes the existing Server apply/read check.
    assert all(updates[segment_id].apply(observation) == observation
               for segment_id, observation in published.items())
    assert refined.style_card.validate_sources(published.values()) == refined.style_card


def test_refine_rejects_unknown_source_ids():
    observation = TechniqueObservation.model_validate(_source("seg-0000100", 100, 200, name="来源"))
    draft = ReferenceRefinement.model_validate({
        "members": [{"sequence_no": 0, "observations": [_update("seg-0000200")]}],
        "style_card": {"scope": "测试", "signals": []},
    })
    with pytest.raises(ValueError, match="not observed"):
        draft.for_sources({0: ReferenceChunk(observations=[observation])})


def test_descriptive_values_are_not_narrowed_or_rewritten():
    long_summary = "画面" * 400
    slots = ["纯文字槽位", {"name": "素材", "kind": "video", "requirement": "替换"}]
    signals = [f"描述信号{i}" for i in range(9)]
    data = _source("seg-0000100", 100, 200, name="长" * 301,
                   steps=[_step(100, 200, framing="DOLLY_WHIP", enter="TELEPORT")])
    data["note"].update({"summary": long_summary, "role": "BREATH", "slots": slots, "signals": signals})
    data["note"]["look"]["ground"] = "背景" * 350
    note = PickNote.model_validate(data["note"])
    assert note.summary == long_summary and note.name == "长" * 301 and note.role == "BREATH"
    assert note.look.ground == "背景" * 350
    assert note.motion.steps[0].framing == "DOLLY_WHIP" and note.motion.steps[0].enter == "TELEPORT"
    assert note.slots[0] == "纯文字槽位" and note.slots[1].kind == "video"
    assert note.signals == signals

    observation = TechniqueObservation.model_validate(data)
    examples = [{"segment_id": "seg-0000100", "at_ms": at_ms, "detail": f"位置{at_ms}"}
                for at_ms in range(100, 109)]
    card = AnchoredStyleCard.model_validate({"scope": "多信号", "signals": [
        {"name": f"手法{i}", "description": "描述", "examples": examples} for i in range(9)]})
    assert len(card.signals) == 9 and len(card.signals[0].examples) == 9
    assert card.validate_sources([observation]) == card


def test_recovery_accepts_only_a_missing_final_root_brace(tmp_path):
    data = _source("seg-0000100", 100, 200, name="补尾")
    segment = ReferenceSegment.model_validate(data["segment"])
    image = tmp_path / "frame.jpg"
    Image.new("RGB", (16, 16), "#333333").save(image)
    frames = {stamp: image for stamp in segment.frame_ms}
    note = PickNote.model_validate(data["note"])
    complete = note.model_dump_json()

    async def missing_root(inputs, schema):
        return schema.model_validate_json(complete[:-1])

    observed = asyncio.run(harness.observe_segments([segment], frames, missing_root, 1))
    assert observed.observations[0].note == note

    async def truncated(inputs, schema):
        return schema.model_validate_json(complete[:-20])

    with pytest.raises(ValidationError):
        asyncio.run(harness.observe_segments([segment], frames, truncated, 1))

    incomplete = deepcopy(data["note"])
    incomplete.pop("summary")

    async def missing_field(inputs, schema):
        return schema.model_validate_json(json.dumps(incomplete))

    with pytest.raises(ValidationError):
        asyncio.run(harness.observe_segments([segment], frames, missing_field, 1))


def test_source_volume_and_legacy_card_contracts_remain():
    data = _source("seg-0000100", 100, 200, name="来源")
    with pytest.raises(ValidationError):
        ReferenceChunk.model_validate({"observations": [data] * 9})
    invalid = deepcopy(data["segment"])
    invalid["frame_ms"][0] = 99
    with pytest.raises(ValidationError, match="identity"):
        ReferenceSegment.model_validate(invalid)

    group = {"technique_key": "硬切", "description": "切换景别", "segment_ids": ["seg-0000100"]}
    card = StyleCard(summary="构图变化", techniques=[group])
    assert card.assignments(["seg-0000100"]) == {"seg-0000100": "硬切"}
    with pytest.raises(ValueError, match="exactly"):
        card.assignments(["seg-0000200"])
    with pytest.raises(ValidationError):
        ReferenceRefinement.model_validate({
            "members": [{"sequence_no": i, "observations": []} for i in range(64)],
            "style_card": {"scope": "测试", "signals": []},
        })
