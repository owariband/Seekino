"""Task-independent frame inspection over a host-authorized source and range."""
from __future__ import annotations

from collections.abc import Callable
from hashlib import sha256
import json
import logging
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from engine.common.frame_sampler import extract_video_frames, plan_frame_timestamps
from gateway.media import ToolImageArtifact

from .errors import AgentToolFeedbackError
from .tool import AgentTool, build_agent_tool
from .tool_result import JsonToolResult

_logger = logging.getLogger(__name__)


class InspectVideoFramesArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start_ms: int | None = Field(default=None, strict=True, ge=0)
    end_ms: int | None = Field(default=None, strict=True, ge=0)
    frame_count: int = Field(default=3, strict=True, ge=1, le=4)


def build_inspect_video_frames_tool(
    *, source_path: Path, read_bounds: tuple[int, int], max_height: int,
    timeout_seconds: float, check_control: Callable[[], None],
    trace_context: dict[str, str],
) -> AgentTool:
    def inspect_video_frames(
        start_ms: int | None = None, end_ms: int | None = None, frame_count: int = 3,
    ) -> JsonToolResult:
        check_control()
        start = read_bounds[0] if start_ms is None else start_ms
        end = read_bounds[1] if end_ms is None else end_ms
        if not read_bounds[0] <= start < end <= read_bounds[1] or frame_count > end - start:
            raise AgentToolFeedbackError(
                "INVALID_FRAME_RANGE", "Sampling must stay inside the authorized video range.",
                f"Use absolute integer milliseconds within [{read_bounds[0]}, {read_bounds[1]}) and 1–4 distinct positions.",
            )
        timestamps = [start + value for value in plan_frame_timestamps(
            end - start, frame_fps=frame_count * 1000 / (end - start), max_frames=frame_count,
        )]
        started = monotonic()
        identity = {**trace_context, "inspection_id": uuid4().hex}
        status = "failed"
        _logger.info("event=video_frame_inspection_started context=%s start_ms=%s end_ms=%s frame_count=%s timestamps_ms=%s",
                     json.dumps(identity), start, end, frame_count, timestamps)
        try:
            with TemporaryDirectory(prefix="anivag-tool-frames-") as directory:
                frames = []
                for index, timestamp in enumerate(timestamps):
                    check_control()
                    remaining = timeout_seconds - (monotonic() - started)
                    if remaining <= 0:
                        raise TimeoutError("Frame inspection deadline expired")
                    frames.extend(extract_video_frames(
                        source_path, [timestamp], output_dir=Path(directory) / str(index),
                        max_height=max_height, timeout_seconds=remaining,
                    ))
                check_control()
                if [timestamp for timestamp, _path in frames] != timestamps:
                    raise RuntimeError("Frame extractor returned unexpected sampling positions")
                artifact = ToolImageArtifact.from_paths([
                    (path, f"Source-video frame requested at {timestamp} ms.")
                    for timestamp, path in frames
                ])
                metadata = [{"requested_timestamp_ms": timestamp,
                             "sha256": sha256(path.read_bytes()).hexdigest(),
                             "size_bytes": path.stat().st_size} for timestamp, path in frames]
            check_control()
            if monotonic() - started >= timeout_seconds:
                raise TimeoutError("Frame inspection deadline expired")
            status = "success"
            return JsonToolResult(success=True, payload={
                **identity, "start_ms": start, "end_ms": end, "frame_count": len(metadata),
                "time_unit": "ms", "sampling": "uniform_midpoints", "frames": metadata,
                "images_supplied": bool(artifact.images),
                "limitations": ["Requested seek positions are not measured frame PTS.",
                                "Still frames contain no audio and do not prove continuous motion."],
            }, artifact=artifact)
        except Exception as error:
            _logger.warning("event=video_frame_inspection_failed context=%s error_type=%s",
                            json.dumps(identity), type(error).__name__)
            raise
        finally:
            _logger.info("event=video_frame_inspection_finished context=%s status=%s duration_ms=%s timestamps_ms=%s",
                         json.dumps(identity), status, round((monotonic() - started) * 1000), timestamps)

    return build_agent_tool(
        func=inspect_video_frames, name="inspect_video_frames",
        description=(
            f"View 1–4 actual frames from the bound source video within [{read_bounds[0]}, {read_bounds[1]}) ms. "
            "Times are absolute integer source-video milliseconds. Omit times for an overview or narrow "
            "the range to inspect a visual uncertainty. Images contain no audio; do not infer voice identity from them."
        ),
        args_schema=InspectVideoFramesArguments, maximum_result_bytes=16_384,
    )
