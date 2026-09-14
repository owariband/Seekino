from pathlib import Path

from engine.parse.analysis_windows import plan_analysis_windows
from engine.parse.frame_sampler import plan_frame_timestamps


def test_plan_analysis_windows_uses_non_overlapping_canonical_chunks() -> None:
    windows = plan_analysis_windows(
        Path("episode.mp4"),
        duration_ms=60_018,
        output_dir=Path("windows"),
        chunk_duration_ms=20_000,
        context_overlap_ms=2_000,
    )

    assert len(windows) == 3
    assert [
        (window.chunk_start_ms, window.chunk_end_ms) for window in windows
    ] == [(0, 20_000), (20_000, 40_000), (40_000, 60_018)]
    assert [
        (window.analysis_start_ms, window.analysis_end_ms)
        for window in windows
    ] == [(0, 22_000), (18_000, 42_000), (38_000, 60_018)]
    assert windows[1].relative_chunk_start_ms == 2_000
    assert windows[1].relative_chunk_end_ms == 22_000


def test_plan_frame_timestamps_preserves_66_second_fallback_density() -> None:
    timestamps = plan_frame_timestamps(
        66_000,
        frame_fps=0.5,
        max_frames=33,
    )

    assert len(timestamps) == 33
    assert timestamps[0] == 1000
    assert timestamps[-1] == 65_000
