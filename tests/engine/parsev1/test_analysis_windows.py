import json
from pathlib import Path
from types import SimpleNamespace

from engine.parsev1.analysis_windows import (
    materialize_canonical_chunks,
    plan_analysis_windows,
    probe_video_duration_seconds,
)
from engine.parsev1.frame_sampler import plan_frame_timestamps


def test_probe_video_duration_preserves_milliseconds_and_ignores_audio_tail(
    tmp_path,
    monkeypatch,
) -> None:
    video = tmp_path / "episode.mp4"
    video.write_bytes(b"video")
    monkeypatch.setattr(
        "engine.parsev1.analysis_windows.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(stdout=json.dumps({
            "streams": [{"duration": "8.008"}], "format": {"duration": "8.999"},
        })),
    )

    assert probe_video_duration_seconds(video) == 8008


def test_probe_video_duration_uses_stream_time_base_when_duration_is_missing(tmp_path, monkeypatch):
    video = tmp_path / "episode.mp4"
    video.write_bytes(b"video")
    monkeypatch.setattr(
        "engine.parsev1.analysis_windows.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(stdout=json.dumps({
            "streams": [{"duration": "N/A", "duration_ts": 192192, "time_base": "1/24000"}],
        })),
    )
    assert probe_video_duration_seconds(video) == 8008


def test_probe_video_duration_uses_actual_frame_times_without_stream_duration(tmp_path, monkeypatch):
    video = tmp_path / "episode.webm"
    video.write_bytes(b"video")
    responses = iter([
        {"streams": [{"avg_frame_rate": "25/1"}]},
        {"frames": [
            {"best_effort_timestamp_time": "5.000", "duration_time": "0.040"},
            {"best_effort_timestamp_time": "5.040"},
            {"best_effort_timestamp_time": "5.080", "pkt_duration_time": "0.040"},
        ]},
    ])
    monkeypatch.setattr(
        "engine.parsev1.analysis_windows.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(stdout=json.dumps(next(responses))),
    )
    assert probe_video_duration_seconds(video) == 120


def test_fractional_video_duration_keeps_every_sample_inside_the_video():
    timestamps = plan_frame_timestamps(8008, frame_fps=1, max_frames=100)
    assert len(timestamps) == 9
    assert timestamps[-1] == 7563
    assert all(type(timestamp) is int and 0 <= timestamp < 8008 for timestamp in timestamps)


def test_sub_second_video_and_dense_sampling_keep_integer_half_open_positions():
    assert plan_frame_timestamps(250, frame_fps=2, max_frames=10) == [125]
    assert plan_frame_timestamps(1, frame_fps=10000, max_frames=10) == [0]


def test_plan_analysis_windows_uses_non_overlapping_canonical_chunks() -> None:
    windows = plan_analysis_windows(
        Path("episode.mp4"),
        duration_seconds=61000,
        output_dir=Path("windows"),
        chunk_duration_seconds=20,
        context_overlap_seconds=2,
    )

    assert len(windows) == 3
    assert [
        (window.chunk_start_seconds, window.chunk_end_seconds) for window in windows
    ] == [(0, 20000), (20000, 40000), (40000, 61000)]
    assert [
        (window.analysis_start_seconds, window.analysis_end_seconds)
        for window in windows
    ] == [(0, 22000), (18000, 42000), (38000, 61000)]
    assert windows[1].relative_chunk_start_seconds == 2000
    assert windows[1].relative_chunk_end_seconds == 22000


def test_plan_frame_timestamps_preserves_66_second_fallback_density() -> None:
    timestamps = plan_frame_timestamps(
        66000,
        frame_fps=0.5,
        max_frames=33,
    )

    assert len(timestamps) == 33
    assert timestamps[0] == 1000
    assert timestamps[-1] == 65000


def test_plan_frame_timestamps_supports_multiple_frames_per_second() -> None:
    timestamps = plan_frame_timestamps(
        2000,
        frame_fps=2.0,
        max_frames=10,
    )

    assert timestamps == [250, 750, 1250, 1750]
    assert len(set(timestamps)) == 4


def test_canonical_media_uses_chunk_boundaries_without_context_overlap(
    tmp_path,
    monkeypatch,
) -> None:
    source = tmp_path / "episode.mp4"
    source.write_bytes(b"source")
    windows = plan_analysis_windows(
        source,
        duration_seconds=40000,
        output_dir=tmp_path / "windows",
        chunk_duration_seconds=20,
        context_overlap_seconds=3,
    )
    commands: list[list[str]] = []

    def run(command, **_kwargs):
        commands.append(command)
        Path(command[-1]).write_bytes(b"canonical")

    monkeypatch.setattr("engine.parsev1.analysis_windows.subprocess.run", run)

    paths = materialize_canonical_chunks(
        windows,
        output_dir=tmp_path / "canonical",
    )

    assert [path.name for path in paths] == ["chunk-0000.mp4", "chunk-0001.mp4"]
    assert [command[command.index("-ss") + 1] for command in commands] == [
        "0.000",
        "20.000",
    ]
    assert [command[command.index("-t") + 1] for command in commands] == [
        "20.000",
        "20.000",
    ]
