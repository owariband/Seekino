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


def test_steps_tile_source_and_chunk_owns_the_start():
    data = make_chunk()
    result = ReferenceChunk.model_validate(data)
    assert result.validate_owner(0, 150, 200) == result  # spanning a chunk boundary preserves the action
    with pytest.raises(ValueError, match="another chunk"):
        result.validate_owner(150, 300, 300)
    with pytest.raises(ValueError, match="exceeds"):
        result.validate_owner(0, 150, 199)
    note = data["observations"][0]["note"]
    note["motion"]["steps"][0]["end_ms"] = 133
    with pytest.raises(ValidationError, match="tile"):
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
