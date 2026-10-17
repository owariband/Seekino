from __future__ import annotations

from copy import deepcopy
from types import MappingProxyType

import pytest

from engine.parsev2.schemas import VideoParseResult
from engine.parsev2.validation import (
    ChunkResultUpdate,
    IntervalRefinementProposal,
    PrefixRefinementProposal,
    RefinementMergeConflictError,
    RefinementMember,
    apply_chunk_update,
    merge_refinement_content,
    ordered_scope_union,
    validate_interval_refinement,
    validate_member_scopes,
    validate_prefix_refinement,
    validate_result_time_range,
)


def _result(*, person: str = "爱音", description: str = "爱音走进房间") -> VideoParseResult:
    return VideoParseResult.model_validate({
        "persons": [person],
        "timeline": [{
            "event_type": "ACTION",
            "start_seconds": 1000,
            "end_seconds": 3000,
            "description": description,
            "participants": [person],
            "visible_persons": [person],
        }],
    })


def test_result_time_validation_uses_the_host_authorized_media_range() -> None:
    result = _result()
    assert validate_result_time_range(result, start_seconds=0, end_seconds=4000) == result
    with pytest.raises(ValueError, match="outside the authorized"):
        validate_result_time_range(result, start_seconds=2000, end_seconds=4000)


def test_refine_keeps_only_actual_explicit_changes() -> None:
    original = _result()
    proposal = IntervalRefinementProposal(updates=[ChunkResultUpdate(
        sequence_no=0,
        persons=["爱音"],
        timeline=[{
            "event_type": "ACTION",
            "start_seconds": 1000,
            "end_seconds": 3000,
            "description": "爱音推门走进房间",
            "participants": ["爱音"],
            "visible_persons": ["爱音"],
        }],
    )])
    validated = validate_interval_refinement(
        proposal,
        members=[RefinementMember(0, 0, 20000, original)],
    )

    assert len(validated.updates) == 1
    assert validated.updates[0].changed_fields == {"timeline"}


def test_explicit_fields_apply_to_current_content_without_snapshot_overwrite() -> None:
    snapshot = _result()
    proposal = IntervalRefinementProposal(updates=[ChunkResultUpdate(
        sequence_no=0,
        persons=["爱音", "乐奈"],
    )])
    validated = validate_interval_refinement(
        proposal,
        members=[RefinementMember(0, 0, 20000, snapshot)],
    )

    concurrently_updated = _result(description="另一窗口已经修正动作描述")
    committed = apply_chunk_update(concurrently_updated, validated.updates[0])
    assert committed.persons == ["爱音", "乐奈"]
    assert committed.timeline[0].description == "另一窗口已经修正动作描述"


def test_refine_rejects_targets_and_events_outside_the_interval_authority() -> None:
    member = RefinementMember(2, 40000, 60000, _result())
    with pytest.raises(ValueError, match="outside its members"):
        validate_interval_refinement(
            IntervalRefinementProposal(
                updates=[ChunkResultUpdate(sequence_no=1, persons=["爱音"])]
            ),
            members=[member],
        )

    outside = _result().model_dump(mode="python")
    outside["timeline"][0]["start_seconds"] = 39000
    outside["timeline"][0]["end_seconds"] = 41000
    with pytest.raises(ValueError, match="outside the authorized"):
        validate_interval_refinement(
            IntervalRefinementProposal(updates=[ChunkResultUpdate(
                sequence_no=2,
                timeline=outside["timeline"],
            )]),
            members=[member],
        )


def test_scope_validation_and_union_are_ordered_deduplicated_and_monotonic() -> None:
    assert ordered_scope_union([0, 2], [1, 2, 3]) == [0, 1, 2, 3]
    assert validate_member_scopes(
        {1: [0, 1], 2: [2]},
        member_sequence_nos=[1, 2],
    ) == {1: (0, 1), 2: (2,)}

    with pytest.raises(ValueError, match="ordered and unique"):
        validate_member_scopes({1: [1, 0]}, member_sequence_nos=[1])
    with pytest.raises(ValueError, match="own sequence"):
        validate_member_scopes({1: [0]}, member_sequence_nos=[1])


@pytest.mark.parametrize("scope", [None, [], "0", [0, None], [False], [-1, 0]])
def test_scope_validation_rejects_invalid_persisted_metadata(scope) -> None:
    with pytest.raises(ValueError, match="scope"):
        validate_member_scopes({0: scope}, member_sequence_nos=[0])


def test_prefix_is_bound_to_one_target_and_compact_budgets() -> None:
    member = RefinementMember(3, 0, 20000, _result())
    proposal = PrefixRefinementProposal(
        series_compact="series",
        video_compact="video",
        update=ChunkResultUpdate(sequence_no=3, persons=["爱音", "乐奈"]),
    )
    result = validate_prefix_refinement(
        proposal,
        member=member,
        series_compact_max_chars=10,
        video_compact_max_chars=10,
    )
    assert result.update is not None

    with pytest.raises(ValueError, match="outside its bound target"):
        validate_prefix_refinement(
            proposal.model_copy(update={
                "update": ChunkResultUpdate(sequence_no=4, persons=["爱音"])
            }),
            member=member,
            series_compact_max_chars=10,
            video_compact_max_chars=10,
        )


