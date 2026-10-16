from __future__ import annotations

import base64
import hashlib
import json
import logging
from pathlib import Path

import pytest
from PIL import Image

from agent.utils import video_frames
from gateway.media import ToolImageArtifact


@pytest.fixture
def frame_source(tmp_path, monkeypatch):
    source = tmp_path / "episode.mp4"
    source.write_bytes(b"source-video")
    image = tmp_path / "sample.jpg"
    Image.new("RGB", (8, 8), "green").save(image)
    image_bytes = image.read_bytes()
    calls = []

    def extract(path, timestamps, *, output_dir, max_height, timeout_seconds):
        directory = Path(output_dir)
        directory.mkdir(parents=True, exist_ok=True)
        calls.append({
            "source": Path(path), "timestamps": list(timestamps),
            "directory": directory, "max_height": max_height,
            "timeout_seconds": timeout_seconds,
        })
        frames = []
        for index, timestamp in enumerate(timestamps):
            frame = directory / f"frame-{index}.jpg"
            frame.write_bytes(image_bytes)
            frames.append((timestamp, frame))
        return frames

    monkeypatch.setattr(video_frames, "extract_video_frames", extract)
    return source, image_bytes, calls


def _tool(source, *, check_control=lambda: None):
    return video_frames.build_inspect_video_frames_tool(
        source_path=source, read_bounds=(10_000, 16_000), max_height=480,
        timeout_seconds=10, check_control=check_control,
        trace_context={"parse_task_id": "parse-test", "interval_id": "interval-test"},
    )


@pytest.mark.parametrize("arguments,bounds,count", [
    ({}, (10_000, 16_000), 3),
    ({"start_ms": 11_400, "end_ms": 12_600}, (11_400, 12_600), 3),
    ({"start_ms": 12_000, "frame_count": 1}, (12_000, 16_000), 1),
    ({"end_ms": 12_000, "frame_count": 4}, (10_000, 12_000), 4),
])
def test_frames_keep_absolute_positions_and_survive_temporary_cleanup(
    frame_source, arguments, bounds, count,
):
    source, image_bytes, calls = frame_source
    result = _tool(source).invoke({
        "type": "tool_call", "id": "inspect-test", "name": "inspect_video_frames",
        "args": arguments,
    })

    assert result.status == "success"
    payload = json.loads(result.content)
    assert (payload["start_ms"], payload["end_ms"]) == bounds
    assert payload["frame_count"] == count
    assert payload["time_unit"] == "ms" and payload["images_supplied"] is True
    assert payload["inspection_id"]
    timestamps = [stamp for call in calls for stamp in call["timestamps"]]
    assert len(timestamps) == len(set(timestamps)) == count
    assert all(bounds[0] <= stamp < bounds[1] for stamp in timestamps)
    assert [frame["requested_timestamp_ms"] for frame in payload["frames"]] == timestamps
    assert all(call["source"] == source and call["max_height"] == 480 for call in calls)
    assert all(0 < call["timeout_seconds"] <= 10 for call in calls)
    assert calls and all(not call["directory"].exists() for call in calls)
    assert isinstance(result.artifact, ToolImageArtifact)
    assert len(result.artifact.images) == count
    for image, frame in zip(result.artifact.images, payload["frames"], strict=True):
        assert image.data_url.startswith("data:image/jpeg;base64,")
        assert base64.b64decode(image.data_url.split(",", 1)[1], validate=True) == image_bytes
        assert image.size_bytes == frame["size_bytes"] == len(image_bytes)
        assert frame["sha256"] == hashlib.sha256(image_bytes).hexdigest()
    assert "artifact_uri" not in result.content and str(source) not in result.content


