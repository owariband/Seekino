from __future__ import annotations

import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Literal, Protocol

from gateway.models import (
    ImageInput,
    MultimodalModel,
    TextInput,
    VideoInput,
)
from gateway.sse_proxy import ContentFilterError

from .analysis_windows import probe_video_duration_ms
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

ProvenanceProvider = Literal["doubao", "deepseek", "ollama", "llama_cpp"]


class VideoChunkEvidenceParser(Protocol):
    def analyze(
        self,
        video_path: str | Path,
        *,
        context: str | None = None,
        duration_ms: int | None = None,
        candidate_transcript: TranscriptAnalysis | None = None,
        background_images: Sequence[BackgroundImage] = (),
    ) -> VideoEvidence: ...


class VideoChunkParser:
    """Parse one existing video chunk with direct video model input."""

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
        path, resolved_duration_ms = _resolve_video_chunk(video_path, duration_ms)
        return _transcribe_video_chunk(
            self._transcript_model,
            path,
            duration_ms=resolved_duration_ms,
            transcript_fps=self.transcript_fps,
        )

    def analyze(
        self,
        video_path: str | Path,
        *,
        context: str | None = None,
        duration_ms: int | None = None,
        candidate_transcript: TranscriptAnalysis | None = None,
        background_images: Sequence[BackgroundImage] = (),
    ) -> VideoEvidence:
        path, resolved_duration_ms = _resolve_video_chunk(video_path, duration_ms)
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
                *build_background_image_inputs(background_images),
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
        background_images: Sequence[BackgroundImage] = (),
    ) -> VideoEvidence:
        return _analyze_ordered_frames(
            self._video_model,
            frames,
            duration_ms=duration_ms,
            candidate_transcript=candidate_transcript,
            context=context,
            frame_fps=None,
            sequence_intro=ARK_FRAME_SEQUENCE_INTRO,
            background_images=background_images,
        )


class OrderedFrameVideoChunkParser:
    """Parse one video chunk after Engine samples an ordered frame sequence."""

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
        background_images: Sequence[BackgroundImage] = (),
    ) -> VideoEvidence:
        path, resolved_duration_ms = _resolve_video_chunk(video_path, duration_ms)
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
                background_images=background_images,
            )

    def analyze_frames(
        self,
        frames: Sequence[tuple[int, str | Path]],
        *,
        duration_ms: int,
        candidate_transcript: TranscriptAnalysis,
        context: str | None = None,
        background_images: Sequence[BackgroundImage] = (),
    ) -> VideoEvidence:
        return _analyze_ordered_frames(
            self._model,
            frames,
            duration_ms=duration_ms,
            candidate_transcript=candidate_transcript,
            context=context,
            frame_fps=self.frame_fps,
            sequence_intro=FRAME_SEQUENCE_INTRO,
            background_images=background_images,
        )


