"""Pure input contracts and event construction for ASR/OCR-only parsing."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import NotRequired, TypedDict

from pydantic import BaseModel, ConfigDict, Field

from .schemas import DialogueEvent, DialogueTextVariant, OnScreenTextEvent, VideoParseResult


class ASRWord(TypedDict):
    text: str
    start_ms: int | None
    end_ms: int | None
    score: float | None


class ASRCue(TypedDict):
    start_seconds: int
    end_seconds: int
    text: str
    speaker_id: NotRequired[str | None]
    words: NotRequired[list[ASRWord]]


class ASRResult(TypedDict):
    """Normalized transcript with absolute source-video millisecond bounds."""

    transcript: list[ASRCue]
    language: NotRequired[str]


def select_asr_window(result: ASRResult, *, start_ms: int, end_ms: int) -> ASRResult:
    """Select overlapping cues without changing their absolute source times.

    Whole cues supply boundary context; Understanding still writes only inside
    its authorized window. Copies prevent one consumer from changing the cache.
    """
    if type(start_ms) is not int or type(end_ms) is not int or not 0 <= start_ms < end_ms:
        raise ValueError("ASR window must be increasing nonnegative milliseconds")
    selected: ASRResult = {"transcript": [
        deepcopy(cue) for cue in result["transcript"]
        if cue["start_seconds"] < end_ms and cue["end_seconds"] > start_ms
    ]}
    if "language" in result:
        selected["language"] = result["language"]
    return selected


class OCRLine(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start_seconds: int = Field(ge=0, strict=True)
    end_seconds: int = Field(gt=0, strict=True)
    text: str = Field(min_length=1)
    language: str | None = None


class OCRResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lines: list[OCRLine] = Field(default_factory=list)


def build_chunk_without_understanding(
    *,
    asr_result: ASRResult,
    ocr_result: OCRResult | None,
) -> VideoParseResult:
    """Map normalized observations to formal events without inferring people."""

    transcript = (
        asr_result.get("transcript", [])
        if isinstance(asr_result, Mapping)
        else []
    )
    timeline: list[DialogueEvent | OnScreenTextEvent] = []
    for cue in transcript if isinstance(transcript, list) else []:
        if not isinstance(cue, Mapping) or not str(cue.get("text", "")).strip():
            continue
        text = str(cue["text"]).strip()
        timeline.append(DialogueEvent(
            event_type="DIALOGUE",
            start_seconds=int(cue["start_seconds"]),
            end_seconds=int(cue["end_seconds"]),
            description=text,
            participants=[],
            visible_persons=[],
            speaker=None,
            text_variants=[DialogueTextVariant(
                language="und", role="ORIGINAL", text=text
            )],
        ))
    for line in ocr_result.lines if ocr_result is not None else []:
        text = line.text.strip()
        if not text:
            continue
        timeline.append(OnScreenTextEvent(
            event_type="ON_SCREEN_TEXT",
            start_seconds=int(line.start_seconds),
            end_seconds=int(line.end_seconds),
            description=text,
            participants=[],
            visible_persons=[],
            language=(str(line.language) if line.language else None),
            text=text,
        ))
    timeline.sort(key=lambda item: (item.start_seconds, item.end_seconds))
    return VideoParseResult(persons=[], timeline=timeline)