def _merge_base() -> dict:
    first = _result().model_dump(mode="json")
    second = _result(person="爽世", description="爽世坐下").model_dump(mode="json")["timeline"][0]
    second.update(start_seconds=4000, end_seconds=6000)
    return {"persons": ["爱音", "爽世"], "timeline": [first["timeline"][0], second]}


def test_refinement_merge_preserves_changes_to_different_events_without_mutating_inputs() -> None:
    base = _merge_base()
    current, proposed = deepcopy(base), deepcopy(base)
    current["timeline"][0]["description"] = "爱音已经推开房门"
    proposed["timeline"][1]["description"] = "爽世坐在窗边"
    before = deepcopy((base, current, proposed))

    merged = merge_refinement_content(MappingProxyType(base), current, proposed)

    assert isinstance(merged, dict)
    assert merged["timeline"] == [current["timeline"][0], proposed["timeline"][1]]
    assert merged["persons"] == base["persons"]
    assert (base, current, proposed) == before


@pytest.mark.parametrize("same_field", [True, False])
def test_refinement_merge_rejects_different_changes_to_the_same_event(same_field) -> None:
    base = _merge_base()
    current, proposed = deepcopy(base), deepcopy(base)
    current["timeline"][0]["description"] = "先提交的动作修正"
    if same_field:
        proposed["timeline"][0]["description"] = "另一窗口的动作修正"
    else:
        proposed["timeline"][0]["visible_persons"] = []
    before = deepcopy((base, current, proposed))

    with pytest.raises(RefinementMergeConflictError):
        merge_refinement_content(base, current, proposed)

    assert (base, current, proposed) == before


def test_refinement_merge_accepts_identical_changes_idempotently() -> None:
    base = _merge_base()
    current = deepcopy(base)
    current["persons"].append("灯")
    current["timeline"][0]["description"] = "两个窗口一致的修正"
    proposed = deepcopy(current)

    assert merge_refinement_content(base, current, proposed) == current
    assert merge_refinement_content(base, current, base) == current
    assert merge_refinement_content(base, base, proposed) == proposed
    assert merge_refinement_content(base, base, base) == base


def test_refinement_merge_handles_persons_as_one_field_independently_from_timeline() -> None:
    base = _merge_base()
    current, proposed = deepcopy(base), deepcopy(base)
    current["persons"].append("灯")
    proposed["timeline"][1]["description"] = "另一个事件的修正"

    assert merge_refinement_content(base, current, proposed) == {
        "persons": current["persons"], "timeline": proposed["timeline"],
    }
    assert merge_refinement_content(base, proposed, current) == {
        "persons": current["persons"], "timeline": proposed["timeline"],
    }
    proposed["persons"].append("立希")
    with pytest.raises(RefinementMergeConflictError):
        merge_refinement_content(base, current, proposed)


@pytest.mark.parametrize("change", ["add", "remove", "reorder", "retime", "type", "duplicate"])
@pytest.mark.parametrize("swap_sides", [False, True])
def test_refinement_merge_rejects_ambiguous_concurrent_timeline_structure(change, swap_sides) -> None:
    base = _merge_base()
    current, proposed = deepcopy(base), deepcopy(base)
    current["timeline"][0]["description"] = "并发窗口的内容修正"
    if change == "add":
        added = deepcopy(proposed["timeline"][1])
        added.update(start_seconds=7000, end_seconds=8000)
        proposed["timeline"].append(added)
    elif change == "remove":
        proposed["timeline"].pop()
    elif change == "reorder":
        proposed["timeline"].reverse()
    elif change == "retime":
        proposed["timeline"][1]["start_seconds"] += 1
    elif change == "type":
        proposed["timeline"][1]["event_type"] = "EMOTION"
    else:
        proposed["timeline"].append(deepcopy(proposed["timeline"][0]))
    before = deepcopy((base, current, proposed))

    with pytest.raises(RefinementMergeConflictError):
        merge_refinement_content(base, proposed if swap_sides else current,
                                 current if swap_sides else proposed)

    assert (base, current, proposed) == before


def test_refinement_merge_rejects_duplicate_keys_even_when_all_key_lists_match() -> None:
    base = _merge_base()
    base["timeline"].append(deepcopy(base["timeline"][0]))
    current, proposed = deepcopy(base), deepcopy(base)
    current["timeline"][0]["description"] = "第一个事件修正"
    proposed["timeline"][1]["description"] = "第二个事件修正"

    with pytest.raises(RefinementMergeConflictError):
        merge_refinement_content(base, current, proposed)


@pytest.mark.parametrize("change", ["add", "remove", "retime"])
@pytest.mark.parametrize("swap_sides", [False, True])
def test_refinement_merge_accepts_unilateral_timeline_structure_changes(change, swap_sides) -> None:
    base = _merge_base()
    changed = deepcopy(base)
    if change == "add":
        added = deepcopy(changed["timeline"][1])
        added.update(start_seconds=7000, end_seconds=8000)
        changed["timeline"].append(added)
    elif change == "remove":
        changed["timeline"].pop()
    else:
        changed["timeline"][1]["start_seconds"] += 1
    before = deepcopy((base, changed))

    assert merge_refinement_content(base, base if swap_sides else changed,
                                    changed if swap_sides else base) == changed
    assert (base, changed) == before
