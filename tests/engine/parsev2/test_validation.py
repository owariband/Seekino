from __future__ import annotations

import pytest

from engine.parsev2.schemas import VideoParseResult
from engine.parsev2.validation import (
    ChunkResultUpdate,
    IntervalRefinementProposal,
    PrefixRefinementProposal,
    RefinementMember,
    apply_chunk_update,
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
