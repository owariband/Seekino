from __future__ import annotations

import math
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Literal, Protocol

from gateway.multimodal import (
    ImageInput,
    MultimodalModel,
    TextInput,
    VideoInput,
)
from gateway.errors import ContentFilterError

from .analysis_windows import probe_video_duration_seconds
from .background import BackgroundImage, build_background_image_inputs
from .frame_sampler import extract_video_frames, plan_frame_timestamps
from .prompts import (
    ARK_FRAME_SEQUENCE_INTRO,
    FRAME_SEQUENCE_INTRO,
    TRANSCRIPT_PROMPT,
    build_fallback_refiner_prompt,
    build_neutral_visual_prompt,
    build_video_parse_prompt,
)
from .schemas import (
    NeutralVisualEvidence,
    TranscriptAnalysis,
    VideoEvidence,
    VideoParseProvenance,
    VideoTimeline,
)

ProvenanceProvider = str


class VideoASRAgentProtocol(Protocol):
    def transcribe(
        self,
        video_path: str | Path,
        *,
        duration_seconds: int | None = None,
    ) -> TranscriptAnalysis: ...


class VideoUnderstandingAgentProtocol(Protocol):
    def understand(
        self,
        video_path: str | Path,
        *,
        transcript: TranscriptAnalysis,
        context: str | None = None,
        duration_seconds: int | None = None,
        background_images: Sequence[BackgroundImage] = (),
    ) -> VideoEvidence: ...


class VideoASRAgent:
    """Transcribe one video window without interpreting its visual content."""

    def __init__(
        self,
        model: MultimodalModel,
        *,
        fps: float,
    ) -> None:
        _validate_fps("fps", fps)
        self.model_name = model.model_name
        self.fps = fps
        self._model = model

    def transcribe(
        self,
        video_path: str | Path,
        *,
        duration_seconds: int | None = None,
    ) -> TranscriptAnalysis:
        path, resolved_duration_seconds = _resolve_video_chunk(video_path, duration_seconds)
        return _transcribe_video_chunk(
            self._model,
            path,
            duration_seconds=resolved_duration_seconds,
            transcript_fps=self.fps,
        )


class VideoUnderstandingAgent:
    """Understand one video window from video evidence and an explicit ASR DTO."""

    def __init__(
        self,
        model: MultimodalModel,
        *,
        fps: float,
        video_audio_available: bool = True,
    ) -> None:
        _validate_fps("fps", fps)
        self.model_name = model.model_name
        self.fps = fps
        self.video_audio_available = video_audio_available
        self._model = model

    def understand(
        self,
        video_path: str | Path,
        *,
        transcript: TranscriptAnalysis,
        context: str | None = None,
        duration_seconds: int | None = None,
        background_images: Sequence[BackgroundImage] = (),
    ) -> VideoEvidence:
        path, resolved_duration_seconds = _resolve_video_chunk(video_path, duration_seconds)
        transcript = _clamp_transcript(transcript, resolved_duration_seconds)
        instruction = build_video_parse_prompt(
            resolved_duration_seconds,
            transcript,
            context=context,
            ordered_frames=False,
            video_audio_available=self.video_audio_available,
        )
        timeline = self._model.generate(
            [
                *build_background_image_inputs(background_images),
                VideoInput(path=path, fps=self.fps),
                TextInput(text=instruction),
            ],
            schema_name="video_timeline",
            result_type=VideoTimeline,
        )
        provider = _provenance_provider(self._model)
        return _to_video_evidence(
            timeline,
            transcript,
            resolved_duration_seconds,
            provenance=VideoParseProvenance(
                provider=provider,
                model=self.model_name,
                input_mode="video",
                frame_fps=self.fps if provider == "llama_cpp" else None,
            ),
        )

    def analyze_frames(
        self,
        frames: Sequence[tuple[int, str | Path]],
        *,
        duration_seconds: int,
        transcript: TranscriptAnalysis,
        context: str | None = None,
        background_images: Sequence[BackgroundImage] = (),
    ) -> VideoEvidence:
        return _analyze_ordered_frames(
            self._model,
            frames,
            duration_seconds=duration_seconds,
            transcript=transcript,
            context=context,
            frame_fps=None,
            sequence_intro=ARK_FRAME_SEQUENCE_INTRO,
            background_images=background_images,
        )


