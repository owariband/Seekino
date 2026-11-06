"""Source-video time contracts and anonymous voice grouping of existing ASR."""
from __future__ import annotations

from copy import deepcopy
import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .observations import ASRResult


class SpeakerTurn(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)
    speaker_id: str = Field(min_length=1)

    @model_validator(mode="after")
    def positive_interval(self):
        if self.end_ms <= self.start_ms or not self.speaker_id.strip():
            raise ValueError("speaker turn requires a positive interval and nonempty label")
        return self


class DiarizationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: Literal[1] = 1
    turns: list[SpeakerTurn]
    exclusive_turns: list[SpeakerTurn]

    def validate_duration(self, duration_ms: int):
        for turns in (self.turns, self.exclusive_turns):
            previous = -1
            for turn in turns:
                if not previous <= turn.start_ms < turn.end_ms <= duration_ms:
                    raise ValueError("speaker turns must be ordered and inside the original video")
                previous = turn.start_ms
        if any(left.end_ms > right.start_ms for left, right in zip(self.exclusive_turns, self.exclusive_turns[1:])):
            raise ValueError("exclusive speaker turns must not overlap")
        labels = {turn.speaker_id for turn in self.turns}
        if any(turn.speaker_id not in labels for turn in self.exclusive_turns):
            raise ValueError("exclusive speaker label is absent from regular diarization")
        return self


def normalize_diarization(payload: dict, *, duration_ms: int) -> dict:
    def turns(key):
        result = []
        for item in payload[key]:
            start, end = item["start"], item["end"]
            if any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
                   for value in (start, end)) or end <= start:
                raise ValueError("pyannote returned invalid speaker timestamps")
            start_ms, end_ms = max(0, round(start*1000)), min(duration_ms, round(end*1000))
            if start_ms < end_ms:
                result.append(SpeakerTurn(start_ms=start_ms, end_ms=end_ms, speaker_id=item["speaker"]))
        return result
    return DiarizationResult(turns=turns("segments"), exclusive_turns=turns("exclusive_segments")).validate_duration(duration_ms).model_dump()


def assign_video_speakers(asr: ASRResult, diarization: dict, *, scope_id: str) -> ASRResult:
    """Preserve original text/times; do not force labels for silence or mixed turns."""
    result = deepcopy(asr)
    turns = DiarizationResult.model_validate(diarization).exclusive_turns
    result.update(speaker_scope="video", speaker_scope_id=scope_id,
                  speaker_ids=sorted({turn.speaker_id for turn in turns}))
    cursor = 0
    for cue in result["transcript"]:
        start, end = cue["start_seconds"], cue["end_seconds"]
        while cursor < len(turns) and turns[cursor].end_ms <= start:
            cursor += 1
        candidates = set()
        for turn in turns[cursor:]:
            if turn.start_ms >= end:
                break
            if turn.end_ms > start:
                candidates.add(turn.speaker_id)
        cue["speaker_id"] = next(iter(candidates)) if len(candidates) == 1 else None
    return result
