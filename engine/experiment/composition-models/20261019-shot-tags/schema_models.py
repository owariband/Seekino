"""Strict VLM payload validation for shot-composition-draft-v1.

Only the five VLM groups belong in a response. Identity, provenance, detector
and PCP are added by the experiment after validation, never asked of the VLM.
No validators normalize or repair values.
"""
from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SCHEMA_VERSION = "shot-composition-draft-v1"
PROMPT_VERSION = "shot-tag-v1"
CompositionLabel = Literal["center", "rule_of_thirds", "symmetric", "horizontal", "vertical", "diagonal", "triangle", "curve", "pattern", "vanishing_point", "frame_within_frame", "fill_frame", "none_clear"]
Cell = Literal["TL", "TC", "TR", "ML", "MC", "MR", "BL", "BC", "BR"]
Clarity = Literal["clear", "ambiguous"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    @model_validator(mode="after")
    def unique_enum_lists(self):
        for name in type(self).model_fields:
            value = getattr(self, name)
            if isinstance(value, list) and all(isinstance(x, str) for x in value):
                if len(value) != len(set(value)):
                    raise ValueError(f"{name} must not contain duplicate values")
        return self


class Camera(StrictModel):
    shot_size: Literal["extreme_close_up", "close_up", "medium_close_up", "medium", "medium_long", "full", "long", "extreme_long", "insert_detail", "unknown"]
    vertical_angle: Literal["overhead", "high", "eye_level", "low", "worm", "unknown"]
    roll: Literal["level", "dutch", "unknown"]
    framing_device: list[Literal["over_the_shoulder", "pov", "through_frame", "reflection", "screen_in_screen"]] = Field(max_length=2)
    clarity: Clarity


class NegativeSpace(StrictModel):
    amount: Literal["none", "some", "large"]
    sides: list[Literal["left", "right", "top", "bottom"]] = Field(max_length=4)


class Composition(StrictModel):
    primary: CompositionLabel
    secondary: list[CompositionLabel] = Field(max_length=2)
    dominant_lines: list[Literal["horizontal", "vertical", "diagonal_rising", "diagonal_falling", "converging", "curved"]] = Field(max_length=2)
    depth_structure: Literal["flat", "layered", "deep"]
    foreground_element: bool
    negative_space: NegativeSpace
    clarity: Clarity

    @model_validator(mode="after")
    def secondary_excludes_primary(self):
        if self.primary in self.secondary:
            raise ValueError("composition.secondary must exclude composition.primary")
        return self


class Person(StrictModel):
    cell: Cell
    facing: Literal["camera", "left", "right", "away", "three_quarter_left", "three_quarter_right"]
    visible_extent: Literal["face", "bust", "waist_up", "knee_up", "full_body", "partial"]


class Subjects(StrictModel):
    main_subject_kind: Literal["person", "group", "object", "environment", "text"]
    main_subject_cell: Cell
    main_subject_scale: Literal["tiny", "small", "medium", "large", "dominant"]
    person_count: Annotated[int, Field(ge=0, le=6)] | Literal["many"]
    partial_people: int = Field(ge=0)
    persons: list[Person] = Field(max_length=4)
    arrangement: Literal["single", "side_by_side", "front_back", "facing_each_other", "back_to_back", "row", "scattered", "none"]
    clarity: Clarity


class Frame(StrictModel):
    kind: Literal["story", "title_card", "credits", "text_screen", "black_or_flash", "transition"]
    style_flags: list[Literal["chibi", "monochrome_or_filtered", "split_screen", "heavy_blur", "screen_content", "speed_lines"]] = Field(max_length=6)
    burned_subtitles: bool


class Content(StrictModel):
    setting: Literal["interior", "exterior", "stage", "vehicle", "abstract", "unknown"]
    location_hint: str

    @field_validator("location_hint")
    @classmethod
    def lowercase_english_words(cls, value: str) -> str:
        if value and not re.fullmatch(r"[a-z]+(?:[-'][a-z]+)*(?: [a-z]+(?:[-'][a-z]+)*)*", value):
            raise ValueError("location_hint must contain lowercase English words separated by spaces")
        if len(value.split()) > 6:
            raise ValueError("location_hint must contain at most six words")
        return value


class ShotTag(StrictModel):
    camera: Camera
    composition: Composition
    subjects: Subjects
    frame: Frame
    content: Content


def count_bucket(value: int | str) -> str:
    """Brief's five reporting buckets: its omitted count 6 belongs to many (6+).

    The original schema value stays exact (6); this mapping is only for metrics
    and ranking. The VLM's literal 'many' itself still means strictly more than 6.
    """
    if value == "many" or int(value) >= 6:
        return "many"
    value = int(value)
    return str(value) if value < 3 else "3–5"


def count_agreement(vlm_count: int | str, detector_count: int) -> str:
    if vlm_count == "many":
        return "agree" if detector_count > 6 else "vlm_higher"
    return "agree" if vlm_count == detector_count else "vlm_higher" if vlm_count > detector_count else "detector_higher"