@pytest.mark.parametrize("arguments", [
    {"start_ms": True}, {"start_ms": 10_000.0}, {"start_ms": "10000"},
    {"end_ms": False}, {"end_ms": 15_000.5},
    {"start_ms": 9_999}, {"end_ms": 16_001},
    {"start_ms": 12_000, "end_ms": 12_000},
    {"start_ms": 13_000, "end_ms": 12_000},
    {"start_ms": 12_000, "end_ms": 12_002, "frame_count": 3},
    {"frame_count": 0}, {"frame_count": 5}, {"frame_count": True},
    {"frame_count": 3.0}, {"frame_count": "3"},
    {"source_path": "/different/episode.mp4"}, {"read_bounds": [0, 100_000]},
])
def test_invalid_or_source_overriding_arguments_never_extract(frame_source, arguments):
    source, _, calls = frame_source
    payload = json.loads(_tool(source).invoke(arguments))
    assert payload["error"]["code"]
    assert calls == []


def test_control_cancellation_propagates_before_extraction(frame_source):
    source, _, calls = frame_source

    class Cancelled(RuntimeError):
        pass

    def cancelled():
        raise Cancelled("parse lease cancelled")

    with pytest.raises(Cancelled, match="parse lease cancelled"):
        _tool(source, check_control=cancelled).execute()
    assert calls == []


def test_control_cancellation_after_extraction_cleans_temporary_images(frame_source):
    source, _, calls = frame_source

    def cancelled_after_read():
        if calls:
            raise RuntimeError("parse lease cancelled")

    with pytest.raises(RuntimeError, match="parse lease cancelled"):
        _tool(source, check_control=cancelled_after_read).execute()
    assert calls and all(not call["directory"].exists() for call in calls)


def test_extractor_timeout_is_not_downgraded_to_recoverable_feedback(frame_source, monkeypatch):
    source, _, calls = frame_source
    original = video_frames.extract_video_frames

    def timed_out(*args, **kwargs):
        original(*args, **kwargs)
        raise TimeoutError("frame extraction timed out")

    monkeypatch.setattr(video_frames, "extract_video_frames", timed_out)
    with pytest.raises(TimeoutError, match="frame extraction timed out"):
        _tool(source).execute()
    assert calls and all(not call["directory"].exists() for call in calls)


def test_total_deadline_includes_extraction_and_result_preparation(frame_source, monkeypatch):
    source, _, calls = frame_source
    original = video_frames.extract_video_frames
    now = [100.0]
    monkeypatch.setattr(video_frames, "monotonic", lambda: now[0])

    def slow_read(*args, **kwargs):
        frames = original(*args, **kwargs)
        now[0] += 11
        return frames

    monkeypatch.setattr(video_frames, "extract_video_frames", slow_read)
    with pytest.raises(TimeoutError):
        _tool(source).execute()
    assert calls and all(not call["directory"].exists() for call in calls)


def test_single_frame_encoding_cannot_return_success_after_deadline(frame_source, monkeypatch):
    source, _, calls = frame_source
    encode = ToolImageArtifact.from_paths
    now = [100.0]
    monkeypatch.setattr(video_frames, "monotonic", lambda: now[0])

    def slow_encoding(cls, *args, **kwargs):
        artifact = encode(*args, **kwargs)
        now[0] += 11
        return artifact

    monkeypatch.setattr(ToolImageArtifact, "from_paths", classmethod(slow_encoding))
    with pytest.raises(TimeoutError):
        _tool(source).execute(frame_count=1)
    assert len(calls) == 1
    assert all(not call["directory"].exists() for call in calls)


def test_frame_logs_retain_trace_and_timestamps_without_image_payload(frame_source, caplog):
    source, image_bytes, calls = frame_source
    with caplog.at_level(logging.INFO):
        result = _tool(source).execute(start_ms=11_400, end_ms=12_600)
    assert result.success
    records = "\n".join(f"{record.getMessage()} {record.__dict__!r}" for record in caplog.records)
    assert "parse-test" in records and "interval-test" in records
    for call in calls:
        for timestamp in call["timestamps"]:
            assert str(timestamp) in records
    assert base64.b64encode(image_bytes).decode("ascii") not in records
    assert "data:image/" not in records
