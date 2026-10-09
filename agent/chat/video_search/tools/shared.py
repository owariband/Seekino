from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass, field
import json
from threading import Lock
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, StringConstraints

from engine.rag.contracts import VideoChunkSearchHit

from ...common.errors import AgentModelInvocationError, AgentToolFeedbackError
from ...common.state import ExecutionControl
from ...common.tool_output import bounded_json, range_text
from ..context import detail_sections as _detail_sections
from ..state import DraftItem
from ..video_search_canvas import VideoSearchContextCanvas

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
    revision: int | None = None
    catalog_references: set[str] = field(default_factory=set)
    catalog_total: int | None = None
    _lock: Lock = field(default_factory=Lock, init=False, repr=False)

    def record(self, payload: dict[str, Any]) -> None:
        if payload.get("collection") != "draft":
            return
        with self._lock:
            revision = payload["revision"]
            if self.revision != revision:
                self.revision = revision
                self.catalog_complete = False
                self.catalog_references.clear()
                self.catalog_total = None
                self.expected.clear()
                self.ranges.clear()
            reference = payload.get("reference")
            if reference is None:
                self.catalog_total = payload["total"]
                self.catalog_references.update(item["reference"] for item in payload["items"])
                if payload["next_cursor"] is None:
                    self.catalog_complete = True
            elif payload.get("section") == "overview":
                lengths = {item["name"]: item["total_chars"] for item in payload["sections"]}
                if self.expected.get(reference) != lengths:
                    self.expected[reference] = lengths
                    self.ranges[reference] = {name: [] for name in lengths}
            else:
                section = payload["section"]
                if section in self.ranges.get(reference, {}):
                    self.ranges[reference][section].append((payload["start"], payload["end"]))

    def mark_catalog_complete(self) -> None:
        # Compatibility for unregistered legacy readers.
        with self._lock:
            self.catalog_complete = True

    def mark_overview(self, video_chunk_id: str, sections: dict[str, str]) -> None:
        with self._lock:
            lengths = {name: len(content) for name, content in sections.items()}
            if self.expected.get(video_chunk_id) != lengths:
                self.expected[video_chunk_id] = lengths
                self.ranges[video_chunk_id] = {name: [] for name in lengths}

    def mark_range(self, video_chunk_id: str, section: str, start: int, end: int) -> None:
        with self._lock:
            ranges = self.ranges.get(video_chunk_id)
            if ranges is not None and section in ranges:
                ranges[section].append((start, end))

    def covers(self, video_chunk_ids: Collection[str], *, revision: int | None = None) -> bool:
        ids = set(video_chunk_ids)
        with self._lock:
            if revision is not None and (
                self.revision != revision or self.catalog_total != len(ids)
                or self.catalog_references != ids
            ):
                return False
            return self.catalog_complete and all(
                reference in self.expected and all(
                    _ranges_cover(self.ranges[reference][section], total_chars)
                    for section, total_chars in self.expected[reference].items()
                )
                for reference in ids
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
        "start_seconds": hit.evidence.start_seconds,
        "end_seconds": hit.evidence.end_seconds,
        "description": _bounded_text(hit.selection_reason.summary, 500),
        "fused_rank": hit.candidate.fused_rank,
        "fused_score": hit.candidate.fused_score,
        "lexical_rank": hit.candidate.lexical_rank,
        "transcript_rank": hit.candidate.transcript_rank,
        "semantic_rank": hit.candidate.text_rank,
        "visual_rank": hit.candidate.vision_rank,
    }


def _draft_card(canvas: VideoSearchContextCanvas, item: DraftItem) -> dict[str, Any]:
    evidence = canvas.retrieval_item(item.video_chunk_id).evidence
    return {
        "video_chunk_id": item.video_chunk_id,
        "episode_order": evidence.episode_order,
        "start_seconds": item.start_seconds,
        "end_seconds": item.end_seconds,
        "description": _bounded_text(item.claim, 500),
    }


def _bounded_text(value: str, maximum_chars: int) -> str:
    if len(value) <= maximum_chars:
        return value
    return value[: maximum_chars - 1].rstrip() + "…"
