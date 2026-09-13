from __future__ import annotations

import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Literal, Protocol

from gateway import (
    ContentFilterError,
    ImageInput,
    MultimodalModel,
    TextInput,
    VideoInput,
)

from .analysis_windows import probe_video_duration_ms
from .frame_sampler import extract_video_frames, plan_frame_timestamps
from .prompts import (
    ARK_FRAME_SEQUENCE_INTRO,
    FRAME_SEQUENCE_INTRO,
    TRANSCRIPT_PROMPT,
    build_video_parse_prompt,
)
from .schemas import (
    TranscriptAnalysis,
    VideoEvidence,
    VideoParseProvenance,
    VideoTimeline,
)

ProvenanceProvider = Literal["doubao", "ollama", "llama_cpp"]


class SegmentEvidenceParser(Protocol):
    def analyze(
        self,
        video_path: str | Path,
        *,
        context: str | None = None,
        duration_ms: int | None = None,
        candidate_transcript: TranscriptAnalysis | None = None,
    ) -> VideoEvidence: ...


class SegmentParser:
    """Parse one existing segment with direct video model input."""

    def __init__(
        self,
        transcript_model: MultimodalModel,
        video_model: MultimodalModel,
        *,
        transcript_fps: float,
        video_fps: float,
        video_audio_available: bool = True,
    ) -> None:
        _validate_fps("transcript_fps", transcript_fps)
        _validate_fps("video_fps", video_fps)
        self.transcript_model_name = transcript_model.model_name
        self.video_model_name = video_model.model_name
        self.transcript_fps = transcript_fps
        self.video_fps = video_fps
        self.video_audio_available = video_audio_available
        self._transcript_model = transcript_model
        self._video_model = video_model

    def transcribe(
        self,
        video_path: str | Path,
        *,
        duration_ms: int | None = None,
    ) -> TranscriptAnalysis:
        path, resolved_duration_ms = _resolve_segment(video_path, duration_ms)
        transcript = self._transcript_model.generate(
            [
                VideoInput(path=path, fps=self.transcript_fps),
                TextInput(text=TRANSCRIPT_PROMPT),
            ],
            schema_name="candidate_transcript",
            result_type=TranscriptAnalysis,
        )
        return _clamp_transcript(transcript, resolved_duration_ms)

    def analyze(
        self,
        video_path: str | Path,
        *,
        context: str | None = None,
        duration_ms: int | None = None,
        candidate_transcript: TranscriptAnalysis | None = None,
    ) -> VideoEvidence:
        path, resolved_duration_ms = _resolve_segment(video_path, duration_ms)
        transcript = candidate_transcript
        if transcript is None:
            transcript = self.transcribe(path, duration_ms=resolved_duration_ms)
        transcript = _clamp_transcript(transcript, resolved_duration_ms)
        instruction = build_video_parse_prompt(
            resolved_duration_ms,
            transcript,
            context=context,
            ordered_frames=False,
            video_audio_available=self.video_audio_available,
        )
        timeline = self._video_model.generate(
            [
                VideoInput(path=path, fps=self.video_fps),
                TextInput(text=instruction),
            ],
            schema_name="video_timeline",
            result_type=VideoTimeline,
        )
        provider = _provenance_provider(self._video_model)
        return _to_video_evidence(
            timeline,
            transcript,
            resolved_duration_ms,
            provenance=VideoParseProvenance(
                provider=provider,
                model=self.video_model_name,
                input_mode="video",
                frame_fps=self.video_fps if provider == "llama_cpp" else None,
            ),
        )

    def analyze_frames(
        self,
        frames: Sequence[tuple[int, str | Path]],
        *,
        duration_ms: int,
        candidate_transcript: TranscriptAnalysis,
        context: str | None = None,
    ) -> VideoEvidence:
        return _analyze_ordered_frames(
            self._video_model,
            frames,
            duration_ms=duration_ms,
            candidate_transcript=candidate_transcript,
            context=context,
            frame_fps=None,
            sequence_intro=ARK_FRAME_SEQUENCE_INTRO,
        )


