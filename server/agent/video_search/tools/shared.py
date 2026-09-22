from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass, field
import json
from threading import Lock
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, StringConstraints

from ....schema import VideoChunkSearchHit
from ...common.errors import AgentModelInvocationError, AgentToolFeedbackError
from ...common.state import ExecutionControl
from ...common.tool_output import bounded_json, range_text
from ..state import DraftItem
from ..workspace import AgentWorkspace

_OptionalCursor = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=2_000),
] | None

class RemoveItemArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    video_chunk_id: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=36),
    ]

@dataclass(slots=True)
class _ToolCallBudget:
    maximum: int
    role: str
    used: int = 0
    _lock: Lock = field(default_factory=Lock, init=False, repr=False)

    def __post_init__(self) -> None:
        if (
            isinstance(self.maximum, bool)
            or not isinstance(self.maximum, int)
            or self.maximum <= 0
        ):
            raise ValueError("maximum Tool calls must be a positive integer")

    def consume(self, tool_name: str) -> None:
        with self._lock:
            self.used += 1
            if self.used > self.maximum:
                raise AgentModelInvocationError(
                    f"{self.role} Agent Tool-call budget exceeded before {tool_name}"
                )


@dataclass(slots=True)
class DraftReadAudit:
    catalog_complete: bool = False
    expected: dict[str, dict[str, int]] = field(default_factory=dict)
    ranges: dict[str, dict[str, list[tuple[int, int]]]] = field(default_factory=dict)
    _lock: Lock = field(default_factory=Lock, init=False, repr=False)

    def mark_catalog_complete(self) -> None:
        with self._lock:
            self.catalog_complete = True

    def mark_overview(self, video_chunk_id: str, sections: dict[str, str]) -> None:
        with self._lock:
            lengths = {name: len(content) for name, content in sections.items()}
            if self.expected.get(video_chunk_id) != lengths:
                self.expected[video_chunk_id] = lengths
                self.ranges[video_chunk_id] = {name: [] for name in lengths}

    def mark_range(
        self, video_chunk_id: str, section: str, start: int, end: int,
    ) -> None:
        with self._lock:
            ranges = self.ranges.get(video_chunk_id)
            if ranges is not None and section in ranges:
                ranges[section].append((start, end))

    def covers(self, video_chunk_ids: Collection[str]) -> bool:
        with self._lock:
            return self.catalog_complete and all(
                video_chunk_id in self.expected
                and all(
                    _ranges_cover(
                        self.ranges[video_chunk_id][section], total_chars,
                    )
                    for section, total_chars in self.expected[video_chunk_id].items()
                )
                for video_chunk_id in video_chunk_ids
            )


def _ranges_cover(ranges: list[tuple[int, int]], total_chars: int) -> bool:
    covered = 0
    for start, end in sorted(ranges):
        if start > covered:
            return False
        covered = max(covered, end)
        if covered >= total_chars:
            return True
    return covered >= total_chars


_EVIDENCE_SECTION_FIELDS = {
    "evidence.transcript": ("transcript",),
    "evidence.events": ("allTimeEvents", "allTimeEventText"),
    "evidence.text": (
        "baselineVisualCaption", "retrievalText", "videoCompact",
        "contextBeforeCompact", "contextAfterCompact", "windowSummary",
    ),
    "evidence.series_context": (
        "seriesDescription", "importantExtraData", "characterBible",
    ),
}


def _detail_sections(
    hit: VideoChunkSearchHit, draft: DraftItem | None = None,
) -> dict[str, str]:
    evidence = hit.evidence.model_dump(mode="json", by_alias=True)
    grouped = {
        name: {field_name: evidence[field_name] for field_name in field_names}
        for name, field_names in _EVIDENCE_SECTION_FIELDS.items()
    }
    grouped_fields = {
        field_name
        for field_names in _EVIDENCE_SECTION_FIELDS.values()
        for field_name in field_names
    }
    grouped["evidence.metadata"] = {
        name: value for name, value in evidence.items() if name not in grouped_fields
    }
    sections = {
        "candidate": hit.candidate.model_dump(mode="json", by_alias=True),
        "selection_reason": hit.selection_reason.model_dump(mode="json", by_alias=True),
        **grouped,
    }
    if draft is not None:
        sections = {"draft": draft.model_dump(mode="json"), **sections}
    return {
        name: json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        for name, value in sections.items()
    }


def _read_detail_section(
    video_chunk_id: str,
    sections: dict[str, str],
    *,
    section: str,
    start: int,
    end: int | None,
    maximum_result_bytes: int,
    maximum_read_chars: int,
) -> str:
    if section == "overview":
        if start != 0 or end is not None:
            raise AgentToolFeedbackError(
                "INVALID_RANGE", "overview does not accept a character range.",
                "Call with video_chunk_id and section='overview' only.",
            )
        return bounded_json({
            "id": video_chunk_id,
            "section": "overview",
            "sections": [
                {"name": name, "total_chars": len(content)}
                for name, content in sections.items()
            ],
        }, maximum_result_bytes)
    content = sections.get(section)
    if content is None:
        raise AgentToolFeedbackError(
            "UNKNOWN_SECTION", f"Unknown section {section!r}.",
            "Read section='overview' to see the available section names.",
        )
    return range_text(
        content,
        resource_id=video_chunk_id,
        section=section,
        start=start,
        end=end,
        maximum_bytes=maximum_result_bytes,
        maximum_chars=maximum_read_chars,
    )

def _start_tool_call(
    control: ExecutionControl,
    budget: _ToolCallBudget | None,
    tool_name: str,
) -> None:
    control.check()
    if budget is not None:
        budget.consume(tool_name)


def _retrieval_card(hit: VideoChunkSearchHit) -> dict[str, Any]:
    return {
        "video_chunk_id": hit.candidate.video_chunk_id,
        "episode_order": hit.evidence.episode_order,
        "start_ms": hit.evidence.start_ms,
        "end_ms": hit.evidence.end_ms,
        "description": _bounded_text(hit.selection_reason.summary, 500),
        "fused_rank": hit.candidate.fused_rank,
        "fused_score": hit.candidate.fused_score,
        "transcript_rank": hit.candidate.transcript_rank,
        "semantic_rank": hit.candidate.text_rank,
        "visual_rank": hit.candidate.vision_rank,
    }


def _draft_card(workspace: AgentWorkspace, item: DraftItem) -> dict[str, Any]:
    evidence = workspace.retrieval_item(item.video_chunk_id).evidence
    return {
        "video_chunk_id": item.video_chunk_id,
        "episode_order": evidence.episode_order,
        "start_ms": item.start_ms,
        "end_ms": item.end_ms,
        "description": _bounded_text(item.claim, 500),
    }


def _bounded_text(value: str, maximum_chars: int) -> str:
    if len(value) <= maximum_chars:
        return value
    return value[: maximum_chars - 1].rstrip() + "…"
