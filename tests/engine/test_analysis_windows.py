from pathlib import Path
from types import SimpleNamespace

from engine.parse.analysis_windows import (
    materialize_canonical_chunks,
    plan_analysis_windows,
    probe_video_duration_seconds,
)
from engine.parse.frame_sampler import plan_frame_timestamps


def test_probe_video_duration_rounds_up_to_the_containing_second(
    tmp_path,
    monkeypatch,
) -> None:
    video = tmp_path / "episode.mp4"
    video.write_bytes(b"video")
    monkeypatch.setattr(
        "engine.parse.analysis_windows.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(stdout="60.018\n"),
    )

    assert probe_video_duration_seconds(video) == 61


def test_plan_analysis_windows_uses_non_overlapping_canonical_chunks() -> None:
    windows = plan_analysis_windows(
        Path("episode.mp4"),
        duration_seconds=61,
        output_dir=Path("windows"),
        chunk_duration_seconds=20,
        context_overlap_seconds=2,
    )

    assert len(windows) == 3
    assert [
        (window.chunk_start_seconds, window.chunk_end_seconds) for window in windows
    ] == [(0, 20), (20, 40), (40, 61)]
    assert [
        (window.analysis_start_seconds, window.analysis_end_seconds)
        for window in windows
    ] == [(0, 22), (18, 42), (38, 61)]
    assert windows[1].relative_chunk_start_seconds == 2
    assert windows[1].relative_chunk_end_seconds == 22


def test_plan_frame_timestamps_preserves_66_second_fallback_density() -> None:
    timestamps = plan_frame_timestamps(
        66,
        frame_fps=0.5,
        max_frames=33,
    )

    assert len(timestamps) == 33
    assert timestamps[0] == 1
    assert timestamps[-1] == 65


def test_plan_frame_timestamps_supports_multiple_frames_per_second() -> None:
    timestamps = plan_frame_timestamps(
        2,
        frame_fps=2.0,
        max_frames=10,
    )

    assert timestamps == [0.25, 0.75, 1.25, 1.75]
    assert len(set(timestamps)) == 4


def test_canonical_media_uses_chunk_boundaries_without_context_overlap(
    tmp_path,
    monkeypatch,
) -> None:
    source = tmp_path / "episode.mp4"
    source.write_bytes(b"source")
    windows = plan_analysis_windows(
        source,
        duration_seconds=40,
        output_dir=tmp_path / "windows",
        chunk_duration_seconds=20,
        context_overlap_seconds=3,
    )
    commands: list[list[str]] = []

    def run(command, **_kwargs):
        commands.append(command)
        Path(command[-1]).write_bytes(b"canonical")

    monkeypatch.setattr("engine.parse.analysis_windows.subprocess.run", run)

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
