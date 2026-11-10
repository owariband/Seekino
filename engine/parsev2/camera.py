"""Observed shot movement and an editable 2-D interpretation for an editing client."""

from __future__ import annotations

from bisect import bisect_left
from importlib.resources import files
from statistics import median
from typing import Annotated, Literal, NamedTuple, NotRequired, Sequence, TypedDict

from pydantic import BaseModel, ConfigDict, Field

CameraMove = Literal[
    "PAN_RIGHT",
    "PAN_LEFT",
    "TILT_UP",
    "TILT_DOWN",
    "PUSH_IN",
    "PULL_OUT",
    "STATIC",
    "FOLLOW",
    "ORBIT",
    "ROLL",
    "COMPOUND",
    "UNKNOWN",
]
CameraTiming = Literal["STEADY", "EASE_IN", "EASE_OUT", "EASE_IN_OUT", "STEPPED", "VARIABLE", "UNKNOWN"]
_Observation = Annotated[str, Field(min_length=1, max_length=240, pattern=r"\S")]


class SampledPan(TypedDict):
    first_sample_ms: int
    last_sample_ms: int
    if_background_is_fixed: Literal["PAN_LEFT", "PAN_RIGHT"]
    sampled_timing: CameraTiming
    travel_in_frame_widths: float
    samples: NotRequired[list[tuple[int, float]]]