class OrderedFrameSegmentParser:
    """Parse one segment after Engine samples an ordered frame sequence."""

    def __init__(
        self,
        model: MultimodalModel,
        *,
        frame_fps: float,
        max_frames: int,
        max_height: int,
    ) -> None:
        _validate_fps("frame_fps", frame_fps)
        if max_frames <= 0:
            raise ValueError("max_frames must be greater than zero")
        if max_height <= 0:
            raise ValueError("max_height must be greater than zero")
        self.model_name = model.model_name
        self.frame_fps = frame_fps
        self.max_frames = max_frames
        self.max_height = max_height
        self._model = model

    def analyze(
        self,
        video_path: str | Path,
        *,
        context: str | None = None,
        duration_ms: int | None = None,
        candidate_transcript: TranscriptAnalysis | None = None,
    ) -> VideoEvidence:
        path, resolved_duration_ms = _resolve_segment(video_path, duration_ms)
        transcript = candidate_transcript or TranscriptAnalysis(transcript=[])
        timestamps_ms = plan_frame_timestamps(
            resolved_duration_ms,
            frame_fps=self.frame_fps,
            max_frames=self.max_frames,
        )
        with tempfile.TemporaryDirectory(prefix="anivag-qwen-frames-") as directory:
            frames = extract_video_frames(
                path,
                timestamps_ms,
                output_dir=Path(directory),
                max_height=self.max_height,
            )
            return self.analyze_frames(
                frames,
                duration_ms=resolved_duration_ms,
                candidate_transcript=transcript,
                context=context,
            )

    def analyze_frames(
        self,
        frames: Sequence[tuple[int, str | Path]],
        *,
        duration_ms: int,
        candidate_transcript: TranscriptAnalysis,
        context: str | None = None,
    ) -> VideoEvidence:
        return _analyze_ordered_frames(
            self._model,
            frames,
            duration_ms=duration_ms,
            candidate_transcript=candidate_transcript,
            context=context,
            frame_fps=self.frame_fps,
            sequence_intro=FRAME_SEQUENCE_INTRO,
        )


class ContentFilterFallbackSegmentParser:
    """Use one local segment fallback only for an explicit content filter."""

    def __init__(
        self,
        primary: SegmentEvidenceParser,
        fallback: SegmentEvidenceParser,
        *,
        primary_model: str,
        on_fallback: Callable[[ContentFilterError], None] | None = None,
    ) -> None:
        self._primary = primary
        self._fallback = fallback
        self._primary_model = primary_model
        self._on_fallback = on_fallback

    def analyze(
        self,
        video_path: str | Path,
        *,
        context: str | None = None,
        duration_ms: int | None = None,
        candidate_transcript: TranscriptAnalysis | None = None,
    ) -> VideoEvidence:
        try:
            return self._primary.analyze(
                video_path,
                context=context,
                duration_ms=duration_ms,
                candidate_transcript=candidate_transcript,
            )
        except ContentFilterError as error:
            if self._on_fallback is not None:
                self._on_fallback(error)
            evidence = self._fallback.analyze(
                video_path,
                context=context,
                duration_ms=duration_ms,
                candidate_transcript=candidate_transcript,
            )
            if evidence.provenance is None:
                raise RuntimeError("local fallback must return video parse provenance")
            return evidence.model_copy(
                update={
                    "provenance": evidence.provenance.model_copy(
                        update={
                            "fallback_reason": "content_filter",
                            "primary_model": self._primary_model,
                            "primary_error_code": error.code,
                            "primary_filter_type": error.content_filter_type,
                            "primary_filter_details": error.content_filter_details,
                            "primary_response_id": error.response_id,
                            "primary_partial_output_chars": (
                                error.partial_output_chars
                            ),
                        }
                    )
                }
            )


