"""Parse V2 facts for source-grounded editing observations and whole-video cards."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_serializer, model_validator


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)


Text = Annotated[str, Field(min_length=1)]
SegmentID = Annotated[str, Field(pattern=r"^seg-[0-9]{7,}$")]
Role = Text


class PaletteColor(_Model):
    hex: str = Field(pattern=r"^#[0-9a-f]{6}$")
    share: float = Field(ge=0, le=1)


class ReferenceSegment(_Model):
    segment_id: SegmentID
    start_ms: int = Field(ge=0)
    end_ms: int
    first_frame: int = Field(ge=0)
    fps: float | None = Field(default=None, gt=0, exclude_if=lambda value: value is None)
    frame_ms: list[int] = Field(min_length=1, max_length=90)
    frame_delta: list[Annotated[float, Field(ge=0, le=255)]] | None = None
    fine_starts_ms: list[int]
    flashes_ms: list[int]
    palette: list[PaletteColor] = Field(max_length=4)

    @model_serializer(mode="wrap")
    def serialize_measured(self, handler):
        values = handler(self)
        if self.frame_delta is None:
            values.pop("frame_delta", None)  # preserve already-published observation fingerprints
        return values

    @model_validator(mode="after")
    def measured_clock(self):
        if self.frame_delta is not None and len(self.frame_delta) != len(self.frame_ms):
            raise ValueError("each frame must keep its own measured change")
        if (self.segment_id != f"seg-{self.start_ms:07d}" or self.frame_ms[0] != self.start_ms
                or self.frame_ms != sorted(set(self.frame_ms)) or self.frame_ms[-1] >= self.end_ms):
            raise ValueError("segment identity and frame timestamps must match its source range")
        if not set(self.fine_starts_ms) <= set(self.frame_ms[1:]) or not set(self.flashes_ms) <= set(self.frame_ms):
            raise ValueError("candidate times must correspond to decoded frames")
        return self


class ShotView(_Model):
    framing: Text
    subject: Text | None
    enter: Text


class Step(ShotView):
    start_ms: int
    end_ms: int


class Look(_Model):
    ground: Text
    subject_treatment: Text
    texture: Text | None
    type_treatment: Text | None


class Motion(_Model):
    steps: list[Step]
    camera: Text | None
    easing: Text | None
    accent: Text | None = Field(default=None, exclude_if=lambda value: value is None)


class Slot(_Model):
    name: Text
    kind: Literal["image", "text", "video"]
    requirement: Text


class NoteDescription(_Model):
    name: Text
    summary: Text
    role: Role | None
    look: Look
    slots: list[Slot | Text]
    signals: list[Text]


class Note(NoteDescription):
    motion: Motion


class PickNote(Note):
    """Model observations use the same material slots as stored notes."""


class TechniqueObservation(_Model):
    segment: ReferenceSegment
    note: Note
    technique_key: Text | None = None

    @model_validator(mode="after")
    def source_steps(self):
        for step in self.note.motion.steps:
            if not self.segment.start_ms <= step.start_ms < step.end_ms <= self.segment.end_ms:
                raise ValueError("steps must stay inside the measured source interval")
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
    """First published format, retained for reading existing observations."""
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


class SignalExample(_Model):
    segment_id: SegmentID
    at_ms: int = Field(ge=0)
    detail: Text


class StyleSignal(_Model):
    name: Text
    description: Text
    examples: list[SignalExample] = Field(min_length=1)


class AnchoredStyleCard(_Model):
    scope: Text
    signals: list[StyleSignal]

    def validate_sources(self, observations):
        ranges = {o.segment.segment_id: (o.segment.start_ms, o.segment.end_ms) for o in observations}
        for signal in self.signals:
            for example in signal.examples:
                bounds = ranges.get(example.segment_id)
                if bounds is None or not bounds[0] <= example.at_ms < bounds[1]:
                    raise ValueError("style signal must cite a position inside its own segment")
        return self


class RefinedObservation(_Model):
    segment_id: SegmentID
    technique_key: Text | None
    name: Text
    summary: Text
    role: Role | None
    camera: Text | None

    def apply(self, original: TechniqueObservation) -> TechniqueObservation:
        if original.segment.segment_id != self.segment_id:
            raise ValueError("refined observation must match its source")
        fields = self.model_dump(include={"name", "summary", "role"})
        fields["motion"] = original.note.motion.model_copy(update={"camera": self.camera})
        return original.model_copy(update={"technique_key": self.technique_key,
                                           "note": original.note.model_copy(update=fields)})


class RefinedMember(_Model):
    sequence_no: int = Field(ge=0, lt=63)
    observations: list[RefinedObservation]


class ReferenceRefinement(_Model):
    members: list[RefinedMember] = Field(max_length=63)
    style_card: AnchoredStyleCard

    def for_sources(self, members: dict[int, ReferenceChunk]):
        """The host owns membership. A model can improve a note, never move, add or drop its source."""
        updates = [note for member in self.members for note in member.observations]
        by_id = {note.segment_id: note for note in updates}
        originals = [o for chunk in members.values() for o in chunk.observations]
        source_ids = {o.segment.segment_id for o in originals}
        if set(by_id) - source_ids:
            raise ValueError("Refine references source segments that were not observed")
        self.style_card.validate_sources(originals)
        host_members = []
        for seq, chunk in sorted(members.items()):
            observations = []
            for original in chunk.observations:
                update = by_id.get(original.segment.segment_id)
                if update is None:
                    update = RefinedObservation(
                        segment_id=original.segment.segment_id,
                        technique_key=original.technique_key,
                        name=original.note.name,
                        summary=original.note.summary,
                        role=original.note.role,
                        camera=original.note.motion.camera,
                    )
                observations.append(update)
            host_members.append(RefinedMember(sequence_no=seq, observations=observations))
        return self.model_copy(update={"members": host_members})
