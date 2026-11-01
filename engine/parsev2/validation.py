"""Authority and actual-field validation for Video Parse V2 results."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
import json
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .schemas import TimelineEvent, VideoParseResult


_PersonName = Annotated[str, Field(min_length=1, pattern=r"\S")]


class _ValidationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", revalidate_instances="always")


class ChunkResultUpdate(_ValidationModel):
    """Complete validated fields assembled by the program for one Chunk commit."""

    sequence_no: int = Field(ge=0, strict=True)
    persons: list[_PersonName] | None = None
    timeline: list[TimelineEvent] | None = None

    @model_validator(mode="after")
    def validate_explicit_fields(self) -> "ChunkResultUpdate":
        changed_fields = self.model_fields_set - {"sequence_no"}
        if not changed_fields:
            raise ValueError("an update must explicitly provide persons or timeline")
        if any(getattr(self, field) is None for field in changed_fields):
            raise ValueError("explicit update fields cannot be null")
        return self

    @property
    def changed_fields(self) -> frozenset[str]:
        return frozenset(self.model_fields_set - {"sequence_no"})


class IntervalRefinementProposal(_ValidationModel):
    """Compatibility transport for complete snapshot-relative member fields.

    The host derives these fields from validated complete Refine JSON or a
    local Canvas draft. Omitted members and fields stay unchanged.
    """

    updates: list[ChunkResultUpdate] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_unique_targets(self) -> "IntervalRefinementProposal":
        targets = [update.sequence_no for update in self.updates]
        if len(set(targets)) != len(targets):
            raise ValueError("Interval updates must target each member at most once")
        return self


class ValidatedIntervalRefinement(_ValidationModel):
    """Program-assembled fields that differ from the bound input snapshot."""

    updates: list[ChunkResultUpdate]


class RefinedMember(VideoParseResult):
    """One complete model-produced member; persistent identity stays with the host."""

    sequence_no: int = Field(ge=0, strict=True)


class IntervalRefinementResult(_ValidationModel):
    """Complete JSON returned by the direct Refine model."""

    members: list[RefinedMember] = Field(min_length=1)


def normalize_refinement_response(value: Any) -> Any:
    """Order Refine timelines without changing event values or caller input.

    The harness validates and, on failure, returns this same value, so error
    locations always point into the rejected response provided to the model.
    Canonical VideoParseResult validation remains strict for every caller.
    """
    if not isinstance(value, dict) or not isinstance(value.get("members"), list):
        return value
    members = []
    for member in value["members"]:
        timeline = member.get("timeline") if isinstance(member, dict) else None
        # Never coerce, clip, deduplicate or infer an invalid time value.
        if isinstance(timeline, list) and all(
            isinstance(event, dict) and type(event.get("start_seconds")) is int
            for event in timeline
        ):
            member = {**member, "timeline": sorted(timeline, key=lambda event: event["start_seconds"])}
        members.append(member)
    return {**value, "members": members}


class RefinementResultError(ValueError):
    """A correctable result-gate failure, distinct from lease/network failures."""


class PrefixRefinementProposal(_ValidationModel):
    """Compatibility transport for summaries and complete bound-target fields."""

    series_compact: str
    video_compact: str
    update: ChunkResultUpdate | None = None


class ValidatedPrefixRefinement(_ValidationModel):
    series_compact: str
    video_compact: str
    update: ChunkResultUpdate | None = None


@dataclass(frozen=True, slots=True)
class RefinementMember:
    """Trusted member identity, time authority and current model input snapshot."""

    sequence_no: int
    start_seconds: int
    end_seconds: int
    content: VideoParseResult

    def __post_init__(self) -> None:
        if (
            isinstance(self.sequence_no, bool)
            or not isinstance(self.sequence_no, int)
            or self.sequence_no < 0
        ):
            raise ValueError("sequence_no must be a nonnegative integer")
        _validate_bounds(self.start_seconds, self.end_seconds)


def _validate_bounds(start_seconds: int, end_seconds: int) -> None:
    if (
        isinstance(start_seconds, bool)
        or not isinstance(start_seconds, int)
        or start_seconds < 0
        or isinstance(end_seconds, bool)
        or not isinstance(end_seconds, int)
        or end_seconds <= start_seconds
    ):
        raise ValueError("time bounds must be increasing nonnegative integers")


def validate_result_time_range(
    result: VideoParseResult,
    *,
    start_seconds: int,
    end_seconds: int,
    dialogue_bounds: tuple[int, int] | None = None,
) -> VideoParseResult:
    """Revalidate a result and keep every event inside the trusted media window."""

    _validate_bounds(start_seconds, end_seconds)
    if dialogue_bounds is not None:
        _validate_bounds(*dialogue_bounds)
    validated = VideoParseResult.model_validate(result)
    for event in validated.timeline:
        start, end = (dialogue_bounds if dialogue_bounds is not None
                      and event.event_type == "DIALOGUE" else (start_seconds, end_seconds))
        if event.start_seconds < start or event.end_seconds > end:
            raise ValueError(
                "timeline event falls outside the authorized media time range"
            )
    return validated


def parse_content_fingerprint(content: Mapping[str, object]) -> str:
    """Bind a persisted parse snapshot to its full canonical JSON payload."""

    return sha256(json.dumps(
        dict(content), ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")).hexdigest()


class RefinementMergeConflictError(ValueError):
    """Concurrent edits cannot be combined at whole-event granularity."""

    def __init__(self, message: str, *, reason: str = "entry_changed",
                 context: Mapping[str, Any] | None = None) -> None:
        self.reason = reason
        self.context = dict(context or {})
        super().__init__(message)


def _merge_refinement_value(base: object, current: object, proposed: object) -> object:
    if proposed == base or proposed == current:
        return deepcopy(current)
    if current == base:
        return deepcopy(proposed)
    raise RefinementMergeConflictError("Refine changed the same entry differently")


def merge_refinement_content(
    base: Mapping[str, Any],
    current: Mapping[str, Any],
    proposed: Mapping[str, Any],
) -> dict[str, Any]:
    """Merge validated results; ambiguous event structure retains a conflict.

    Independent context changes may survive without another model review.
    Persons remain one atomic list, and each timeline event is atomic too.
    """
    merged = deepcopy(dict(current))
    for field in ("persons", "timeline"):
        original, latest, candidate = base[field], current[field], proposed[field]
        if (field != "timeline" or candidate == original
                or latest == original or candidate == latest):
            try:
                merged[field] = _merge_refinement_value(original, latest, candidate)
            except RefinementMergeConflictError as error:
                raise RefinementMergeConflictError(
                    "Refine changed the persons list differently", reason="persons_changed",
                    context={"field": field},
                ) from error
            continue
        # Execution-local Canvas refs are not persistent event identities.
        # Only unchanged, unique, ordered time/type anchors permit alignment.
        identities = [
            [(event["event_type"], event["start_seconds"], event["end_seconds"])
             for event in events]
            for events in (original, latest, candidate)
        ]
        if (identities[0] != identities[1] or identities[0] != identities[2]
                or len(set(identities[0])) != len(identities[0])):
            mismatch = next((index for index in range(max(map(len, identities)))
                             if len({keys[index] if index < len(keys) else None for keys in identities}) > 1), None)
            raise RefinementMergeConflictError(
                "Refine event structure changed or is ambiguous", reason="timeline_structure_changed",
                context={"field": field, "base_events": len(original), "current_events": len(latest),
                         "proposed_events": len(candidate), "current_anchors_changed": identities[0] != identities[1],
                         "proposed_anchors_changed": identities[0] != identities[2],
                         "first_mismatch_index": mismatch,
                         "first_mismatch_anchors": {
                             name: dict(zip(("event_type", "start_ms", "end_ms"), keys[mismatch]))
                             if mismatch is not None and mismatch < len(keys) else None
                             for name, keys in zip(("base", "current", "proposed"), identities)
                         },
                         "duplicate_base_anchors": len(set(identities[0])) != len(identities[0])},
            )
        merged[field] = []
        for index, (before, now, after) in enumerate(zip(original, latest, candidate, strict=True)):
            try:
                merged[field].append(_merge_refinement_value(before, now, after))
            except RefinementMergeConflictError as error:
                raise RefinementMergeConflictError(
                    "Refine changed the same event differently", reason="event_changed",
                    context={"field": field, "event_index": index, "event_type": before["event_type"],
                             "start_ms": before["start_seconds"], "end_ms": before["end_seconds"],
                             "current_changed_fields": sorted(key for key in before.keys() | now.keys()
                                                              if before.get(key) != now.get(key)),
                             "proposed_changed_fields": sorted(key for key in before.keys() | after.keys()
                                                               if before.get(key) != after.get(key))},
                ) from error
    return merged


def apply_chunk_update(
    current: VideoParseResult,
    update: ChunkResultUpdate,
) -> VideoParseResult:
    """Apply complete program fields to an exact snapshot, retaining omissions."""

    values = current.model_dump(mode="python")
    for field in update.changed_fields:
        value = getattr(update, field)
        if isinstance(value, list):
            values[field] = [
                item.model_dump(mode="python") if isinstance(item, BaseModel) else item
                for item in value
            ]
        else:
            values[field] = value
    return VideoParseResult.model_validate(values)


def validate_refinement_result(
    result: IntervalRefinementResult,
    *,
    members: Sequence[RefinementMember],
    dialogue_bounds: tuple[int, int] | None = None,
) -> ValidatedIntervalRefinement:
    """Business gate for a complete result; never mutates or persists a snapshot."""
    result = IntervalRefinementResult.model_validate(result)
    expected = [member.sequence_no for member in members]
    actual = [member.sequence_no for member in result.members]
    if len(actual) != len(set(actual)) or set(actual) != set(expected):
        raise RefinementResultError(
            f"members.sequence_no: return every member exactly once; expected {expected}, got {actual}"
        )
    try:
        return validate_interval_refinement(
            IntervalRefinementProposal(updates=[
                ChunkResultUpdate(sequence_no=member.sequence_no,
                                  persons=member.persons, timeline=member.timeline)
                for member in result.members
            ]),
            members=members, dialogue_bounds=dialogue_bounds,
        )
    except ValueError as error:
        raise RefinementResultError(f"members.timeline: {error}") from error


def validate_interval_refinement(
    proposal: IntervalRefinementProposal,
    *,
    members: Sequence[RefinementMember],
    dialogue_bounds: tuple[int, int] | None = None,
) -> ValidatedIntervalRefinement:
    """Validate program-assembled fields against the exact refinement snapshot.

    The transaction must verify the base identity and merge these complete
    fields against current content. Direct Refine output passes through the
    complete-result gate before reaching this internal update contract.
    """

    proposal = IntervalRefinementProposal.model_validate(proposal)
    ordered_members = tuple(members)
    if not ordered_members:
        raise ValueError("an Interval must have at least one member")
    sequences = [member.sequence_no for member in ordered_members]
    if sequences != list(range(sequences[0], sequences[0] + len(sequences))):
        raise ValueError("Interval members must be ordered and consecutive")

    by_sequence = {member.sequence_no: member for member in ordered_members}
    interval_start = ordered_members[0].start_seconds
    interval_end = ordered_members[-1].end_seconds
    actual_updates: list[ChunkResultUpdate] = []
    for proposed in proposal.updates:
        member = by_sequence.get(proposed.sequence_no)
        if member is None:
            raise ValueError("Interval proposal targets a Chunk outside its members")

        actual: dict[str, object] = {"sequence_no": proposed.sequence_no}
        for field in proposed.changed_fields:
            if getattr(proposed, field) != getattr(member.content, field):
                actual[field] = getattr(proposed, field)
        if len(actual) == 1:
            continue

        update = ChunkResultUpdate.model_validate(actual)
        candidate = apply_chunk_update(member.content, update)
        validate_result_time_range(
            candidate,
            start_seconds=interval_start,
            end_seconds=interval_end,
            dialogue_bounds=dialogue_bounds,
        )
        actual_updates.append(update)

    return ValidatedIntervalRefinement(updates=actual_updates)


def ordered_scope_union(
    current_scope: Iterable[int],
    member_sequence_nos: Iterable[int],
) -> list[int]:
    """Return the monotonic ordered set union used by transactional commits."""

    values: list[int] = []
    for value in (*tuple(current_scope), *tuple(member_sequence_nos)):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("scope values must be nonnegative integers")
        values.append(value)
    return sorted(set(values))


def validate_member_scopes(
    member_scopes: Mapping[int, Sequence[int]],
    *,
    member_sequence_nos: Sequence[int],
) -> dict[int, tuple[int, ...]]:
    """Validate the persisted scope hints supplied to an Interval prompt."""

    expected = tuple(member_sequence_nos)
    if set(member_scopes) != set(expected):
        raise ValueError("member scopes must match the Interval members exactly")
    validated: dict[int, tuple[int, ...]] = {}
    for sequence_no in expected:
        raw_scope = member_scopes[sequence_no]
        if not isinstance(raw_scope, (list, tuple)):
            raise ValueError("member scope must be an ordered sequence of Chunk numbers")
        scope = tuple(raw_scope)
        if not scope:
            raise ValueError("member scope must not be empty")
        ordered_scope_union(scope, ())
        if list(scope) != sorted(set(scope)):
            raise ValueError("member scope must be ordered and unique")
        if sequence_no not in scope:
            raise ValueError("member scope must include its own sequence number")
        validated[sequence_no] = scope
    return validated


def validate_prefix_refinement(
    proposal: PrefixRefinementProposal,
    *,
    member: RefinementMember,
    series_compact_max_chars: int,
    video_compact_max_chars: int,
) -> ValidatedPrefixRefinement:
    """Restrict Prefix output to its bound target and configured compact budgets."""

    proposal = PrefixRefinementProposal.model_validate(proposal)
    if len(proposal.series_compact) > series_compact_max_chars:
        raise ValueError("series_compact exceeds its configured character budget")
    if len(proposal.video_compact) > video_compact_max_chars:
        raise ValueError("video_compact exceeds its configured character budget")

    update = proposal.update
    if update is not None:
        if update.sequence_no != member.sequence_no:
            raise ValueError("Prefix update targets a Chunk outside its bound target")
        validated = validate_interval_refinement(
            IntervalRefinementProposal(updates=[update]),
            members=[member],
        )
        update = validated.updates[0] if validated.updates else None

    return ValidatedPrefixRefinement(
        series_compact=proposal.series_compact,
        video_compact=proposal.video_compact,
        update=update,
    )
