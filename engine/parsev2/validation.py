"""Authority and actual-field validation for Video Parse V2 results."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
import json
from typing import Annotated

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

    Active agents assemble these fields from Canvas; model tools use explicit
    local operations instead. Omitted members and fields stay unchanged.
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
) -> VideoParseResult:
    """Revalidate a result and keep every event inside the trusted media window."""

    _validate_bounds(start_seconds, end_seconds)
    validated = VideoParseResult.model_validate(result)
    for event in validated.timeline:
        if event.start_seconds < start_seconds or event.end_seconds > end_seconds:
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


def validate_interval_refinement(
    proposal: IntervalRefinementProposal,
    *,
    members: Sequence[RefinementMember],
) -> ValidatedIntervalRefinement:
    """Validate program-assembled fields against the exact refinement snapshot.

    The transaction must check every input version and fingerprint before
    applying these complete fields. Model-facing operations live in Canvas.
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
