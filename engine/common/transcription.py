from __future__ import annotations

import math
import subprocess
import tempfile
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from gateway.multimodal import MultimodalModel, TextInput, VideoInput
from gateway.openai.audio import transcribe_audio

from .media_probe import probe_video_duration_seconds


class TranscriptCue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start_seconds: int = Field(ge=0)
    end_seconds: int = Field(ge=0)
    text: str = Field(min_length=1)


class TranscriptAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    transcript: list[TranscriptCue]


TRANSCRIPT_PROMPT = """
你只负责从这段视频的音轨中转写可听见的人声，不负责理解画面。

要求：
1. 所有 start_seconds 和 end_seconds 都是相对于当前视频片段起点的整数毫秒。
2. transcript 只保留能听清的原语言台词，不翻译，不补全听不清的内容。
3. 不要根据画面、字幕、角色外观或背景知识猜测或修正台词。
4. 不要识别说话者身份，不要输出画面描述、动作、情绪或剧情推断。
5. 没有可确认的人声时返回空 transcript。
""".strip()


class VideoASRAgentProtocol(Protocol):
    def transcribe(
        self,
        video_path: str | Path,
        *,
        duration_seconds: int | None = None,
    ) -> TranscriptAnalysis: ...


class AudioTranscriptionAgent:
    """Extract window audio and normalize an Audio Transcriptions response."""

    def __init__(
        self,
        model: str,
        *,
        provider: str,
        base_url: str,
        api_key: str,
        request_timeout_seconds: float,
        response_format: str = "verbose_json",
    ) -> None:
        self.model_name = model
        self.provider_name = provider
        self._base_url = base_url
        self._api_key = api_key
        self._timeout = request_timeout_seconds
        self._response_format = response_format

    def transcribe(
        self, video_path: str | Path, *, duration_seconds: int | None = None
    ) -> TranscriptAnalysis:
        path = Path(video_path).resolve(strict=True)
        duration = (
            duration_seconds
            if duration_seconds is not None
            else probe_video_duration_seconds(path)
        )
        with tempfile.TemporaryDirectory(prefix="anivag-asr-") as directory:
            audio_path = Path(directory) / "audio.wav"
            subprocess.run(
                [
                    "ffmpeg",
                    "-nostdin",
                    "-v",
                    "error",
                    "-y",
                    "-i",
                    str(path),
                    "-vn",
                    "-ac",
                    "1",
                    "-ar",
                    "16000",
                    "-c:a",
                    "pcm_s16le",
                    str(audio_path),
                ],
                check=True,
                capture_output=True,
                timeout=self._timeout,
            )
            payload = transcribe_audio(
                audio_path,
                model=self.model_name,
                provider=self.provider_name,
                base_url=self._base_url,
                api_key=self._api_key,
                timeout=self._timeout,
                response_format=self._response_format,
            )
        return normalize_transcription(payload, duration_seconds=duration)


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

    def transcribe(
        self,
        video_path: str | Path,
        *,
        duration_seconds: int | None = None,
    ) -> TranscriptAnalysis:
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
            result_type=TranscriptAnalysis,
        )
        return clamp_transcript(transcript, duration_seconds)


def normalize_transcription(
    payload: dict, *, duration_seconds: int
) -> TranscriptAnalysis:
    """Convert provider seconds to integer milliseconds, retaining existing names."""
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
                        start_seconds=start_seconds, end_seconds=end_seconds, text=text
                    )
                )
        return TranscriptAnalysis(
            transcript=sorted(cues, key=lambda cue: cue.start_seconds)
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
        else []
    )


def clamp_transcript(
    transcript: TranscriptAnalysis,
    duration_seconds: int,
) -> TranscriptAnalysis:
    return transcript.model_copy(
        update={
            "transcript": [
                cue.model_copy(
                    update={"end_seconds": min(cue.end_seconds, duration_seconds)}
                )
                for cue in transcript.transcript
                if cue.start_seconds < duration_seconds
            ]
        }
    )