def _analyze_ordered_frames(
    model: MultimodalModel,
    frames: Sequence[tuple[int, str | Path]],
    *,
    duration_ms: int,
    candidate_transcript: TranscriptAnalysis,
    context: str | None,
    frame_fps: float | None,
    sequence_intro: str,
) -> VideoEvidence:
    inputs, timestamps_ms = _ordered_frame_inputs(
        frames,
        duration_ms=duration_ms,
        sequence_intro=sequence_intro,
    )
    transcript = _clamp_transcript(candidate_transcript, duration_ms)
    inputs.append(
        TextInput(
            text=build_video_parse_prompt(
                duration_ms,
                transcript,
                context=context,
                ordered_frames=True,
            )
        )
    )
    timeline = model.generate(
        inputs,
        schema_name="video_timeline",
        result_type=VideoTimeline,
    )
    return _to_video_evidence(
        timeline,
        transcript,
        duration_ms,
        provenance=VideoParseProvenance(
            provider=_provenance_provider(model),
            model=model.model_name,
            input_mode="ordered_frames",
            frame_fps=frame_fps,
            frame_timestamps_ms=timestamps_ms,
        ),
    )


def _ordered_frame_inputs(
    frames: Sequence[tuple[int, str | Path]],
    *,
    duration_ms: int,
    sequence_intro: str,
) -> tuple[list[TextInput | ImageInput], list[int]]:
    if duration_ms <= 0:
        raise ValueError("duration_ms must be greater than zero")
    if not frames:
        raise ValueError("at least one sampled frame is required")

    inputs: list[TextInput | ImageInput] = [TextInput(text=sequence_intro)]
    timestamps_ms: list[int] = []
    previous_timestamp_ms = -1
    for index, (timestamp_ms, frame_path) in enumerate(frames, start=1):
        if timestamp_ms <= previous_timestamp_ms:
            raise ValueError("frame timestamps must be strictly increasing")
        if timestamp_ms < 0 or timestamp_ms >= duration_ms:
            raise ValueError("frame timestamp must be inside the video duration")
        inputs.extend(
            [
                TextInput(
                    text=f"Frame {index}/{len(frames)}: t={timestamp_ms} ms"
                ),
                ImageInput(path=Path(frame_path), detail="high"),
            ]
        )
        timestamps_ms.append(timestamp_ms)
        previous_timestamp_ms = timestamp_ms
    return inputs, timestamps_ms


def _resolve_segment(
    video_path: str | Path,
    duration_ms: int | None,
) -> tuple[Path, int]:
    path = Path(video_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"video file does not exist: {path}")
    if duration_ms is None:
        duration_ms = probe_video_duration_ms(path)
    elif duration_ms <= 0:
        raise ValueError("duration_ms must be greater than zero")
    return path, duration_ms


def _to_video_evidence(
    timeline: VideoTimeline,
    transcript: TranscriptAnalysis,
    duration_ms: int,
    *,
    provenance: VideoParseProvenance,
) -> VideoEvidence:
    return VideoEvidence(
        duration_ms=duration_ms,
        candidate_transcript=transcript.transcript,
        summary=timeline.summary,
        events=sorted(
            [
                event.model_copy(update={"end_ms": min(event.end_ms, duration_ms)})
                for event in timeline.events
                if event.start_ms < duration_ms
            ],
            key=lambda event: (event.start_ms, event.end_ms),
        ),
        provenance=provenance,
    )


def _validate_fps(name: str, fps: float) -> None:
    if not 0.2 <= fps <= 5.0:
        raise ValueError(f"{name} must be between 0.2 and 5.0")


def _provenance_provider(model: MultimodalModel) -> ProvenanceProvider:
    # Existing checkpoints persist the Ark/Doubao path as "doubao".
    if model.provider_name == "ark":
        return "doubao"
    if model.provider_name == "ollama":
        return "ollama"
    if model.provider_name == "llama_cpp":
        return "llama_cpp"
    raise ValueError(f"unsupported provenance provider: {model.provider_name}")


def _clamp_transcript(
    transcript: TranscriptAnalysis,
    duration_ms: int,
) -> TranscriptAnalysis:
    return transcript.model_copy(
        update={
            "transcript": [
                cue.model_copy(update={"end_ms": min(cue.end_ms, duration_ms)})
                for cue in transcript.transcript
                if cue.start_ms < duration_ms
            ]
        }
    )
