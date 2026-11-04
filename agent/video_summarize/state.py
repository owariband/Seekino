from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agent.utils.context_canvas import ContextCanvas, canvas_json
from gateway.media import ToolImage, ToolImageArtifact


@dataclass(frozen=True, slots=True)
class SummaryChunk:
    chunk_parse_task_id: str
    sequence_no: int
    version: int
    content: dict[str, Any]


@dataclass(frozen=True, slots=True)
class VideoSummarizeInput:
    video_id: str
    duration_ms: int
    episode_order: int
    background: dict[str, Any]
    chunks: tuple[SummaryChunk, ...]

    def __post_init__(self) -> None:
        if not self.video_id.strip() or type(self.duration_ms) is not int or self.duration_ms <= 0:
            raise ValueError("summarize requires a video identity and positive duration_ms")
        if type(self.episode_order) is not int or self.episode_order <= 0:
            raise ValueError("episode_order must be a positive integer")
        if (not self.chunks or len({item.chunk_parse_task_id for item in self.chunks}) != len(self.chunks)
                or sorted(item.sequence_no for item in self.chunks) != list(range(len(self.chunks)))):
            raise ValueError("summarize requires all uniquely identified, contiguous video chunks")
        if any(not item.chunk_parse_task_id.strip() or type(item.version) is not int or item.version < 1
               or not isinstance(item.content, dict) for item in self.chunks):
            raise ValueError("summary chunks require committed content and positive versions")


@dataclass(frozen=True, slots=True)
class VideoSummarizeLimits:
    maximum_result_bytes: int
    maximum_read_chars: int
    maximum_catalog_items: int
    maximum_digest_chars: int

    def __post_init__(self) -> None:
        if any(type(value) is not int or value < 1 for value in (
            self.maximum_result_bytes, self.maximum_read_chars, self.maximum_catalog_items,
            self.maximum_digest_chars,
        )):
            raise ValueError("summarize limits must be positive integers supplied by the host")


def _digest_event_line(event: dict[str, Any]) -> str | None:
    """One deterministic digest line for a stored timeline event, or None when nothing renders."""
    event_type = event.get("event_type")
    if not isinstance(event_type, str) or not event_type.strip():
        return None
    start, end = event.get("start_seconds"), event.get("end_seconds")
    line = f"{start}-{end} {event_type}" if type(start) is int and type(end) is int else event_type
    description = event.get("description")
    if isinstance(description, str) and description:
        line = f"{line}: {description}"
    if event_type == "ON_SCREEN_TEXT":
        text = event.get("text")
        if isinstance(text, str) and text:
            line = f"{line} | text: {text}"
    return line


def build_chunk_digest(chunks: tuple[SummaryChunk, ...], maximum_digest_chars: int) -> str:
    """Host-built plain-text digest of every chunk's timeline, truncated deterministically.

    Every chunk header and ON_SCREEN_TEXT line is kept unless the limit forces dropping
    ON_SCREEN_TEXT from the end; other event lines are dropped evenly when truncating.
    """
    lines: list[str] = []
    required: list[bool] = []
    for chunk in sorted(chunks, key=lambda item: item.sequence_no):
        lines.append(f"## chunk {chunk.sequence_no} ref={chunk.chunk_parse_task_id}")
        required.append(True)
        for event in chunk.content.get("timeline", ()):
            if not isinstance(event, dict):
                continue
            line = _digest_event_line(event)
            if line is None:
                continue
            lines.append(line)
            required.append(event.get("event_type") == "ON_SCREEN_TEXT")
    return _assemble_digest(lines, required, maximum_digest_chars)


