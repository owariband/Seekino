from __future__ import annotations

import math
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_serializer, model_validator

from gateway.multimodal import MultimodalModel, TextInput, VideoInput

from .media_probe import probe_video_duration_seconds


class TranscriptWord(BaseModel):
    """Aligned lexical unit (a character for unspaced languages), in milliseconds."""

    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1)
    start_ms: int | None = Field(default=None, ge=0)
    end_ms: int | None = Field(default=None, ge=0)
    score: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_bounds(self) -> "TranscriptWord":
        if (self.start_ms is None) != (self.end_ms is None):
            raise ValueError("word timestamps must both be known or both be null")
        if self.start_ms is not None and self.end_ms <= self.start_ms:
            raise ValueError("word timestamps must have positive duration")
        return self


class TranscriptCue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start_seconds: int = Field(ge=0)
    end_seconds: int = Field(ge=0)
    text: str = Field(min_length=1)
    speaker_id: str | None = None
    words: list[TranscriptWord] | None = None

    @model_serializer(mode="wrap")
    def serialize_optional_alignment(self, handler):
        value = handler(self)
        if self.words is None:
            value.pop("words", None)
        return value


class TranscriptAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    transcript: list[TranscriptCue]
    language: str | None = None

    @model_serializer(mode="wrap")
    def serialize_optional_language(self, handler):
        value = handler(self)
        if self.language is None:
            value.pop("language", None)
        return value


class _SecondsTranscriptCue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start_seconds: float = Field(
        ge=0, strict=True, allow_inf_nan=False,
        description="Start time in seconds relative to the current video clip",
    )
    end_seconds: float = Field(
        ge=0, strict=True, allow_inf_nan=False,
        description="End time in seconds relative to the current video clip",
    )
    text: str = Field(min_length=1)
    speaker_id: str | None = Field(
        description="Anonymous voice label such as speaker1 or speaker2 within this clip; null if uncertain",
    )


class _SecondsTranscriptAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    transcript: list[_SecondsTranscriptCue]


TRANSCRIPT_PROMPT = """
你只负责从这段视频的音轨中转写可听见的人声，并按声音区分匿名说话人，不负责理解画面。

要求：
1. 所有 start_seconds 和 end_seconds 都是相对于当前视频片段起点的秒数，可使用小数秒；例如第 4.2 秒写 4.2。
2. transcript 只保留能听清的原语言台词，不翻译，不补全听不清的内容。
3. 不要根据画面、字幕、角色外观或背景知识猜测或修正台词。
4. 每条 transcript 都输出 speaker_id；只有能根据音色和声音特征可靠区分说话人时才填写匿名编号，否则必须写 JSON null。按当前片段内首次发声顺序使用 speaker1、speaker2 等编号，不带下划线；同一个声音再次出现时复用相同编号，不因情绪或音量变化重新编号。编号仅在当前输入片段内有效。
5. 说话人切换时必须拆成不同条目，不要把不同说话人的台词合并为一条；能分清的重叠发言分别输出，时间范围可以重叠。
6. 只区分匿名说话人，不匹配真实姓名，不根据画面、字幕、台词提及的人名或背景知识决定 speaker_id；无法可靠分清的发言必须使用 speaker_id=null，不强行猜测，不沿用上一句的编号，不为填满非空编号而合并不同声音。null 不是字符串，不影响保留已听清的台词。
7. text 只写原文台词，不添加 speaker 编号或姓名前缀，不输出画面描述、动作、情绪或剧情推断。
8. 没有可确认的人声时返回空 transcript。
""".strip()


