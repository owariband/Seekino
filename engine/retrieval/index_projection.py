"""Pure Parse V2 projection for one fixed VideoChunk's search representations."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from engine.parsev2.schemas import DialogueEvent, OnScreenTextEvent, VideoParseResult

INDEX_PROJECTION_VERSION = "parse-v2-chunk-index-v1"

_NonBlankString = Annotated[str, Field(min_length=1, pattern=r"\S")]
LexicalFieldName = Literal[
    "dialogue",
    "screen_text",
    "person_names",
    "event_description",
    "keywords",
    "questions",
    "summary",
]
_EVENT_FIELDS = frozenset({"dialogue", "screen_text", "person_names", "event_description"})

# Initial field priorities; evaluate them against real multilingual search cases.
LEXICAL_FIELD_WEIGHTS: dict[str, float] = {
    "dialogue": 3.0,
    "screen_text": 3.0,
    "person_names": 2.0,
    "event_description": 1.0,
    "keywords": 2.0,
    "questions": 1.0,
    "summary": 1.0,
}
LEXICAL_PHRASE_FIELDS = ("dialogue", "screen_text")


class _ProjectionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", revalidate_instances="always")


class SourceEnrichment(_ProjectionModel):
    """Optional, Chunk-owned text already generated from formal parse facts."""

    language: _NonBlankString
    summary: _NonBlankString | None = None
    keywords: list[_NonBlankString] = Field(default_factory=list)
    questions: list[_NonBlankString] = Field(default_factory=list)


class LexicalField(_ProjectionModel):
    """One searchable text with either a Timeline or a Chunk enhancement source."""

    field: LexicalFieldName
    raw_text: _NonBlankString
    language: _NonBlankString | None = None
    role: Literal["ORIGINAL", "TRANSLATION"] | None = None
    speaker: _NonBlankString | None = None
    start_seconds: int = Field(ge=0, strict=True)
    end_seconds: int = Field(gt=0, strict=True)
    timeline_index: int | None = Field(default=None, ge=0, strict=True)
    enrichment_index: int | None = Field(default=None, ge=0, strict=True)

    @model_validator(mode="after")
    def validate_source(self) -> "LexicalField":
        if self.end_seconds <= self.start_seconds:
            raise ValueError("end_seconds must be greater than start_seconds")
        if self.field in _EVENT_FIELDS:
            if self.timeline_index is None or self.enrichment_index is not None:
                raise ValueError("event fields require only timeline_index")
        elif self.enrichment_index is None or self.timeline_index is not None:
            raise ValueError("enrichment fields require only enrichment_index")
        if self.field == "dialogue" and (self.language is None or self.role is None):
            raise ValueError("dialogue requires language and role")
        if self.field not in _EVENT_FIELDS and self.language is None:
            raise ValueError("enrichment fields require language")
        if self.field != "dialogue" and (self.role is not None or self.speaker is not None):
            raise ValueError("role and speaker belong only to dialogue")
        return self


class ChunkIndexProjection(_ProjectionModel):
    """Search text for a Chunk before the Engine joins identity and vectors."""

    lexical_fields: list[LexicalField]
    semantic_text: str


def project_chunk_index(
    result: VideoParseResult,
    *,
    chunk_start_seconds: int,
    chunk_end_seconds: int,
    description_language: str | None = None,
    enrichments: Sequence[SourceEnrichment] = (),
) -> ChunkIndexProjection:
    """Project events overlapping one half-open Chunk, retaining full Timeline indices."""

    if (
        isinstance(chunk_start_seconds, bool)
        or not isinstance(chunk_start_seconds, int)
        or chunk_start_seconds < 0
        or isinstance(chunk_end_seconds, bool)
        or not isinstance(chunk_end_seconds, int)
        or chunk_end_seconds <= chunk_start_seconds
    ):
        raise ValueError("Chunk bounds must be increasing nonnegative integer seconds")
    if description_language is not None and (
        not isinstance(description_language, str) or not description_language.strip()
    ):
        raise ValueError("description_language must be nonblank when provided")

    lexical_fields: list[LexicalField] = []
    semantic_parts: list[str] = []
    for timeline_index, event in enumerate(result.timeline):
        if event.start_seconds >= chunk_end_seconds or event.end_seconds <= chunk_start_seconds:
            continue

        source = {
            "start_seconds": event.start_seconds,
            "end_seconds": event.end_seconds,
            "timeline_index": timeline_index,
        }
        lexical_fields.append(LexicalField(
            field="event_description",
            raw_text=event.description,
            language=description_language,
            **source,
        ))
        event_parts = [f"[{event.start_seconds},{event.end_seconds}) {event.description}"]

        # Each name keeps its own event source, including an off-screen speaker.
        names = list(dict.fromkeys(
            ([event.speaker] if isinstance(event, DialogueEvent) and event.speaker else [])
            + event.participants
            + event.visible_persons
        ))
        for name in names:
            lexical_fields.append(LexicalField(
                field="person_names",
                raw_text=name,
                **source,
            ))
        if names:
            event_parts.append("人物：" + "、".join(names))

        if isinstance(event, DialogueEvent):
            for variant in event.text_variants:
                lexical_fields.append(LexicalField(
                    field="dialogue",
                    raw_text=variant.text,
                    language=variant.language,
                    role=variant.role,
                    speaker=event.speaker,
                    **source,
                ))
                event_parts.append(
                    f"对白（{variant.language}, {variant.role}）：{variant.text}"
                )
        elif isinstance(event, OnScreenTextEvent):
            lexical_fields.append(LexicalField(
                field="screen_text",
                raw_text=event.text,
                language=event.language,
                **source,
            ))
            event_parts.append("画面文字：" + event.text)
        semantic_parts.append("；".join(event_parts))

    for enrichment_index, enrichment in enumerate(enrichments):
        source = {
            "language": enrichment.language,
            "start_seconds": chunk_start_seconds,
            "end_seconds": chunk_end_seconds,
            "enrichment_index": enrichment_index,
        }
        if enrichment.summary is not None:
            lexical_fields.append(LexicalField(
                field="summary", raw_text=enrichment.summary, **source
            ))
            semantic_parts.append(f"摘要（{enrichment.language}）：{enrichment.summary}")
        for field_name, values in (
            ("keywords", enrichment.keywords),
            ("questions", enrichment.questions),
        ):
            for value in dict.fromkeys(values):
                lexical_fields.append(LexicalField(
                    field=field_name, raw_text=value, **source
                ))

    return ChunkIndexProjection(
        lexical_fields=lexical_fields,
        semantic_text="\n".join(semantic_parts),
    )


def compute_index_projection_revision(projection: ChunkIndexProjection) -> str:
    """Hash the complete text projection and its contract, independent of storage."""

    payload = {
        "projection_version": INDEX_PROJECTION_VERSION,
        "lexical_fields": [field.model_dump(mode="json") for field in projection.lexical_fields],
        "semantic_text": projection.semantic_text,
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