class OrderedFrameVideoUnderstandingAgent:
    """Understand one video window from an ordered frame sequence."""

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

    def understand(
        self,
        video_path: str | Path,
        *,
        transcript: TranscriptAnalysis,
        context: str | None = None,
        duration_seconds: int | None = None,
        background_images: Sequence[BackgroundImage] = (),
    ) -> VideoEvidence:
        path, resolved_duration_seconds = _resolve_video_chunk(video_path, duration_seconds)
        timestamps_seconds = plan_frame_timestamps(
            resolved_duration_seconds,
            frame_fps=self.frame_fps,
            max_frames=self.max_frames,
        )
        with tempfile.TemporaryDirectory(prefix="anivag-qwen-frames-") as directory:
            frames = extract_video_frames(
                path,
                timestamps_seconds,
                output_dir=Path(directory),
                max_height=self.max_height,
            )
            return self.analyze_frames(
                frames,
                duration_seconds=resolved_duration_seconds,
                transcript=transcript,
                context=context,
                background_images=background_images,
            )

    def analyze_frames(
        self,
        frames: Sequence[tuple[int, str | Path]],
        *,
        duration_seconds: int,
        transcript: TranscriptAnalysis,
        context: str | None = None,
        background_images: Sequence[BackgroundImage] = (),
    ) -> VideoEvidence:
        return _analyze_ordered_frames(
            self._model,
            frames,
            duration_seconds=duration_seconds,
            transcript=transcript,
            context=context,
            frame_fps=self.frame_fps,
            sequence_intro=FRAME_SEQUENCE_INTRO,
            background_images=background_images,
        )


class FrameFallbackVideoUnderstandingAgent:
    """Understand a window from explicit ASR and grouped visual evidence."""

    def __init__(
        self,
        vision_model: MultimodalModel,
        refiner_model: MultimodalModel,
        *,
        asr_model_name: str | None,
        frame_fps: float,
        group_duration_seconds: int,
        max_frames_per_group: int,
        max_height: int,
    ) -> None:
        _validate_fps("frame_fps", frame_fps)
        if group_duration_seconds <= 0:
            raise ValueError("group_duration_seconds must be greater than zero")
        if max_frames_per_group <= 0:
            raise ValueError("max_frames_per_group must be greater than zero")
        if max_height <= 0:
            raise ValueError("max_height must be greater than zero")
        if not vision_model.provider_name.strip() or not refiner_model.provider_name.strip():
            raise ValueError("fallback models must identify their providers")

        self.asr_model_name = asr_model_name
        self.vision_model_name = vision_model.model_name
        self.refiner_model_name = refiner_model.model_name
        self.frame_fps = frame_fps
        self.group_duration_seconds = group_duration_seconds
        self.max_frames_per_group = max_frames_per_group
        self.max_height = max_height
        self._vision_model = vision_model
        self._refiner_model = refiner_model

    def understand(
        self,
        video_path: str | Path,
        *,
        transcript: TranscriptAnalysis,
        context: str | None = None,
        duration_seconds: int | None = None,
        background_images: Sequence[BackgroundImage] = (),
    ) -> VideoEvidence:
        path, resolved_duration_seconds = _resolve_video_chunk(video_path, duration_seconds)
        transcript = _clamp_transcript(transcript, resolved_duration_seconds)

        mapped_observations: list[dict[str, Any]] = []
        all_timestamps_seconds: list[int] = []
        group_frame_counts: list[int] = []
        frame_interval_seconds = 1000 / self.frame_fps
        with tempfile.TemporaryDirectory(
            prefix="anivag-deepseek-frames-"
        ) as directory:
            frame_root = Path(directory)
            for group_index, (group_start_seconds, group_end_seconds) in enumerate(
                _analysis_groups(
                    resolved_duration_seconds,
                    group_duration_seconds=self.group_duration_seconds,
                )
            ):
                local_timestamps_seconds = plan_frame_timestamps(
                    group_end_seconds - group_start_seconds,
                    frame_fps=self.frame_fps,
                    max_frames=self.max_frames_per_group,
                )
                timestamps_seconds = [
                    group_start_seconds + timestamp_seconds
                    for timestamp_seconds in local_timestamps_seconds
                ]
                frames = extract_video_frames(
                    path,
                    timestamps_seconds,
                    output_dir=frame_root / f"group-{group_index:03d}",
                    max_height=self.max_height,
                )
                evidence = self._vision_model.generate(
                    _neutral_visual_inputs(frames),
                    schema_name="neutral_visual_evidence",
                    result_type=NeutralVisualEvidence,
                )
                _validate_visual_evidence(evidence, frame_count=len(frames))
                mapped_observations.extend(
                    _map_visual_observations(
                        evidence,
                        group_index=group_index,
                        group_start_seconds=group_start_seconds,
                        group_end_seconds=group_end_seconds,
                        timestamps_seconds=timestamps_seconds,
                        frame_interval_seconds=frame_interval_seconds,
                    )
                )
                all_timestamps_seconds.extend(timestamps_seconds)
                group_frame_counts.append(len(frames))

        observations = _deduplicate_visual_observations(
            mapped_observations,
            max_gap_seconds=frame_interval_seconds + 500,
        )
        timeline = self._refiner_model.generate(
            [
                *build_background_image_inputs(background_images),
                TextInput(
                    text=build_fallback_refiner_prompt(
                        resolved_duration_seconds,
                        transcript,
                        observations,
                        context=context,
                    )
                )
            ],
            schema_name="fallback_video_timeline",
            result_type=VideoTimeline,
        )
        _validate_fallback_timeline(
            timeline,
            transcript=transcript,
            observations=observations,
            duration_seconds=resolved_duration_seconds,
        )
        return _to_video_evidence(
            timeline,
            transcript,
            resolved_duration_seconds,
            provenance=VideoParseProvenance(
                provider=_provenance_provider(self._refiner_model),
                model=self.refiner_model_name,
                input_mode="ordered_frames",
                frame_fps=self.frame_fps,
                frame_timestamps_seconds=all_timestamps_seconds,
                group_frame_counts=group_frame_counts,
                group_duration_seconds=self.group_duration_seconds,
                max_frames_per_group=self.max_frames_per_group,
                fallback_transcript_model=self.asr_model_name,
                visual_model=self.vision_model_name,
                refiner_model=self.refiner_model_name,
            ),
        )


