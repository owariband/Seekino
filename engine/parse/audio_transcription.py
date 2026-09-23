from __future__ import annotations

import math
import subprocess
import tempfile
from pathlib import Path

from gateway.openai.audio import transcribe_audio

from .analysis_windows import probe_video_duration_seconds
from .schemas import TranscriptAnalysis, TranscriptCue


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


def normalize_transcription(
    payload: dict, *, duration_seconds: int
) -> TranscriptAnalysis:
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
                max(0, math.floor(start)),
                min(duration_seconds, math.ceil(end)),
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