def pan_timing(samples: list[tuple[int, float]]) -> CameraTiming:
    speed = [abs(b - a) * 1000 / (end - start) for (start, a), (end, b) in zip(samples, samples[1:])]
    if len(speed) < 7:
        return "UNKNOWN"
    part = max(2, len(speed) // 4)
    start, middle, end = median(speed[:part]), median(speed[part:-part]), median(speed[-part:])
    if middle <= 0:
        return "UNKNOWN"
    slow_start = speed[0] < 0.75 * middle and start < 0.90 * middle
    slow_end = speed[-1] < 0.75 * middle and end < 0.90 * middle
    if slow_start and slow_end:
        return "EASE_IN_OUT"
    if slow_start and end >= 0.8 * middle:
        return "EASE_IN"
    if slow_end and start >= 0.8 * middle:
        return "EASE_OUT"
    if min(speed) >= 0.75 * middle and max(speed) <= 1.25 * middle:
        return "STEADY"
    return "UNKNOWN"


_MOVE_NAMES = {
    "PAN_RIGHT": "镜头向右横移",
    "PAN_LEFT": "镜头向左横移",
    "TILT_UP": "镜头向上移",
    "TILT_DOWN": "镜头向下移",
    "PUSH_IN": "镜头推近",
    "PULL_OUT": "镜头拉远",
    "STATIC": "固定机位",
    "FOLLOW": "跟随主体",
    "ORBIT": "环绕主体",
    "ROLL": "画面旋转",
    "COMPOUND": "复合运镜",
    "UNKNOWN": "运镜方式未确定",
}
_TIMING_NAMES = {
    "STEADY": "匀速",
    "EASE_IN": "缓入加速",
    "EASE_OUT": "减速缓出",
    "EASE_IN_OUT": "平滑起步并收住",
    "STEPPED": "阶梯式",
    "VARIABLE": "速度变化",
    "UNKNOWN": "速度未确定",
}


class CameraReference(BaseModel):
    """One within-shot observation. Temporal bounds belong to the enclosing SCENE."""

    model_config = ConfigDict(extra="forbid", revalidate_instances="always")
    movement: CameraMove = Field(description="Direction of the viewport, not the subject or background pixels")
    timing: CameraTiming = Field(description="Visible pace; UNKNOWN when sampling cannot establish it")
    start_framing: _Observation = Field(description="What is visible at the start of this move")
    end_framing: _Observation = Field(description="What is visible at the end, including any reveal")
    visual_evidence: _Observation = Field(
        description="Observed background or frame-edge change that supports the classification"
    )

    def describe(self) -> str:
        start = self.start_framing.rstrip("。.!！ ")
        end = self.end_framing.rstrip("。.!！ ")
        movement = _MOVE_NAMES[self.movement]
        if self.movement != "STATIC":
            movement += "，" + _TIMING_NAMES[self.timing]
        return (
            f"{movement}。"
            f"起幅：{start}。落幅：{end}。画面依据：{self.visual_evidence}"
        )


# Anchors are fractions of the *available* travel after fitting the new artwork.
# Zoom ratios and full-width travel are editable composition choices, never measurements.
_CENTRE = (0.5, 0.5, 1.0)
_VIEWPORT_V1 = {
    "PAN_RIGHT": ((0, 0.5, 1), (1, 0.5, 1)),
    "PAN_LEFT": ((1, 0.5, 1), (0, 0.5, 1)),
    "TILT_UP": ((0.5, 1, 1), (0.5, 0, 1)),
    "TILT_DOWN": ((0.5, 0, 1), (0.5, 1, 1)),
    "PUSH_IN": (_CENTRE, (0.5, 0.5, 1.4)),
    "PULL_OUT": ((0.5, 0.5, 1.4), _CENTRE),
    "STATIC": (_CENTRE, _CENTRE),
}


class MatchedPan(NamedTuple):
    observation: SampledPan
    source_points: list[tuple[int, float]]


def covering_pan(samples: Sequence[SampledPan], start_ms: int, end_ms: int) -> MatchedPan | None:
    """A single unbroken measured run must account for most of this exact event."""
    overlapping = [s for s in samples if s["first_sample_ms"] < end_ms and start_ms < s["last_sample_ms"]]
    if len(overlapping) != 1:
        return None
    run = overlapping[0]
    first, last = run["first_sample_ms"], run["last_sample_ms"]
    source_points = run.get("samples", [])
    if start_ms <= first < last <= end_ms and last - first >= 0.8 * (end_ms - start_ms):
        return MatchedPan(run, source_points)
    points = [(at, x) for at, x in source_points if start_ms <= at < end_ms]
    if len(points) >= 4 and points[-1][0] - points[0][0] >= 0.8 * (end_ms - start_ms):
        points = [(at, round(x - points[0][1], 5)) for at, x in points]
        if abs(points[-1][1]) >= 0.05 and sum(a != b for (_, a), (_, b) in zip(points, points[1:])) >= 3:
            observation: SampledPan = {
                **run,
                "first_sample_ms": points[0][0],
                "last_sample_ms": points[-1][0],
                "sampled_timing": pan_timing(points),
                "travel_in_frame_widths": round(abs(points[-1][1]), 3),
                "samples": points,
            }
            return MatchedPan(observation, source_points)
    return None


def _position_at(points: list[tuple[int, float]], at: int) -> float:
    """Interpolate a measured run at a scene boundary. Hold outside the measured span."""
    i = bisect_left(points, at, key=lambda point: point[0])
    if i == 0:
        return points[0][1]
    if i == len(points):
        return points[-1][1]
    t0, x0 = points[i - 1]
    t1, x1 = points[i]
    return x0 + (x1 - x0) * ((at - t0) / (t1 - t0))


def camera_replay(
    reference: CameraReference, start_ms: int, end_ms: int, samples: Sequence[SampledPan]
) -> dict | None:
    """Resolve the verified event to a pinned, portable example, without running source code."""
    anchors = _VIEWPORT_V1.get(reference.movement)
    if anchors is None or reference.timing in {"STEPPED", "VARIABLE"}:
        return None
    duration_ms = end_ms - start_ms
    timing = reference.timing
    basis = "observed_category" if timing != "UNKNOWN" else "editor_default"
    params = {"duration_ms": duration_ms, "from": list(anchors[0]), "to": list(anchors[1])}
    sample = None
    if reference.movement in {"PAN_LEFT", "PAN_RIGHT"}:
        match = covering_pan(samples, start_ms, end_ms)
        if match is not None:
            sample, source_points = match
            if sample["if_background_is_fixed"] != reference.movement:
                return None
            params["travel_in_view_widths"] = sample["travel_in_frame_widths"]
            if len(source_points) >= 4:
                origin = _position_at(source_points, start_ms)
                finish = _position_at(source_points, end_ms)
                points = [(start_ms, origin)]
                points.extend((t, x) for t, x in source_points if start_ms < t < end_ms)
                points.append((end_ms, finish))
                # Keep the original source timing and cap the portable representation.
                if len(points) > 96:
                    points = [points[round(i * (len(points) - 1) / 95)] for i in range(96)]
                params["travel_in_view_widths"] = round(abs(finish - origin), 5)
                params["progress_samples"] = [
                    [(at - start_ms) / duration_ms, (x - origin) / (finish - origin)] for at, x in points
                ]
            if sample["sampled_timing"] != "UNKNOWN":
                timing, basis = sample["sampled_timing"], "sampled_background"
    params["easing"] = "EASE_IN_OUT" if timing == "UNKNOWN" else timing
    replay = {
        "template_id": "canvas2d/viewport@1",
        "entry": "cameraAt",
        "filename": "viewport-v1.mjs",
        "code": files("engine.parsev2").joinpath("viewport-v1.mjs").read_text(encoding="utf-8"),
        "params": params,
        "easing_basis": basis,
        "anchor_basis": "editable_composition_defaults",
        "travel_basis": "sampled_background" if sample is not None else "editable_composition_defaults",
    }
    if sample is not None:
        replay["sampled_background"] = {k: v for k, v in sample.items() if k != "samples"}
        replay["sampled_background"]["sample_count"] = len(sample.get("samples", []))
        if "progress_samples" in params:
            replay["sampled_background"]["boundary_policy"] = "interpolate_within_run_hold_outside"
    return replay
