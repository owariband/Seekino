from copy import deepcopy

import pytest
from pydantic import ValidationError

from engine.parsev2.edit_reference import ReferenceChunk, StyleCard


def make_chunk():
    return {"observations": [{
        "segment": {"segment_id": "seg-0000100", "start_ms": 100, "end_ms": 200, "first_frame": 3,
                    "frame_ms": [100, 133, 167], "fine_starts_ms": [167], "flashes_ms": [], "palette": []},
        "note": {"name": "切到远景", "summary": "切到远景", "role": "INTRO",
                 "look": {"ground": "单色", "subject_treatment": "剪影", "texture": None, "type_treatment": None},
                 "motion": {"steps": [{"start_ms": 100, "end_ms": 167, "framing": "CU", "enter": "NONE", "subject": "脸"},
                                      {"start_ms": 167, "end_ms": 200, "framing": "WIDE", "enter": "CUT", "subject": "剪影"}],
                            "easing": None, "camera": "STATIC"}, "signals": [], "slots": []},
    }]}


def test_steps_stay_inside_source_and_chunk_owns_the_start():
    data = make_chunk()
    result = ReferenceChunk.model_validate(data)
    assert result.validate_owner(0, 150, 200) == result  # spanning a chunk boundary preserves the action
    with pytest.raises(ValueError, match="another chunk"):
        result.validate_owner(150, 300, 300)
    with pytest.raises(ValueError, match="exceeds"):
        result.validate_owner(0, 150, 199)
    note = data["observations"][0]["note"]
    note["motion"]["steps"][0]["end_ms"] = 133
    assert ReferenceChunk.model_validate(data).observations[0].note.motion.steps[0].end_ms == 133
    note["motion"]["steps"][0]["start_ms"] = 99
    with pytest.raises(ValidationError, match="source interval"):
        ReferenceChunk.model_validate(data)
    assert ReferenceChunk(observations=[]).validate_owner(0, 100, 100).observations == []


def test_style_card_only_groups_existing_ids_once():
    group = {"technique_key": "硬切", "description": "切换景别", "segment_ids": ["seg-0000100"]}
    card = StyleCard(summary="构图变化", techniques=[group])
    assert card.assignments(["seg-0000100"]) == {"seg-0000100": "硬切"}
    with pytest.raises(ValueError, match="exactly"):
        card.assignments(["seg-0000200"])
    duplicate = deepcopy(group)
    duplicate["technique_key"] = "换景"
    with pytest.raises(ValueError, match="duplicate segment"):
        StyleCard(summary="构图变化", techniques=[group, duplicate]).assignments(["seg-0000100"])


def test_native_changes_preserve_old_observations_and_require_all_frame_times():
    data = make_chunk()
    assert "frame_delta" not in ReferenceChunk.model_validate(data).model_dump()["observations"][0]["segment"]
    data["observations"][0]["segment"]["frame_delta"] = [0.0, 1.0]
    with pytest.raises(ValidationError, match="each frame"):
        ReferenceChunk.model_validate(data)


def test_frozen_prompts_and_whole_film_anchors(tmp_path):
    import asyncio
    import hashlib
    import json
    from pathlib import Path

    from PIL import Image
    from agent.video_parse_v2 import edit_reference as harness
    from engine.parsev2.edit_reference import ReferenceRefinement

    expected_keys = {"pick": "729f32ef4a2e830f8b195134be31b1ceee1e3e4d85377c21436cd843361c6b91",
                     "refine": "2c0a0e49e6a557879ea4a645d095c380ebc22a925301e6edbdd0abc0df7f8028"}
    for name, digest in expected_keys.items():
        data = Path(harness.__file__).with_name(f"edit_reference_{name}.txt").read_bytes()
        assert hashlib.sha256(data).hexdigest() == digest

    chunk = ReferenceChunk.model_validate(make_chunk())
    source, = chunk.observations
    image = tmp_path / "frame.jpg"
    Image.new("RGB", (16, 16)).save(image)
    async def pick(inputs, schema):
        assert inputs[0].text == harness._PICK_PROMPT.format(start_ms=100, end_ms=200, fps="30",
                                                            fine_starts_ms=[167], flashes_ms=[])
        return schema.model_validate(source.note.model_dump())
    observed = asyncio.run(harness.observe_segments([source.segment],
        {ms: image for ms in source.segment.frame_ms}, pick, 1))
    assert observed.model_dump() == chunk.model_dump()

    ident = source.segment.segment_id
    signal = {"segment_id": ident, "at_ms": 167, "detail": "wide shot"}
    answer = {"members": [{"sequence_no": 0, "observations": [
        {"segment_id": ident, "technique_key": "cut", "name": "wide cut", "summary": "close to wide",
         "role": "INTRO", "camera": "STATIC"}]}],
        "style_card": {"scope": "opening", "signals": [{"name": "cut", "description": "framing changes",
                                                        "examples": [signal]}]}}
    async def close_root(inputs, schema):
        assert inputs[0].text == harness._REFINE_PROMPT
        packet = json.loads(inputs[1].text)
        assert packet["observations"][0]["sequence_no"] == 1
        return schema.model_validate_json(json.dumps(answer)[:-1])
    refined = asyncio.run(harness.consolidate({0: ReferenceChunk(observations=[]), 1: observed}, close_root, 1))
    assert [(m.sequence_no, len(m.observations)) for m in refined.members] == [(0, 0), (1, 1)]
    update = refined.members[1].observations[0].apply(source)
    assert update.technique_key == "cut" and update.note.name == "wide cut"
    assert update.segment == source.segment and update.note.motion.steps == source.note.motion.steps
    # Anchors may use any source-clock position in the segment, not only a candidate.
    signal["at_ms"] = 150
    assert ReferenceRefinement.model_validate(answer).for_sources({1: observed}).style_card.signals
    signal["at_ms"] = 200  # the exclusive upper bound is still outside the source
    with pytest.raises(ValueError, match="own segment"):
        ReferenceRefinement.model_validate(answer).for_sources({1: observed})
    signal["at_ms"] = 167
    # A repeated update is deterministic: the last occurrence wins.
    duplicate = {**answer["members"][0]["observations"][0], "technique_key": "dissolve", "name": "第二版"}
    answer["members"][0]["observations"].append(duplicate)
    refined = ReferenceRefinement.model_validate(answer).for_sources({1: observed})
    assert refined.members[0].observations[0].technique_key == "dissolve"
    # Omitting the source update preserves the original observation.
    answer["members"] = []
    preserved = ReferenceRefinement.model_validate(answer).for_sources({1: observed})
    assert preserved.members[0].observations[0].apply(source) == source
    # A returned update still has to be structurally complete.
    answer["members"] = [{"sequence_no": 1, "observations": [{**duplicate, "technique_key": "cut"}]}]
    answer["members"][0]["observations"][0].pop("summary")
    with pytest.raises(ValidationError):
        asyncio.run(harness.consolidate({1: observed}, close_root, 1))
