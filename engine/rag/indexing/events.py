"""Formal Timeline → Event text/capture plans → storage-neutral index documents."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from engine.parsev2.schemas import DialogueEvent, VideoParseResult
from engine.rag._validation import _nonblank, _sha256_json, _validate_bounds
from engine.rag.encoding import (
    EmbeddingArtifact, EmbeddingRequest, EncoderContract, validate_embedding_artifact,
    validate_embedding_vector,
)
from engine.rag.indexing.projection import project_chunk_index
from engine.rag.vision.event_frames import CaptureFrame, EventFrameContract

EVENT_PROJECTION_VERSION = "timeline-event-index-v1"
EventType = Literal["SCENE", "ACTION", "DIALOGUE", "EMOTION", "RELATION_CHANGE", "ON_SCREEN_TEXT", "OTHER"]


class EventIndexingContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, revalidate_instances="always")

    semantic_encoder: EncoderContract
    vision_encoder: EncoderContract
    capture: EventFrameContract
    description_language: str | None = Field(default=None, min_length=1, pattern=r"\S")
    projection_version: Literal["timeline-event-index-v1"] = EVENT_PROJECTION_VERSION

    @property
    def contract_id(self) -> str:
        return "event-index:sha256:" + _sha256_json(self.model_dump(mode="json"))

    def as_dict(self) -> dict[str, object]:
        return self.model_dump(mode="json")


class EventLexicalField(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, revalidate_instances="always")

    field: Literal["event_description", "person_names", "dialogue", "screen_text"]
    raw_text: str = Field(min_length=1, pattern=r"\S")
    language: str | None = Field(default=None, min_length=1)
    role: Literal["ORIGINAL", "TRANSLATION"] | None = None

    @model_validator(mode="after")
    def validate_language_role(self) -> EventLexicalField:
        if self.field == "dialogue" and (self.language is None or self.role is None):
            raise ValueError("Event dialogue requires language and role")
        if self.field != "dialogue" and self.role is not None:
            raise ValueError("Only Event dialogue carries a role")
        return self


class EventCaptureDetail(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, revalidate_instances="always")
    frames: tuple[CaptureFrame, ...]


class EventIndexDetail(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, revalidate_instances="always")
    capture: EventCaptureDetail


class EventIndexDocument(BaseModel):
    """Exactly the agreed Event source fields; the deterministic ID stays external."""

    model_config = ConfigDict(extra="forbid", frozen=True, revalidate_instances="always")

    video_chunk_id: str = Field(min_length=1, pattern=r"\S")
    parse_task_id: str = Field(min_length=1, pattern=r"\S")
    version: int = Field(gt=0, strict=True)
    timeline_index: int = Field(ge=0, strict=True)
    event_type: EventType
    speaker: str | None = Field(default=None, min_length=1, pattern=r"\S")
    lexical_fields: tuple[EventLexicalField, ...]
    semantic_vector: tuple[Annotated[float, Field(strict=True, allow_inf_nan=False)], ...]
    vision_vector: tuple[Annotated[float, Field(strict=True, allow_inf_nan=False)], ...]
    detail: EventIndexDetail

    @model_validator(mode="after")
    def validate_document(self) -> EventIndexDocument:
        if self.event_type != "DIALOGUE" and self.speaker is not None:
            raise ValueError("Only DIALOGUE can have a speaker")
        for vector in (self.semantic_vector, self.vision_vector):
            validate_embedding_vector(vector, dimensions=len(vector))
        if not self.lexical_fields or not self.detail.capture.frames:
            raise ValueError("Event documents require text and realized capture frames")
        return self

    @property
    def event_id(self) -> str:
        return compute_event_id(self.parse_task_id, self.video_chunk_id, self.version, self.timeline_index)

    def as_source(self) -> dict[str, object]:
        return self.model_dump(mode="json", exclude={"speaker"} if self.event_type != "DIALOGUE" else set())


@dataclass(frozen=True, slots=True)
class EventIndexPlan:
    parse_task_id: str
    video_chunk_id: str
    version: int
    timeline_index: int
    event_type: EventType
    speaker: str | None
    lexical_fields: tuple[EventLexicalField, ...]
    semantic_request: EmbeddingRequest
    capture_start_ms: int
    capture_end_ms: int
    frame_timestamps_ms: tuple[int, ...]
    contract: EventIndexingContract

    def __post_init__(self) -> None:
        # A copied source/version plan owns its request, like the Chunk index plan.
        object.__setattr__(self, "semantic_request", replace(self.semantic_request))

    @property
    def event_id(self) -> str:
        return compute_event_id(self.parse_task_id, self.video_chunk_id, self.version, self.timeline_index)


def compute_event_id(parse_task_id: str, video_chunk_id: str, version: int, timeline_index: int) -> str:
    if not _nonblank(parse_task_id) or not _nonblank(video_chunk_id):
        raise ValueError("Event source IDs must be nonblank")
    if type(version) is not int or version <= 0 or type(timeline_index) is not int or timeline_index < 0:
        raise ValueError("Event source version/index must be positive/nonnegative integers")
    return _sha256_json([parse_task_id, video_chunk_id, version, timeline_index])


def plan_event_index(
    parse_task_id: str, video_chunk_id: str, version: int, result: VideoParseResult,
    chunk_start_ms: int, chunk_end_ms: int, contract: EventIndexingContract,
) -> tuple[EventIndexPlan, ...]:
    """Retain original Timeline indices and never rewrite formal Event bounds."""

    compute_event_id(parse_task_id, video_chunk_id, version, 0)
    _validate_bounds(chunk_start_ms, chunk_end_ms)
    contract = EventIndexingContract.model_validate(contract)
    result = VideoParseResult.model_validate(result)
    projection = project_chunk_index(
        result, chunk_start_seconds=chunk_start_ms, chunk_end_seconds=chunk_end_ms,
        description_language=contract.description_language,
    )
    fields: dict[int, list[EventLexicalField]] = {}
    for source in projection.lexical_fields:
        assert source.timeline_index is not None
        fields.setdefault(source.timeline_index, []).append(EventLexicalField(
            field=source.field, raw_text=source.raw_text, language=source.language, role=source.role,
        ))
    plans = []
    for index, event in enumerate(result.timeline):
        if index not in fields:
            continue
        lexical = tuple(fields[index])
        # Only this Event contributes text. IDs, times and Chunk enrichments stay out.
        parts = [event.description]
        parts.extend(
            f"{field.field} ({field.language or 'und'}, {field.role or 'text'}): {field.raw_text}"
            for field in lexical if field.field in ("dialogue", "screen_text")
        )
        start, end = max(chunk_start_ms, event.start_seconds), min(chunk_end_ms, event.end_seconds)
        plans.append(EventIndexPlan(
            parse_task_id=parse_task_id, video_chunk_id=video_chunk_id, version=version,
            timeline_index=index, event_type=event.event_type,
            speaker=event.speaker if isinstance(event, DialogueEvent) else None,
            lexical_fields=lexical,
            semantic_request=EmbeddingRequest("semantic", contract.semantic_encoder, "\n".join(parts)),
            capture_start_ms=start, capture_end_ms=end,
            frame_timestamps_ms=contract.capture.timestamps(start, end), contract=contract,
        ))
    return tuple(plans)


def finalize_event_index(
    plan: EventIndexPlan, semantic_artifact: EmbeddingArtifact,
    vision_vector: Sequence[float], frames: Sequence[CaptureFrame],
) -> EventIndexDocument:
    semantic = validate_embedding_artifact(plan.semantic_request, semantic_artifact)
    vision = validate_embedding_vector(vision_vector, dimensions=plan.contract.vision_encoder.dimensions)
    prepared = tuple(CaptureFrame.model_validate(frame) for frame in frames)
    if tuple(frame.requested_at_ms for frame in prepared) != plan.frame_timestamps_ms:
        raise ValueError("Event capture frames differ from the planned timestamps")
    if any(frame.actual_at_ms is not None and not plan.capture_start_ms <= frame.actual_at_ms < plan.capture_end_ms
           for frame in prepared):
        raise ValueError("Event actual frame time falls outside its capture range")
    return EventIndexDocument(
        video_chunk_id=plan.video_chunk_id, parse_task_id=plan.parse_task_id, version=plan.version,
        timeline_index=plan.timeline_index, event_type=plan.event_type, speaker=plan.speaker,
        lexical_fields=plan.lexical_fields, semantic_vector=semantic, vision_vector=vision,
        detail=EventIndexDetail(capture=EventCaptureDetail(frames=prepared)),
    )


class EventIndexReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, revalidate_instances="always")
    index_name: str = Field(min_length=1, pattern=r"\S")
    contract_id: str = Field(min_length=1)
    event_count: int = Field(ge=0, strict=True)
    event_ids: tuple[Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")], ...]
    event_set_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_set(self) -> EventIndexReceipt:
        if (len(self.event_ids) != self.event_count or len(set(self.event_ids)) != self.event_count
                or tuple(sorted(self.event_ids)) != self.event_ids
                or _sha256_json(list(self.event_ids)) != self.event_set_sha256):
            raise ValueError("Event receipt does not describe its exact sorted ID set")
        return self

    def as_dict(self) -> dict[str, object]:
        return self.model_dump(mode="json")


def build_event_index_receipt(
    index_name: str, contract: EventIndexingContract,
    events: Sequence[EventIndexPlan | EventIndexDocument],
) -> EventIndexReceipt:
    ids = tuple(sorted(event.event_id for event in events))
    return EventIndexReceipt(index_name=index_name, contract_id=contract.contract_id,
                             event_count=len(ids), event_ids=ids, event_set_sha256=_sha256_json(list(ids)))


__all__ = [
    "EVENT_PROJECTION_VERSION", "EventType", "EventLexicalField", "EventIndexingContract",
    "EventIndexPlan", "EventIndexDocument", "EventIndexReceipt", "EventIndexDetail", "EventCaptureDetail",
    "compute_event_id", "plan_event_index", "finalize_event_index", "build_event_index_receipt",
]