def _assemble_digest(lines: list[str], required: list[bool], maximum_digest_chars: int) -> str:
    if len("\n".join(lines)) <= maximum_digest_chars:
        return "\n".join(lines)

    def assemble(indices: list[int], omitted: int) -> str:
        body = "\n".join(lines[index] for index in indices)
        return f"{body}\n[digest omitted {omitted} event lines to fit the limit]" if omitted else body

    required_indices = [index for index, flag in enumerate(required) if flag]
    optional_indices = [index for index, flag in enumerate(required) if not flag]

    # Keep every k-th droppable line globally until the result fits the limit.
    for step in range(2, len(optional_indices) + 1):
        kept = {optional_indices[position] for position in range(0, len(optional_indices), step)}
        candidate = assemble(sorted(required_indices + list(kept)), len(optional_indices) - len(kept))
        if len(candidate) <= maximum_digest_chars:
            return candidate

    # Drop all droppable lines; fall back to dropping ON_SCREEN_TEXT from the end.
    candidate = assemble(list(required_indices), len(optional_indices))
    if len(candidate) <= maximum_digest_chars:
        return candidate

    header_indices = [index for index in required_indices if lines[index].startswith("## chunk ")]
    screen_indices = [index for index in required_indices if index not in header_indices]
    for dropped in range(1, len(screen_indices) + 1):
        candidate = assemble(sorted(header_indices + screen_indices[:-dropped]), len(optional_indices) + dropped)
        if len(candidate) <= maximum_digest_chars:
            return candidate
    return assemble(sorted(header_indices), len(optional_indices) + len(screen_indices))


class FrameReference(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    inspection_id: str = Field(min_length=1)
    frame_index: int = Field(ge=0, strict=True)


class SummaryEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    field: Literal["title", "description", "episode_order", "cover"]
    chunk_parse_task_id: str | None = Field(default=None, min_length=1)
    inspection_id: str | None = Field(default=None, min_length=1)
    frame_index: int | None = Field(default=None, ge=0, strict=True)
    quote: str = Field(min_length=1, description="Exact source text, or a concrete observation of the inspected frame.")

    @model_validator(mode="after")
    def one_source(self) -> Self:
        if self.chunk_parse_task_id is not None:
            if self.inspection_id is not None or self.frame_index is not None:
                raise ValueError("evidence must reference one chunk or one inspected frame")
        elif self.inspection_id is None or self.frame_index is None:
            raise ValueError("frame evidence requires inspection_id and frame_index")
        return self


class VideoSummarySubmission(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    title: str | None = Field(default=None, min_length=1, max_length=255)
    title_kind: Literal["official", "generated"] = Field(
        default="official",
        description="Use official for an explicit title card; generated for a concise content-based fallback when no official title is found.",
    )
    description: str = Field(min_length=1)
    episode_order: int | None = Field(default=None, gt=0, le=2_147_483_647, strict=True)
    cover: FrameReference | None = None
    evidence: list[SummaryEvidence] = Field(min_length=1)

    @model_validator(mode="after")
    def generated_title_is_short(self) -> Self:
        if self.title_kind == "generated" and (self.title is None or len(self.title) > 80):
            raise ValueError("a generated title must contain 1 to 80 characters")
        return self


class SummaryCompletion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    completed: Literal[True]


@dataclass(frozen=True, slots=True)
class VideoSummaryResult:
    submission: VideoSummarySubmission
    cover_image: ToolImage | None
    frame_receipts: tuple[dict[str, Any], ...] = ()

    @property
    def title(self) -> str | None:
        return self.submission.title

    @property
    def description(self) -> str:
        return self.submission.description

    @property
    def episode_order(self) -> int | None:
        return self.submission.episode_order

    @property
    def cover(self) -> FrameReference | None:
        return self.submission.cover

    @property
    def evidence(self) -> list[SummaryEvidence]:
        return self.submission.evidence


class SummaryCanvas(ContextCanvas):
    """Frozen input plus execution-local host frame receipts."""

    def __init__(self, inputs: VideoSummarizeInput) -> None:
        super().__init__(f"video-summary:{inputs.video_id}")
        for chunk in sorted(inputs.chunks, key=lambda item: item.sequence_no):
            self.put("chunks", chunk.chunk_parse_task_id, {
                "sequence_no": chunk.sequence_no, "version": chunk.version, "content": chunk.content,
            }, readonly=True)
        self.put("background", "video", inputs.background, readonly=True)
        self.inspections: dict[str, tuple[dict[str, Any], ToolImageArtifact]] = {}
        self.result: VideoSummaryResult | None = None

    def catalog(self, collection: str, *, role: str) -> list[dict[str, Any]]:
        return [{"reference": reference, **(
            {"sequence_no": value["sequence_no"], "version": value["version"]}
            if collection == "chunks" else {}
        )} for reference, value in self.items(collection)]

    def sections(self, collection: str, reference: str, *, role: str) -> dict[str, str]:
        value = self.get(collection, reference)
        return {"content": canvas_json(value["content"] if collection == "chunks" else value)}
