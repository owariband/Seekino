"""Parse V2 facts for source-grounded editing observations and whole-video cards."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)


Text = Annotated[str, Field(min_length=1, max_length=600)]
SegmentID = Annotated[str, Field(pattern=r"^seg-[0-9]{7,}$")]


class PaletteColor(_Model):
    hex: str = Field(pattern=r"^#[0-9a-f]{6}$")
    share: float = Field(ge=0, le=1)


class ReferenceSegment(_Model):
    segment_id: SegmentID
    start_ms: int = Field(ge=0)
    end_ms: int
    first_frame: int = Field(ge=0)
    frame_ms: list[int] = Field(min_length=1, max_length=90)
    fine_starts_ms: list[int]
    flashes_ms: list[int]
    palette: list[PaletteColor] = Field(max_length=4)

    @model_validator(mode="after")
    def measured_clock(self):
        if (self.segment_id != f"seg-{self.start_ms:07d}" or self.frame_ms[0] != self.start_ms
                or self.frame_ms != sorted(set(self.frame_ms)) or self.frame_ms[-1] >= self.end_ms):
            raise ValueError("segment identity and frame timestamps must match its source range")
        if not set(self.fine_starts_ms) <= set(self.frame_ms[1:]) or not set(self.flashes_ms) <= set(self.frame_ms):
            raise ValueError("candidate times must correspond to decoded frames")
        return self


class ShotView(_Model):
    framing: Literal["ECU", "CU", "MCU", "MS", "FULL", "WIDE", "INSERT", "GRAPHIC", "OTHER"]
    subject: Text
    enter: Literal["CUT", "FLASH", "WIPE", "MATCH", "MOVEMENT", "HOLD", "NONE", "UNCERTAIN"]


class Step(ShotView):
    start_ms: int
    end_ms: int


class Look(_Model):
    ground: Text
    subject_treatment: Text
    texture: Text | None
    type_treatment: Text | None


class Motion(_Model):
    steps: list[Step] = Field(min_length=1, max_length=90)
    camera: Text | None
    easing: Text | None


class NoteDescription(_Model):
    name: Text
    summary: Text
    role: Literal["INTRO", "TRANSITION", "ACCENT", "TITLE", "LOOP", "OUTRO"] | None
    look: Look
    slots: list[Text] = Field(max_length=8)
    signals: list[Text] = Field(max_length=8)


class Note(NoteDescription):
    motion: Motion


class TechniqueObservation(_Model):
    segment: ReferenceSegment
    note: Note
    technique_key: Text | None = None

    @model_validator(mode="after")
    def source_steps(self):
        allowed = {*self.segment.fine_starts_ms, self.segment.end_ms}
        cursor = self.segment.start_ms
        for step in self.note.motion.steps:
            if step.start_ms != cursor or step.end_ms <= cursor or step.end_ms not in allowed:
                raise ValueError("steps must tile the measured interval using its candidate boundaries")
            cursor = step.end_ms
        if cursor != self.segment.end_ms:
            raise ValueError("steps must reach the end of the measured interval")
        return self


class ReferenceChunk(_Model):
    observations: list[TechniqueObservation] = Field(max_length=8)

    def validate_owner(self, start_ms, end_ms, duration_ms):
        last_end = start_ms
        for observation in self.observations:
            segment = observation.segment
            if not last_end <= segment.start_ms < end_ms or segment.end_ms > duration_ms:
                raise ValueError("reference segment overlaps, belongs to another chunk or exceeds the source")
            last_end = segment.end_ms
        return self


class TechniqueGroup(_Model):
    technique_key: Text
    description: Text
    segment_ids: list[SegmentID] = Field(min_length=1, max_length=8)


class StyleCard(_Model):
    summary: Text
    techniques: list[TechniqueGroup] = Field(max_length=8)

    def assignments(self, expected_ids):
        keys = {}
        names = set()
        for group in self.techniques:
            if group.technique_key in names:
                raise ValueError("duplicate technique key")
            names.add(group.technique_key)
            for segment_id in group.segment_ids:
                if segment_id in keys:
                    raise ValueError("duplicate segment in whole-video style card")
                keys[segment_id] = group.technique_key
        if set(keys) != set(expected_ids):
            raise ValueError("style card must cover exactly the observed segment IDs")
        return keys
