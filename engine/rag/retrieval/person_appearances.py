"""Deterministic person appearance enumeration over formal Timeline Events.

Storage and authorization stay with the caller; this module only scans the
already trusted Chunk results it is given.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Iterable, Sequence
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from engine.parsev2.schemas import DialogueEvent, TimelineEvent, VideoParseResult
from engine.rag.indexing.events import EventType

_NonBlank = Annotated[str, Field(min_length=1, pattern=r"\S")]
PersonAppearanceRole = Literal["speaking", "visible", "participant"]
_ROLE_ORDER: tuple[PersonAppearanceRole, ...] = ("speaking", "visible", "participant")


class _AppearanceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, revalidate_instances="always")


class PersonTimelineChunk(_AppearanceModel):
    """One formal Chunk result with the Video identity that supplied it."""

    video_id: _NonBlank
    video_chunk_id: _NonBlank
    result: VideoParseResult


class PersonAppearanceSource(_AppearanceModel):
    """One matched Timeline Event and the exact stored name that matched."""

    video_chunk_id: _NonBlank
    timeline_index: int = Field(ge=0, strict=True)
    event_type: EventType
    start_seconds: int = Field(ge=0, strict=True)
    end_seconds: int = Field(gt=0, strict=True)
    matched_name: _NonBlank
    roles: tuple[PersonAppearanceRole, ...]

    @model_validator(mode="after")
    def validate_source(self) -> PersonAppearanceSource:
        if self.end_seconds <= self.start_seconds:
            raise ValueError("appearance source bounds must be increasing")
        _validate_roles(self.roles)
        return self


class PersonAppearance(_AppearanceModel):
    """Merged, playable interval retaining every contributing Event and stored name."""

    video_id: _NonBlank
    start_seconds: int = Field(ge=0, strict=True)
    end_seconds: int = Field(gt=0, strict=True)
    roles: tuple[PersonAppearanceRole, ...]
    matched_names: tuple[_NonBlank, ...]
    sources: tuple[PersonAppearanceSource, ...]

    @model_validator(mode="after")
    def validate_appearance(self) -> PersonAppearance:
        if self.end_seconds <= self.start_seconds:
            raise ValueError("appearance bounds must be increasing")
        _validate_roles(self.roles)
        if not self.matched_names or len(set(self.matched_names)) != len(self.matched_names):
            raise ValueError("appearance matched names must be unique and nonempty")
        if not self.sources:
            raise ValueError("appearance requires at least one source")
        if any(source.start_seconds < self.start_seconds or source.end_seconds > self.end_seconds
               for source in self.sources):
            raise ValueError("appearance sources must fall inside the merged interval")
        if tuple(sorted(self.sources, key=lambda source: (
            source.start_seconds, source.end_seconds,
        ))) != self.sources:
            raise ValueError("appearance sources must be ordered by time")
        names = tuple(dict.fromkeys(source.matched_name for source in self.sources))
        if names != self.matched_names:
            raise ValueError("appearance matched names must follow source order")
        return self


def list_person_appearances(
    chunks: Sequence[PersonTimelineChunk],
    names: Iterable[str],
    *,
    merge_gap_ms: int = 2000,
) -> tuple[PersonAppearance, ...]:
    """List every matching Event as merged half-open millisecond intervals.

    Names match casefolded NFKC equality, or substring containment when the
    query name has at least two characters. Returned names stay exactly as the
    Timeline stored them. Videos keep their first input appearance order.
    """

    if type(merge_gap_ms) is not int or merge_gap_ms < 0:
        raise ValueError("merge_gap_ms must be a nonnegative integer")
    queries = _normalize_query_names(names)
    video_order: list[str] = []
    sources_by_video: dict[str, list[tuple[PersonAppearanceSource, int]]] = {}
    for chunk_order, value in enumerate(chunks):
        chunk = PersonTimelineChunk.model_validate(value)
        if chunk.video_id not in sources_by_video:
            video_order.append(chunk.video_id)
            sources_by_video[chunk.video_id] = []
        bucket = sources_by_video[chunk.video_id]
        for timeline_index, event in enumerate(chunk.result.timeline):
            bucket.extend(
                (source, chunk_order)
                for source in _event_sources(chunk, timeline_index, event, queries)
            )
    appearances: list[PersonAppearance] = []
    for video_id in video_order:
        sources = sources_by_video[video_id]
        if not sources:
            continue
        sources.sort(key=lambda item: (
            item[0].start_seconds, item[0].end_seconds,
            item[1], item[0].timeline_index,
        ))
        merged = [sources[0][0]]
        start, end = merged[0].start_seconds, merged[0].end_seconds
        for source, _chunk_order in sources[1:]:
            if source.start_seconds <= end + merge_gap_ms:
                merged.append(source)
                end = max(end, source.end_seconds)
                continue
            appearances.append(_appearance(video_id, start, end, merged))
            merged = [source]
            start, end = source.start_seconds, source.end_seconds
        appearances.append(_appearance(video_id, start, end, merged))
    return tuple(appearances)


def _normalize_name(value: str) -> str:
    return unicodedata.normalize("NFKC", value).strip().casefold()


def _normalize_query_names(names: Iterable[str]) -> tuple[str, ...]:
    normalized_names = []
    for value in names:
        if not isinstance(value, str):
            raise ValueError("names must be strings")
        normalized = _normalize_name(value)
        if normalized:
            normalized_names.append(normalized)
    if not normalized_names:
        raise ValueError("names must contain at least one nonblank name")
    return tuple(dict.fromkeys(normalized_names))


def _name_matches(query: str, candidate: str) -> bool:
    # A single-character query only matches exactly; short substrings would
    # otherwise attribute unrelated names that share one Chinese character.
    return query == candidate or len(query) >= 2 and query in candidate


def _event_sources(
    chunk: PersonTimelineChunk,
    timeline_index: int,
    event: TimelineEvent,
    queries: tuple[str, ...],
) -> tuple[PersonAppearanceSource, ...]:
    stored_names = list(dict.fromkeys(
        ([event.speaker] if isinstance(event, DialogueEvent) and event.speaker is not None else [])
        + event.participants
        + event.visible_persons
    ))
    sources = []
    for stored_name in stored_names:
        normalized = _normalize_name(stored_name)
        if not any(_name_matches(query, normalized) for query in queries):
            continue
        roles: tuple[PersonAppearanceRole, ...] = tuple(
            role for role, applies in (
                ("speaking", isinstance(event, DialogueEvent) and event.speaker == stored_name),
                ("visible", stored_name in event.visible_persons),
                ("participant", stored_name in event.participants),
            ) if applies
        )
        if not roles:
            continue
        sources.append(PersonAppearanceSource(
            video_chunk_id=chunk.video_chunk_id, timeline_index=timeline_index,
            event_type=event.event_type, start_seconds=event.start_seconds,
            end_seconds=event.end_seconds, matched_name=stored_name, roles=roles,
        ))
    return tuple(sources)


def _appearance(
    video_id: str, start_seconds: int, end_seconds: int, sources: Sequence[PersonAppearanceSource],
) -> PersonAppearance:
    present = {role for source in sources for role in source.roles}
    return PersonAppearance(
        video_id=video_id, start_seconds=start_seconds, end_seconds=end_seconds,
        roles=tuple(role for role in _ROLE_ORDER if role in present),
        matched_names=tuple(dict.fromkeys(source.matched_name for source in sources)),
        sources=tuple(sources),
    )


def _validate_roles(roles: tuple[PersonAppearanceRole, ...]) -> None:
    if not roles or roles != tuple(role for role in _ROLE_ORDER if role in set(roles)):
        raise ValueError(
            "appearance roles must be a nonempty subset in speaking/visible/participant order"
        )


__all__ = [
    "PersonAppearanceRole", "PersonTimelineChunk", "PersonAppearanceSource",
    "PersonAppearance", "list_person_appearances",
]
