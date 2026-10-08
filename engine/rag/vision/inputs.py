"""Visual input, ordered sampling and realized media manifests; no media I/O."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal, TypeAlias

from engine.rag._validation import (
    _nonblank, _positive_finite, _sha256_json, _validate_bounds, is_sha256,
)

VIDEO_CLIP_PREPROCESS_REVISION = "ffmpeg-h264-crf23-scale-cap-v1"

@dataclass(frozen=True, slots=True)
class VideoClipInputContract:
    """Versioned preprocessing requested for one Chunk video segment."""

    fps: float
    max_height: int
    mime_type: str
    preprocess_revision: str
    kind: Literal["video_clip"] = "video_clip"

    def __post_init__(self) -> None:
        if self.kind != "video_clip":
            raise ValueError("video clip input kind must be video_clip")
        if not _positive_finite(self.fps):
            raise ValueError("video clip fps must be positive and finite")
        object.__setattr__(self, "fps", float(self.fps))
        if (
            isinstance(self.max_height, bool)
            or not isinstance(self.max_height, int)
            or self.max_height <= 0
        ):
            raise ValueError("video clip max_height must be a positive integer")
        if not _nonblank(self.mime_type) or not _nonblank(self.preprocess_revision):
            raise ValueError("video clip MIME type and preprocess revision must be nonblank")

    def claims(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "video_fps": self.fps,
            "max_height": self.max_height,
            "mime_type": self.mime_type,
            "preprocess_revision": self.preprocess_revision,
        }


@dataclass(frozen=True, slots=True)
class OrderedFrameSamplingContract:
    """Explicit indexing sampling rule, independent of media extraction."""

    fps: float
    max_frames_per_chunk: int
    strategy: Literal["uniform_midpoint_v1"] = "uniform_midpoint_v1"

    def __post_init__(self) -> None:
        if self.strategy != "uniform_midpoint_v1":
            raise ValueError("unsupported ordered frame sampling strategy")
        if not _positive_finite(self.fps):
            raise ValueError("sampling fps must be positive and finite")
        object.__setattr__(self, "fps", float(self.fps))
        if (
            isinstance(self.max_frames_per_chunk, bool)
            or not isinstance(self.max_frames_per_chunk, int)
            or self.max_frames_per_chunk <= 0
        ):
            raise ValueError("max_frames_per_chunk must be a positive integer")

    def claims(self) -> dict[str, object]:
        return {
            "strategy": self.strategy,
            "fps": self.fps,
            "max_frames_per_chunk": self.max_frames_per_chunk,
        }

    @property
    def sampling_contract_id(self) -> str:
        return f"sampling:sha256:{_sha256_json(self.claims())}"


def plan_ordered_frame_timestamps(
    start_seconds: int,
    end_seconds: int,
    sampling: OrderedFrameSamplingContract,
) -> tuple[int, ...]:
    """Plan absolute integer-millisecond midpoints; existing field names stay fixed."""

    _validate_bounds(start_seconds, end_seconds)
    duration = end_seconds - start_seconds
    requested_count = duration / 1000 * sampling.fps
    count = (
        sampling.max_frames_per_chunk
        if requested_count >= sampling.max_frames_per_chunk
        else max(1, math.ceil(requested_count))
    )
    count = min(count, duration)
    return tuple(
        start_seconds + (2 * index + 1) * duration // (2 * count)
        for index in range(count)
    )


@dataclass(frozen=True, slots=True)
class OrderedFramesInputContract:
    """Structured rules for new builds; opaque IDs remain readable for legacy callers."""

    sampling_contract_id: str
    mime_type: str
    preprocess_revision: str
    kind: Literal["ordered_frames"] = "ordered_frames"
    sampling: OrderedFrameSamplingContract | None = None
    max_height: int | None = None

    def __post_init__(self) -> None:
        if self.kind != "ordered_frames":
            raise ValueError("ordered frames input kind must be ordered_frames")
        if any(
            not _nonblank(value)
            for value in (
                self.sampling_contract_id,
                self.mime_type,
                self.preprocess_revision,
            )
        ):
            raise ValueError("ordered frames input contract fields must be nonblank")
        if self.sampling is not None:
            if self.sampling_contract_id != self.sampling.sampling_contract_id:
                raise ValueError("sampling_contract_id differs from structured sampling")
            if (
                isinstance(self.max_height, bool)
                or not isinstance(self.max_height, int)
                or self.max_height <= 0
            ):
                raise ValueError("ordered frames max_height must be a positive integer")
        elif self.max_height is not None:
            raise ValueError("ordered frames max_height requires structured sampling")

    @classmethod
    def from_sampling(
        cls,
        *,
        sampling: OrderedFrameSamplingContract,
        max_height: int,
        mime_type: str,
        preprocess_revision: str,
    ) -> OrderedFramesInputContract:
        return cls(
            sampling_contract_id=sampling.sampling_contract_id,
            mime_type=mime_type,
            preprocess_revision=preprocess_revision,
            sampling=sampling,
            max_height=max_height,
        )

    def claims(self) -> dict[str, object]:
        claims: dict[str, object] = {
            "kind": self.kind,
            "sampling_contract_id": self.sampling_contract_id,
            "mime_type": self.mime_type,
            "preprocess_revision": self.preprocess_revision,
        }
        if self.sampling is not None:
            claims.update(sampling=self.sampling.claims(), max_height=self.max_height)
        return claims


VisualInputContract: TypeAlias = VideoClipInputContract | OrderedFramesInputContract


@dataclass(frozen=True, slots=True)
class VideoClipManifest:
    """A prepared segment; input_sha256 is the digest of its actual bytes."""

    input_sha256: str
    start_seconds: int
    end_seconds: int
    fps: float
    max_height: int
    mime_type: str
    preprocess_revision: str
    kind: Literal["video_clip"] = "video_clip"

    def __post_init__(self) -> None:
        if self.kind != "video_clip":
            raise ValueError("video clip manifest kind must be video_clip")
        if not is_sha256(self.input_sha256):
            raise ValueError("video clip input_sha256 must be lowercase SHA-256")
        _validate_bounds(self.start_seconds, self.end_seconds)
        contract = VideoClipInputContract(
            fps=self.fps,
            max_height=self.max_height,
            mime_type=self.mime_type,
            preprocess_revision=self.preprocess_revision,
        )
        object.__setattr__(self, "fps", contract.fps)

    @property
    def input_contract(self) -> VideoClipInputContract:
        return VideoClipInputContract(
            fps=self.fps,
            max_height=self.max_height,
            mime_type=self.mime_type,
            preprocess_revision=self.preprocess_revision,
        )


@dataclass(frozen=True, slots=True)
class OrderedFrame:
    """One prepared frame at an absolute integer-millisecond source-video time."""

    ordinal: int
    at_seconds: int
    input_sha256: str
    requested_at_seconds: int | None = None

    def __post_init__(self) -> None:
        if (
            isinstance(self.ordinal, bool)
            or not isinstance(self.ordinal, int)
            or self.ordinal < 0
        ):
            raise ValueError("frame ordinal must be a nonnegative integer")
        if (
            isinstance(self.at_seconds, bool)
            or not isinstance(self.at_seconds, int)
            or self.at_seconds < 0
        ):
            raise ValueError("frame at_seconds must be nonnegative integer milliseconds")
        if not is_sha256(self.input_sha256):
            raise ValueError("frame input_sha256 must be lowercase SHA-256")
        if self.requested_at_seconds is not None:
            if (
                isinstance(self.requested_at_seconds, bool)
                or not isinstance(self.requested_at_seconds, int)
                or self.requested_at_seconds < 0
            ):
                raise ValueError("requested frame time must be nonnegative integer milliseconds")


@dataclass(frozen=True, slots=True)
class OrderedFramesManifest:
    """Realized ordered frames without storage paths or provider request types."""

    frames: tuple[OrderedFrame, ...]
    sampling_contract_id: str
    mime_type: str
    preprocess_revision: str
    kind: Literal["ordered_frames"] = "ordered_frames"
    sampling: OrderedFrameSamplingContract | None = None
    max_height: int | None = None

    def __post_init__(self) -> None:
        if self.kind != "ordered_frames":
            raise ValueError("ordered frames manifest kind must be ordered_frames")
        frames = tuple(self.frames)
        object.__setattr__(self, "frames", frames)
        if not frames:
            raise ValueError("ordered frames manifest must contain at least one frame")
        if [frame.ordinal for frame in frames] != list(range(len(frames))):
            raise ValueError("frame ordinals must be consecutive and ordered")
        if any(
            current.at_seconds < previous.at_seconds
            for previous, current in zip(frames, frames[1:])
        ):
            raise ValueError("frames must be ordered by at_seconds")
        OrderedFramesInputContract(
            sampling_contract_id=self.sampling_contract_id,
            mime_type=self.mime_type,
            preprocess_revision=self.preprocess_revision,
            sampling=self.sampling,
            max_height=self.max_height,
        )

    @property
    def input_contract(self) -> OrderedFramesInputContract:
        return OrderedFramesInputContract(
            sampling_contract_id=self.sampling_contract_id,
            mime_type=self.mime_type,
            preprocess_revision=self.preprocess_revision,
            sampling=self.sampling,
            max_height=self.max_height,
        )

    def as_dict(self) -> dict[str, object]:
        """Persist rules and realized frames together, without provider payloads."""

        return {
            **self.input_contract.claims(),
            "frames": [
                {
                    "ordinal": frame.ordinal,
                    "at_seconds": frame.at_seconds,
                    "input_sha256": frame.input_sha256,
                    **(
                        {"requested_at_seconds": frame.requested_at_seconds}
                        if frame.requested_at_seconds is not None else {}
                    ),
                }
                for frame in self.frames
            ],
        }


VisualInputManifest: TypeAlias = VideoClipManifest | OrderedFramesManifest


def compute_visual_input_contract_id(contract: VisualInputContract) -> str:
    return f"{contract.kind}:sha256:{_sha256_json(contract.claims())}"


def compute_visual_input_revision(manifest: VisualInputManifest) -> str:
    """Return the exact encoder input identity for clip bytes or ordered frames."""

    if isinstance(manifest, VideoClipManifest):
        return manifest.input_sha256
    return _sha256_json(manifest.as_dict())


__all__ = [
    "VideoClipInputContract",
    "OrderedFrameSamplingContract",
    "plan_ordered_frame_timestamps",
    "OrderedFramesInputContract",
    "VideoClipManifest",
    "OrderedFrame",
    "OrderedFramesManifest",
    "VisualInputContract",
    "VisualInputManifest",
    "VIDEO_CLIP_PREPROCESS_REVISION",
    "compute_visual_input_contract_id",
    "compute_visual_input_revision"
]
