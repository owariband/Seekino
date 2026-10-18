"""Domain-owned structured output contract for Video Parse V2."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_serializer, model_validator

_NonBlankString = Annotated[str, Field(min_length=1, pattern=r"\S")]


class _ParseV2Model(BaseModel):
    model_config = ConfigDict(extra="forbid", revalidate_instances="always")


class DialogueTextVariant(_ParseV2Model):
    """One language representation of the same logical utterance."""

    language: _NonBlankString = Field(
        description="BCP 47 language tag for this text, such as ja or zh-Hans"
    )
    role: Literal["ORIGINAL", "TRANSLATION"] = Field(
        description="Whether this is source-language wording or a translation"
    )
    text: _NonBlankString = Field(
        description="Verbatim wording in the declared language"
    )


class TimelineEventBase(_ParseV2Model):
    """Fields shared by every sparse event on the source-video timeline.

    Times are absolute integer milliseconds and use a half-open interval:
    ``[start_seconds, end_seconds)``. Existing field names remain unchanged.
    """

    start_seconds: int = Field(
        ge=0,
        strict=True,
        description="Inclusive absolute start in integer source-video milliseconds",
    )
    end_seconds: int = Field(
        gt=0,
        strict=True,
        description="Exclusive absolute end in integer source-video milliseconds",
    )
    description: _NonBlankString
    participants: list[_NonBlankString] = Field(
        description="People who participate semantically in this event"
    )
    visible_persons: list[_NonBlankString] = Field(
        description="Relevant people visibly present during this event"
    )

    @model_validator(mode="after")
    def validate_common_invariants(self) -> "TimelineEventBase":
        if self.end_seconds <= self.start_seconds:
            raise ValueError("end_seconds must be greater than start_seconds")
        if len(set(self.participants)) != len(self.participants):
            raise ValueError("participants must not contain duplicates")
        if len(set(self.visible_persons)) != len(self.visible_persons):
            raise ValueError("visible_persons must not contain duplicates")
        return self


class SceneEvent(TimelineEventBase):
    event_type: Literal["SCENE"]


class ActionEvent(TimelineEventBase):
    event_type: Literal["ACTION"]


class DialogueEvent(TimelineEventBase):
    event_type: Literal["DIALOGUE"]
    speaker: _NonBlankString | None = Field(
        description="Speaker name, or null when attribution is unreliable"
    )
    speaker_source: Literal["understanding", "voice_default", "refine_edit"] | None = Field(
        default=None,
        description="Program-recorded origin of the final speaker, including null",
    )
    text_variants: list[DialogueTextVariant] = Field(
        min_length=1,
        description=(
            "Language representations of one logical utterance, not competing "
            "ASR or OCR candidates"
        ),
    )

    @model_serializer(mode="wrap")
    def serialize_speaker_source(self, handler):
        values = handler(self)
        if self.speaker_source is None:
            values.pop("speaker_source", None)
        return values

    @model_validator(mode="after")
    def validate_dialogue_invariants(self) -> "DialogueEvent":
        if (
            self.speaker is not None
            and self.speaker not in self.participants
        ):
            raise ValueError("speaker must be a participant when set")

        variant_keys = [
            (variant.language.casefold(), variant.role)
            for variant in self.text_variants
        ]
        if len(set(variant_keys)) != len(variant_keys):
            raise ValueError("text_variants must have unique language and role pairs")
        return self


class EmotionEvent(TimelineEventBase):
    event_type: Literal["EMOTION"]


class RelationChangeEvent(TimelineEventBase):
    event_type: Literal["RELATION_CHANGE"]


class OnScreenTextEvent(TimelineEventBase):
    """Non-dialogue text in the scene; ordinary dialogue subtitles stay Dialogue."""

    event_type: Literal["ON_SCREEN_TEXT"]
    language: _NonBlankString | None = Field(
        description="BCP 47 language tag, or null when it cannot be determined"
    )
    text: _NonBlankString


class OtherEvent(TimelineEventBase):
    event_type: Literal["OTHER"]


TimelineEvent = Annotated[
    SceneEvent
    | ActionEvent
    | DialogueEvent
    | EmotionEvent
    | RelationChangeEvent
    | OnScreenTextEvent
    | OtherEvent,
    Field(discriminator="event_type"),
]


class VideoParseResult(_ParseV2Model):
    """Canonical Video Parse V2 result before projection and persistence."""

    persons: list[_NonBlankString]
    timeline: list[TimelineEvent]

    @model_validator(mode="after")
    def validate_result_invariants(self) -> "VideoParseResult":
        if len(set(self.persons)) != len(self.persons):
            raise ValueError("persons must have unique names")

        known_persons = set(self.persons)
        previous_start_seconds = -1
        for event in self.timeline:
            if event.start_seconds < previous_start_seconds:
                raise ValueError("timeline must be ordered by start_seconds")
            previous_start_seconds = event.start_seconds

            # Non-dialogue observations may have uncertain person attribution.
            if not isinstance(event, DialogueEvent):
                continue

            referenced_persons = event.participants + event.visible_persons
            if event.speaker is not None:
                referenced_persons.append(event.speaker)

            unknown_persons = set(referenced_persons) - known_persons
            if unknown_persons:
                unknown = ", ".join(sorted(unknown_persons))
                raise ValueError(
                    f"timeline references unknown person names: {unknown}"
                )

        return self
