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

EVENT_PROJECTION_VERSION = "timeline-event-index-v3"
EventType = Literal["SCENE", "ACTION", "DIALOGUE", "EMOTION", "RELATION_CHANGE", "ON_SCREEN_TEXT", "OTHER"]


class EventIndexingContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, revalidate_instances="always")

    semantic_encoder: EncoderContract
    vision_encoder: EncoderContract
    capture: EventFrameContract
    description_language: str | None = Field(default=None, min_length=1, pattern=r"\S")
    projection_version: Literal["timeline-event-index-v3"] = EVENT_PROJECTION_VERSION

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


class EventSemanticSource(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, revalidate_instances="always")

    source: Literal["description", "dialogue"]
    language: str | None = Field(default=None, min_length=1, pattern=r"\S")
    role: Literal["ORIGINAL", "TRANSLATION"] | None = None

    @model_validator(mode="after")
    def validate_source(self) -> EventSemanticSource:
        if self.source == "description" and (self.language is not None or self.role is not None):
            raise ValueError("Event description semantic source has no language or role")
        if self.source == "dialogue" and (self.language is None or self.role is None):
            raise ValueError("Event dialogue semantic source requires language and role")
        return self


class EventSemanticVector(EventSemanticSource):
    vector: tuple[Annotated[float, Field(strict=True, allow_inf_nan=False)], ...]

    @model_validator(mode="after")
    def validate_vector(self) -> EventSemanticVector:
        validate_embedding_vector(self.vector, dimensions=len(self.vector))
        return self


@dataclass(frozen=True, slots=True)
class EventSemanticRequest:
    source: EventSemanticSource
    request: EmbeddingRequest


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
    semantic_vectors: tuple[EventSemanticVector, ...]
    vision_vector: tuple[Annotated[float, Field(strict=True, allow_inf_nan=False)], ...]
    detail: EventIndexDetail

    @model_validator(mode="after")
    def validate_document(self) -> EventIndexDocument:
        if self.event_type != "DIALOGUE" and self.speaker is not None:
            raise ValueError("Only DIALOGUE can have a speaker")
        validate_embedding_vector(self.vision_vector, dimensions=len(self.vision_vector))
        if not self.lexical_fields or not self.detail.capture.frames:
            raise ValueError("Event documents require text and realized capture frames")
        dialogue_fields = [field for field in self.lexical_fields if field.field == "dialogue"]
        if bool(dialogue_fields) != (self.event_type == "DIALOGUE"):
            raise ValueError("Only DIALOGUE Events require dialogue lexical fields")
        expected = [("description", None, None), *(
            ("dialogue", field.language, field.role) for field in dialogue_fields
        )]
        actual = [(item.source, item.language, item.role) for item in self.semantic_vectors]
        keys = [(source, language.casefold() if language is not None else None, role)
                for source, language, role in actual]
        if actual != expected or len(set(keys)) != len(keys):
            raise ValueError("Event semantic sources must match description and dialogue lexical fields")
        if len({len(item.vector) for item in self.semantic_vectors}) != 1:
            raise ValueError("Event semantic vectors must share one dimension")
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
    semantic_requests: tuple[EventSemanticRequest, ...]
    capture_start_ms: int
    capture_end_ms: int
    frame_timestamps_ms: tuple[int, ...]
    contract: EventIndexingContract

    def __post_init__(self) -> None:
        # A copied source/version plan owns every request, like the Chunk index plan.
        object.__setattr__(self, "semantic_requests", tuple(
            replace(item, request=replace(item.request)) for item in self.semantic_requests
        ))

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
        # Encode each text independently; metadata labels never enter model input.
        semantic_requests = [EventSemanticRequest(
            EventSemanticSource(source="description"),
            EmbeddingRequest("semantic", contract.semantic_encoder, event.description),
        )]
        if isinstance(event, DialogueEvent):
            semantic_requests.extend(EventSemanticRequest(
                EventSemanticSource(source="dialogue", language=variant.language, role=variant.role),
                EmbeddingRequest("semantic", contract.semantic_encoder, variant.text),
            ) for variant in event.text_variants)
        start, end = max(chunk_start_ms, event.start_seconds), min(chunk_end_ms, event.end_seconds)
        plans.append(EventIndexPlan(
            parse_task_id=parse_task_id, video_chunk_id=video_chunk_id, version=version,
            timeline_index=index, event_type=event.event_type,
            speaker=event.speaker if isinstance(event, DialogueEvent) else None,
            lexical_fields=lexical,
            semantic_requests=tuple(semantic_requests),
            capture_start_ms=start, capture_end_ms=end,
            frame_timestamps_ms=contract.capture.timestamps(start, end), contract=contract,
        ))
    return tuple(plans)


def finalize_event_index(
    plan: EventIndexPlan, semantic_artifacts: Sequence[EmbeddingArtifact],
    vision_vector: Sequence[float], frames: Sequence[CaptureFrame],
) -> EventIndexDocument:
    if len(semantic_artifacts) != len(plan.semantic_requests):
        raise ValueError("Every Event semantic request requires exactly one artifact")
    semantic = tuple(EventSemanticVector(
        **item.source.model_dump(), vector=validate_embedding_artifact(item.request, artifact),
    ) for item, artifact in zip(plan.semantic_requests, semantic_artifacts, strict=True))
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
        lexical_fields=plan.lexical_fields, semantic_vectors=semantic, vision_vector=vision,
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
    "EventSemanticSource", "EventSemanticVector", "EventSemanticRequest",
    "EventIndexPlan", "EventIndexDocument", "EventIndexReceipt", "EventIndexDetail", "EventCaptureDetail",
    "compute_event_id", "plan_event_index", "finalize_event_index", "build_event_index_receipt",
]