class VideoASRAgent:
    """Transcribe one video window without interpreting its visual content."""

    def __init__(
        self,
        model: MultimodalModel,
        *,
        fps: float,
    ) -> None:
        if not 0.2 <= fps <= 5.0:
            raise ValueError("fps must be between 0.2 and 5.0")
        self.model_name = model.model_name
        self.fps = fps
        self._model = model
        self._closed = False

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            close = getattr(self._model, "close", None)
            if callable(close):
                close()

    def transcribe(
        self,
        video_path: str | Path,
        *,
        duration_seconds: int | None = None,
    ) -> TranscriptAnalysis:
        if self._closed:
            raise RuntimeError("Video transcription agent is closed")
        path = Path(video_path).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(f"video file does not exist: {path}")
        if duration_seconds is None:
            duration_seconds = probe_video_duration_seconds(path)
        elif duration_seconds <= 0:
            raise ValueError("duration_seconds must be greater than zero")
        transcript = self._model.generate(
            [
                VideoInput(path=path, fps=self.fps),
                TextInput(text=TRANSCRIPT_PROMPT),
            ],
            schema_name="candidate_transcript",
            result_type=_SecondsTranscriptAnalysis,
        )
        return normalize_transcription(
            {"segments": [
                {"start": cue.start_seconds, "end": cue.end_seconds,
                 "text": cue.text, "speaker_id": cue.speaker_id}
                for cue in transcript.transcript
            ]},
            duration_seconds=duration_seconds,
        )


def normalize_transcription(
    payload: dict, *, duration_seconds: int
) -> TranscriptAnalysis:
    """Convert provider seconds to integer milliseconds, retaining existing names."""
    language = payload.get("language")
    if language is not None and (not isinstance(language, str) or not language.strip()):
        raise ValueError("ASR language must be a nonempty string")
    segments = payload.get("segments")
    if segments is not None and not isinstance(segments, list):
        raise ValueError("ASR segments must be a list")
    if segments:
        cues = []
        for segment in segments:
            start, end = float(segment["start"]), float(segment["end"])
            if not math.isfinite(start) or not math.isfinite(end) or end < start:
                raise ValueError("ASR returned invalid segment timestamps")
            start_seconds, end_seconds = (
                max(0, round(start * 1000)),
                min(duration_seconds, round(end * 1000)),
            )
            text = segment.get("text", "")
            if not isinstance(text, str):
                raise ValueError("ASR segment text must be a string")
            text = text.strip()
            if text and start_seconds < end_seconds:
                cues.append(
                    TranscriptCue(
                        start_seconds=start_seconds, end_seconds=end_seconds, text=text,
                        speaker_id=segment.get("speaker_id"),
                        words=_normalize_words(segment.get("words"), start_seconds, end_seconds),
                    )
                )
        return TranscriptAnalysis(
            transcript=sorted(cues, key=lambda cue: cue.start_seconds), language=language,
        )
    text = payload.get("text", "" if segments == [] else None)
    if not isinstance(text, str):
        raise ValueError("ASR response must contain text or timestamped segments")
    # JSON-only providers have window-level timing; never invent finer timestamps.
    return TranscriptAnalysis(
        transcript=[
            TranscriptCue(
                start_seconds=0, end_seconds=duration_seconds, text=text.strip()
            )
        ]
        if text.strip()
        else [], language=language,
    )


def _normalize_words(words, start_ms: int, end_ms: int) -> list[TranscriptWord] | None:
    if words is None:
        return None
    if not isinstance(words, list):
        raise ValueError("ASR words must be a list")
    result = []
    for word in words:
        if not isinstance(word, dict):
            raise ValueError("ASR word must be an object")
        text = word.get("word")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("ASR word must contain nonempty text")
        start, end = word.get("start"), word.get("end")
        for value in (start, end):
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)):
                raise ValueError("ASR returned invalid word timestamps")
        if start is not None and end is not None:
            if end < start:
                raise ValueError("ASR returned reversed word timestamps")
            start, end = max(start_ms, round(start * 1000)), min(end_ms, round(end * 1000))
            if start >= end:
                start = end = None
        else:
            start = end = None
        # Missing/zero-width/out-of-cue timing stays unknown; retain the text.
        result.append(TranscriptWord(text=text, start_ms=start, end_ms=end, score=word.get("score")))
    return result
