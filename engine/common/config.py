from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

from gateway.config import VideoFallbackConfig


@dataclass(frozen=True, slots=True)
class VideoParseRuntimeConfig:
    chunk_duration_seconds: int = 60
    context_overlap_seconds: int = 3
    max_height: int = 720
    asr_concurrency: int = 4
    asr_mode: Literal["lite", "none"] = "lite"
    request_timeout_seconds: float = 600
    max_context_events: int = 8
    max_context_chars: int = 2000
    transcript_model: str | None = None
    video_model: str | None = None
    transcript_fps: float | None = None
    video_fps: float | None = None
    fallback_enabled: bool | None = None
    fallback_config: VideoFallbackConfig | None = None
    video_input_mode: Literal["video", "ordered_frames"] = "video"
    video_max_frames_per_window: int | None = None
    transcript_max_output_tokens: int | None = None
    video_max_output_tokens: int | None = None
    batch_max_size: int = 10
    dispatch_interval_seconds: float = 0

    def __post_init__(self) -> None:
        if type(self.batch_max_size) is not int or self.batch_max_size <= 0:
            raise ValueError("batch_max_size must be a positive integer")
        if (
            isinstance(self.dispatch_interval_seconds, bool)
            or not isinstance(self.dispatch_interval_seconds, (float, int))
            or not math.isfinite(self.dispatch_interval_seconds)
            or self.dispatch_interval_seconds < 0
        ):
            raise ValueError("dispatch_interval_seconds must be finite and nonnegative")
        if self.chunk_duration_seconds <= 0:
            raise ValueError("chunk_duration_seconds must be greater than zero")
        if self.context_overlap_seconds < 0:
            raise ValueError("context_overlap_seconds cannot be negative")
        if self.max_height <= 0:
            raise ValueError("max_height must be greater than zero")
        if self.asr_concurrency <= 0:
            raise ValueError("asr_concurrency must be greater than zero")
        if self.request_timeout_seconds <= 0:
            raise ValueError("request_timeout_seconds must be greater than zero")
        if (self.video_input_mode == "ordered_frames") != (
            self.video_max_frames_per_window is not None
        ):
            raise ValueError("ordered_frames requires video_max_frames_per_window")
        if self.video_max_frames_per_window is not None and self.video_max_frames_per_window <= 0:
            raise ValueError("video_max_frames_per_window must be positive")
        for name in ("transcript_max_output_tokens", "video_max_output_tokens"):
            value = getattr(self, name)
            if value is not None and value <= 0:
                raise ValueError(f"{name} must be positive")