class ContentFilterVideoUnderstandingAgent:
    """Use one configured video-chunk fallback for an explicit content filter."""

    def __init__(
        self,
        primary: VideoUnderstandingAgentProtocol,
        fallback: VideoUnderstandingAgentProtocol,
        *,
        primary_model: str,
        on_fallback: Callable[[ContentFilterError], None] | None = None,
    ) -> None:
        self._primary = primary
        self._fallback = fallback
        self._primary_model = primary_model
        self._on_fallback = on_fallback

    def understand(
        self,
        video_path: str | Path,
        *,
        transcript: TranscriptAnalysis,
        context: str | None = None,
        duration_seconds: int | None = None,
        background_images: Sequence[BackgroundImage] = (),
    ) -> VideoEvidence:
        try:
            return self._primary.understand(
                video_path,
                transcript=transcript,
                context=context,
                duration_seconds=duration_seconds,
                background_images=background_images,
            )
        except ContentFilterError as error:
            if self._on_fallback is not None:
                self._on_fallback(error)
            evidence = self._fallback.understand(
                video_path,
                transcript=transcript,
                context=context,
                duration_seconds=duration_seconds,
                background_images=background_images,
            )
            if evidence.provenance is None:
                raise RuntimeError("fallback must return video parse provenance")
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
    duration_seconds: int,
    transcript: TranscriptAnalysis,
    context: str | None,
    frame_fps: float | None,
    sequence_intro: str,
    background_images: Sequence[BackgroundImage] = (),
) -> VideoEvidence:
    inputs, timestamps_seconds = _ordered_frame_inputs(
        frames,
        duration_seconds=duration_seconds,
        sequence_intro=sequence_intro,
    )
    inputs = [*build_background_image_inputs(background_images), *inputs]
    transcript = _clamp_transcript(transcript, duration_seconds)
    inputs.append(
        TextInput(
            text=build_video_parse_prompt(
                duration_seconds,
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
        duration_seconds,
        provenance=VideoParseProvenance(
            provider=_provenance_provider(model),
            model=model.model_name,
            input_mode="ordered_frames",
            frame_fps=frame_fps,
            frame_timestamps_seconds=timestamps_seconds,
        ),
    )


def _ordered_frame_inputs(
    frames: Sequence[tuple[int, str | Path]],
    *,
    duration_seconds: int,
    sequence_intro: str,
) -> tuple[list[TextInput | ImageInput], list[int]]:
    if duration_seconds <= 0:
        raise ValueError("duration_seconds must be greater than zero")
    if not frames:
        raise ValueError("at least one sampled frame is required")

    inputs: list[TextInput | ImageInput] = [TextInput(text=sequence_intro)]
    timestamps_seconds: list[int] = []
    previous_timestamp_seconds = -1
    for index, (timestamp_seconds, frame_path) in enumerate(frames, start=1):
        if isinstance(timestamp_seconds, bool) or not isinstance(timestamp_seconds, int):
            raise ValueError("frame timestamp must be integer milliseconds")
        if timestamp_seconds <= previous_timestamp_seconds:
            raise ValueError("frame timestamps must be strictly increasing")
        if timestamp_seconds < 0 or timestamp_seconds >= duration_seconds:
            raise ValueError("frame timestamp must be inside the video duration")
        inputs.extend(
            [
                TextInput(
                    text=(
                        f"Frame {index}/{len(frames)}: "
                        f"t={timestamp_seconds} milliseconds"
                    )
                ),
                ImageInput(path=Path(frame_path), detail="high"),
            ]
        )
        timestamps_seconds.append(timestamp_seconds)
        previous_timestamp_seconds = timestamp_seconds
    return inputs, timestamps_seconds


def _neutral_visual_inputs(
    frames: Sequence[tuple[int, str | Path]],
) -> list[TextInput | ImageInput]:
    if not frames:
        raise ValueError("at least one sampled frame is required")
    inputs: list[TextInput | ImageInput] = [
        TextInput(
            text=(
                f"以下是同一视频片段按时间顺序排列的 {len(frames)} 张抽帧图片。"
                "Frame 编号只表示当前请求中的输入顺序。"
            )
        )
    ]
    for frame_number, (_, frame_path) in enumerate(frames, start=1):
        inputs.extend(
            [
                TextInput(text=f"Frame {frame_number}/{len(frames)}"),
                ImageInput(path=Path(frame_path), detail="high"),
            ]
        )
    inputs.append(TextInput(text=build_neutral_visual_prompt(len(frames))))
    return inputs


def _analysis_groups(
    duration_seconds: int,
    *,
    group_duration_seconds: int,
) -> list[tuple[int, int]]:
    if duration_seconds <= 0:
        raise ValueError("duration_seconds must be greater than zero")
    if group_duration_seconds <= 0:
        raise ValueError("group_duration_seconds must be greater than zero")
    group_duration_seconds *= 1000
    return [
        (
            start_seconds,
            min(duration_seconds, start_seconds + group_duration_seconds),
        )
        for start_seconds in range(0, duration_seconds, group_duration_seconds)
    ]


def _validate_visual_evidence(
    evidence: NeutralVisualEvidence,
    *,
    frame_count: int,
) -> None:
    for observation in evidence.observations:
        if any(
            frame_number < 1 or frame_number > frame_count
            for frame_number in observation.evidence_frames
        ):
            raise ValueError("vision model returned an out-of-range evidence frame")


def _map_visual_observations(
    evidence: NeutralVisualEvidence,
    *,
    group_index: int,
    group_start_seconds: int,
    group_end_seconds: int,
    timestamps_seconds: Sequence[int],
    frame_interval_seconds: float,
) -> list[dict[str, Any]]:
    mapped: list[dict[str, Any]] = []
    half_interval_seconds = frame_interval_seconds / 2
    for observation_index, observation in enumerate(evidence.observations):
        evidence_timestamps_seconds = sorted(
            {
                timestamps_seconds[frame_number - 1]
                for frame_number in observation.evidence_frames
            }
        )
        mapped.append(
            {
                "source_observation_ids": [
                    f"g{group_index:03d}-o{observation_index:03d}"
                ],
                "source_group_index": group_index,
                "evidence_frames": sorted(set(observation.evidence_frames)),
                "evidence_timestamps_seconds": evidence_timestamps_seconds,
                "support_intervals_seconds": [
                    [
                        max(
                            group_start_seconds,
                            math.floor(timestamp_seconds - half_interval_seconds),
                        ),
                        min(
                            group_end_seconds,
                            math.ceil(timestamp_seconds + half_interval_seconds),
                        ),
                    ]
                    for timestamp_seconds in evidence_timestamps_seconds
                ],
                "subjects": list(dict.fromkeys(observation.subjects)),
                "visual_fact": observation.visual_fact,
                "hard_subtitle": observation.hard_subtitle,
                "visible_speaker": observation.visible_speaker,
            }
        )
    return mapped


def _deduplicate_visual_observations(
    observations: Sequence[dict[str, Any]],
    *,
    max_gap_seconds: float,
) -> list[dict[str, Any]]:
    ordered = sorted(
        observations,
        key=lambda item: min(item["evidence_timestamps_seconds"]),
    )
    deduplicated: list[dict[str, Any]] = []
    for observation in ordered:
        item = dict(observation)
        key = _visual_observation_key(item)
        if deduplicated:
            previous = deduplicated[-1]
            gap_seconds = min(item["evidence_timestamps_seconds"]) - max(
                previous["evidence_timestamps_seconds"]
            )
            if (
                key == _visual_observation_key(previous)
                and gap_seconds <= max_gap_seconds
            ):
                for field in (
                    "source_observation_ids",
                    "evidence_frames",
                    "evidence_timestamps_seconds",
                    "support_intervals_seconds",
                    "subjects",
                ):
                    previous[field] = _stable_unique(
                        [*previous[field], *item[field]]
                    )
                continue
        deduplicated.append(item)

    for index, observation in enumerate(deduplicated):
        observation["observation_id"] = f"e{index:03d}"
    return deduplicated


def _visual_observation_key(observation: dict[str, Any]) -> tuple[str, str, str]:
    return (
        _normalize_text(observation["visual_fact"]),
        _normalize_text(observation["hard_subtitle"]),
        _normalize_text(observation["visible_speaker"]),
    )


def _stable_unique(values: Sequence[Any]) -> list[Any]:
    unique: list[Any] = []
    for value in values:
        if value not in unique:
            unique.append(value)
    return unique


def _normalize_text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())


def _validate_fallback_timeline(
    timeline: VideoTimeline,
    *,
    transcript: TranscriptAnalysis,
    observations: Sequence[dict[str, Any]],
    duration_seconds: int,
) -> None:
    allowed_dialogue_texts = {
        _normalize_text(cue.text) for cue in transcript.transcript
    }
    for observation in observations:
        subtitle = observation.get("hard_subtitle")
        if not isinstance(subtitle, str):
            continue
        allowed_dialogue_texts.update(
            _normalize_text(line)
            for line in subtitle.splitlines()
            if _normalize_text(line)
        )

    for index, event in enumerate(timeline.events):
        if (
            event.start_seconds >= duration_seconds
            or event.end_seconds > duration_seconds
        ):
            raise ValueError(
                f"fallback event {index} is outside the analysis window"
            )
        if event.event_type != "DIALOGUE" or event.text is None:
            continue
        if _normalize_text(event.text) not in allowed_dialogue_texts:
            raise ValueError(
                f"fallback dialogue {index} is not grounded in ASR or subtitles"
            )


def _resolve_video_chunk(
    video_path: str | Path,
    duration_seconds: int | None,
) -> tuple[Path, int]:
    path = Path(video_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"video file does not exist: {path}")
    if duration_seconds is None:
        duration_seconds = probe_video_duration_seconds(path)
    elif duration_seconds <= 0:
        raise ValueError("duration_seconds must be greater than zero")
    return path, duration_seconds


def _transcribe_video_chunk(
    model: MultimodalModel,
    video_path: Path,
    *,
    duration_seconds: int,
    transcript_fps: float,
) -> TranscriptAnalysis:
    transcript = model.generate(
        [
            VideoInput(path=video_path, fps=transcript_fps),
            TextInput(text=TRANSCRIPT_PROMPT),
        ],
        schema_name="candidate_transcript",
        result_type=TranscriptAnalysis,
    )
    return _clamp_transcript(transcript, duration_seconds)


def _to_video_evidence(
    timeline: VideoTimeline,
    transcript: TranscriptAnalysis,
    duration_seconds: int,
    *,
    provenance: VideoParseProvenance,
) -> VideoEvidence:
    return VideoEvidence(
        duration_seconds=duration_seconds,
        candidate_transcript=transcript.transcript,
        summary=timeline.summary,
        events=sorted(
            [
                event.model_copy(
                    update={"end_seconds": min(event.end_seconds, duration_seconds)}
                )
                for event in timeline.events
                if event.start_seconds < duration_seconds
            ],
            key=lambda event: (event.start_seconds, event.end_seconds),
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
    if model.provider_name == "deepseek":
        return "deepseek"
    if model.provider_name == "ollama":
        return "ollama"
    if model.provider_name == "llama_cpp":
        return "llama_cpp"
    if not model.provider_name.strip():
        raise ValueError("provenance provider must not be empty")
    return model.provider_name


def _clamp_transcript(
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