class DeepSeekFallbackVideoChunkParser:
    """Run Lite ASR, grouped neutral vision, then evidence-only refinement."""

    def __init__(
        self,
        transcript_model: MultimodalModel,
        vision_model: MultimodalModel,
        refiner_model: MultimodalModel,
        *,
        transcript_fps: float,
        frame_fps: float,
        group_duration_ms: int,
        max_frames_per_group: int,
        max_height: int,
    ) -> None:
        _validate_fps("transcript_fps", transcript_fps)
        _validate_fps("frame_fps", frame_fps)
        if group_duration_ms <= 0:
            raise ValueError("group_duration_ms must be greater than zero")
        if max_frames_per_group <= 0:
            raise ValueError("max_frames_per_group must be greater than zero")
        if max_height <= 0:
            raise ValueError("max_height must be greater than zero")
        if vision_model.provider_name != "deepseek":
            raise ValueError("vision_model must use the deepseek provider")
        if refiner_model.provider_name != "deepseek":
            raise ValueError("refiner_model must use the deepseek provider")

        self.transcript_model_name = transcript_model.model_name
        self.vision_model_name = vision_model.model_name
        self.refiner_model_name = refiner_model.model_name
        self.transcript_fps = transcript_fps
        self.frame_fps = frame_fps
        self.group_duration_ms = group_duration_ms
        self.max_frames_per_group = max_frames_per_group
        self.max_height = max_height
        self._transcript_model = transcript_model
        self._vision_model = vision_model
        self._refiner_model = refiner_model

    def analyze(
        self,
        video_path: str | Path,
        *,
        context: str | None = None,
        duration_ms: int | None = None,
        candidate_transcript: TranscriptAnalysis | None = None,
        background_images: Sequence[BackgroundImage] = (),
    ) -> VideoEvidence:
        path, resolved_duration_ms = _resolve_video_chunk(video_path, duration_ms)
        # The primary path may deliberately use an empty candidate transcript.
        # A fallback still needs the dedicated Lite audio pass selected for this
        # chain. Reuse a non-empty precomputed Lite result when one exists.
        if candidate_transcript is None or not candidate_transcript.transcript:
            transcript = _transcribe_video_chunk(
                self._transcript_model,
                path,
                duration_ms=resolved_duration_ms,
                transcript_fps=self.transcript_fps,
            )
        else:
            transcript = _clamp_transcript(
                candidate_transcript,
                resolved_duration_ms,
            )

        mapped_observations: list[dict[str, Any]] = []
        all_timestamps_ms: list[int] = []
        group_frame_counts: list[int] = []
        frame_interval_ms = max(1, round(1000 / self.frame_fps))
        with tempfile.TemporaryDirectory(
            prefix="anivag-deepseek-frames-"
        ) as directory:
            frame_root = Path(directory)
            for group_index, (group_start_ms, group_end_ms) in enumerate(
                _analysis_groups(
                    resolved_duration_ms,
                    group_duration_ms=self.group_duration_ms,
                )
            ):
                local_timestamps_ms = plan_frame_timestamps(
                    group_end_ms - group_start_ms,
                    frame_fps=self.frame_fps,
                    max_frames=self.max_frames_per_group,
                )
                timestamps_ms = [
                    group_start_ms + timestamp_ms
                    for timestamp_ms in local_timestamps_ms
                ]
                frames = extract_video_frames(
                    path,
                    timestamps_ms,
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
                        group_start_ms=group_start_ms,
                        group_end_ms=group_end_ms,
                        timestamps_ms=timestamps_ms,
                        frame_interval_ms=frame_interval_ms,
                    )
                )
                all_timestamps_ms.extend(timestamps_ms)
                group_frame_counts.append(len(frames))

        observations = _deduplicate_visual_observations(
            mapped_observations,
            max_gap_ms=frame_interval_ms + 500,
        )
        timeline = self._refiner_model.generate(
            [
                *build_background_image_inputs(background_images),
                TextInput(
                    text=build_fallback_refiner_prompt(
                        resolved_duration_ms,
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
            duration_ms=resolved_duration_ms,
        )
        return _to_video_evidence(
            timeline,
            transcript,
            resolved_duration_ms,
            provenance=VideoParseProvenance(
                provider="deepseek",
                model=self.refiner_model_name,
                input_mode="ordered_frames",
                frame_fps=self.frame_fps,
                frame_timestamps_ms=all_timestamps_ms,
                group_frame_counts=group_frame_counts,
                group_duration_ms=self.group_duration_ms,
                max_frames_per_group=self.max_frames_per_group,
                fallback_transcript_model=self.transcript_model_name,
                visual_model=self.vision_model_name,
                refiner_model=self.refiner_model_name,
            ),
        )


class ContentFilterFallbackVideoChunkParser:
    """Use one configured video-chunk fallback for an explicit content filter."""

    def __init__(
        self,
        primary: VideoChunkEvidenceParser,
        fallback: VideoChunkEvidenceParser,
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
        background_images: Sequence[BackgroundImage] = (),
    ) -> VideoEvidence:
        try:
            return self._primary.analyze(
                video_path,
                context=context,
                duration_ms=duration_ms,
                candidate_transcript=candidate_transcript,
                background_images=background_images,
            )
        except ContentFilterError as error:
            if self._on_fallback is not None:
                self._on_fallback(error)
            evidence = self._fallback.analyze(
                video_path,
                context=context,
                duration_ms=duration_ms,
                candidate_transcript=candidate_transcript,
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
    duration_ms: int,
    candidate_transcript: TranscriptAnalysis,
    context: str | None,
    frame_fps: float | None,
    sequence_intro: str,
    background_images: Sequence[BackgroundImage] = (),
) -> VideoEvidence:
    inputs, timestamps_ms = _ordered_frame_inputs(
        frames,
        duration_ms=duration_ms,
        sequence_intro=sequence_intro,
    )
    inputs = [*build_background_image_inputs(background_images), *inputs]
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
    duration_ms: int,
    *,
    group_duration_ms: int,
) -> list[tuple[int, int]]:
    if duration_ms <= 0:
        raise ValueError("duration_ms must be greater than zero")
    if group_duration_ms <= 0:
        raise ValueError("group_duration_ms must be greater than zero")
    return [
        (start_ms, min(duration_ms, start_ms + group_duration_ms))
        for start_ms in range(0, duration_ms, group_duration_ms)
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
    group_start_ms: int,
    group_end_ms: int,
    timestamps_ms: Sequence[int],
    frame_interval_ms: int,
) -> list[dict[str, Any]]:
    mapped: list[dict[str, Any]] = []
    half_interval_ms = max(1, frame_interval_ms // 2)
    for observation_index, observation in enumerate(evidence.observations):
        evidence_timestamps_ms = sorted(
            {
                timestamps_ms[frame_number - 1]
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
                "evidence_timestamps_ms": evidence_timestamps_ms,
                "support_intervals_ms": [
                    [
                        max(group_start_ms, timestamp_ms - half_interval_ms),
                        min(group_end_ms, timestamp_ms + half_interval_ms),
                    ]
                    for timestamp_ms in evidence_timestamps_ms
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
    max_gap_ms: int,
) -> list[dict[str, Any]]:
    ordered = sorted(
        observations,
        key=lambda item: min(item["evidence_timestamps_ms"]),
    )
    deduplicated: list[dict[str, Any]] = []
    for observation in ordered:
        item = dict(observation)
        key = _visual_observation_key(item)
        if deduplicated:
            previous = deduplicated[-1]
            gap_ms = min(item["evidence_timestamps_ms"]) - max(
                previous["evidence_timestamps_ms"]
            )
            if key == _visual_observation_key(previous) and gap_ms <= max_gap_ms:
                for field in (
                    "source_observation_ids",
                    "evidence_frames",
                    "evidence_timestamps_ms",
                    "support_intervals_ms",
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
    duration_ms: int,
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
        if event.start_ms >= duration_ms or event.end_ms > duration_ms:
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


def _transcribe_video_chunk(
    model: MultimodalModel,
    video_path: Path,
    *,
    duration_ms: int,
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
    return _clamp_transcript(transcript, duration_ms)


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
    if model.provider_name == "deepseek":
        return "deepseek"
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
