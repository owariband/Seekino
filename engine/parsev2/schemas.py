"""Domain-owned structured output contract for Video Parse V2."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

_NonBlankString = Annotated[str, Field(min_length=1, pattern=r"\S")]


class _ParseV2Model(BaseModel):
    model_config = ConfigDict(extra="forbid", revalidate_instances="always")


class Person(_ParseV2Model):
    """A result-local person identity referenced by timeline events."""

    person_id: _NonBlankString
    label: _NonBlankString


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

    Times are absolute integer seconds and use a half-open interval:
    ``[start_seconds, end_seconds)``.
    """

    start_seconds: int = Field(
        ge=0,
        strict=True,
        description="Inclusive absolute start in integer source-video seconds",
    )
    end_seconds: int = Field(
        gt=0,
        strict=True,
        description="Exclusive absolute end in integer source-video seconds",
    )
    description: _NonBlankString
    participant_person_ids: list[_NonBlankString] = Field(
        description="People who participate semantically in this event"
    )
    visible_person_ids: list[_NonBlankString] = Field(
        description="Relevant people visibly present during this event"
    )

    @model_validator(mode="after")
    def validate_common_invariants(self) -> "TimelineEventBase":
        if self.end_seconds <= self.start_seconds:
            raise ValueError("end_seconds must be greater than start_seconds")
        if len(set(self.participant_person_ids)) != len(self.participant_person_ids):
            raise ValueError("participant_person_ids must not contain duplicates")
        if len(set(self.visible_person_ids)) != len(self.visible_person_ids):
            raise ValueError("visible_person_ids must not contain duplicates")
        return self


class SceneEvent(TimelineEventBase):
    event_type: Literal["SCENE"]


class ActionEvent(TimelineEventBase):
    event_type: Literal["ACTION"]


class DialogueEvent(TimelineEventBase):
    event_type: Literal["DIALOGUE"]
    speaker_person_id: _NonBlankString | None = Field(
        description="Speaker person_id, or null when attribution is unreliable"
    )
    text_variants: list[DialogueTextVariant] = Field(
        min_length=1,
        description=(
            "Language representations of one logical utterance, not competing "
            "ASR or OCR candidates"
        ),
    )

    @model_validator(mode="after")
    def validate_dialogue_invariants(self) -> "DialogueEvent":
        if (
            self.speaker_person_id is not None
            and self.speaker_person_id not in self.participant_person_ids
        ):
            raise ValueError("speaker_person_id must be a participant when set")

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

    persons: list[Person]
    timeline: list[TimelineEvent]

    @model_validator(mode="after")
    def validate_result_invariants(self) -> "VideoParseResult":
        person_ids = [person.person_id for person in self.persons]
        if len(set(person_ids)) != len(person_ids):
            raise ValueError("persons must have unique person_id values")

        known_person_ids = set(person_ids)
        previous_start_seconds = -1
        for event in self.timeline:
            if event.start_seconds < previous_start_seconds:
                raise ValueError("timeline must be ordered by start_seconds")
            previous_start_seconds = event.start_seconds

            referenced_person_ids = (
                event.participant_person_ids + event.visible_person_ids
            )
            if isinstance(event, DialogueEvent) and event.speaker_person_id is not None:
                referenced_person_ids.append(event.speaker_person_id)

            unknown_person_ids = set(referenced_person_ids) - known_person_ids
            if unknown_person_ids:
                unknown = ", ".join(sorted(unknown_person_ids))
                raise ValueError(
                    f"timeline references unknown person_id values: {unknown}"
                )

        return self
