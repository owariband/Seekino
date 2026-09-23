from pathlib import Path

import pytest

from engine.parse.cli import _load_completed_chunks
from engine.parse.schemas import (
    AnalysisWindow,
    ContextSnapshot,
    VideoChunkCheckpoint,
    VideoChunkParseResult,
    VideoParseProvenance,
)


def _window() -> AnalysisWindow:
    return AnalysisWindow(
        sequence_no=0,
        source_video=Path("episode.mp4"),
        media_path=Path(
            "windows/chunk-0000.analysis-0000000000-0000000063.mp4"
        ),
        chunk_start_seconds=0,
        chunk_end_seconds=60,
        analysis_start_seconds=0,
        analysis_end_seconds=63,
    )


def _write_fallback_checkpoint(
    output_dir: Path,
    *,
    provenance_updates: dict[str, object] | None = None,
) -> AnalysisWindow:
    window = _window()
    checkpoint_window = window.model_copy(
        update={
            "media_path": Path(
                "packages/engine/outputs/legacy/windows/"
                "segment-0000.analysis-0000000000-0000000063.mp4"
            )
        }
    )
    provenance_values: dict[str, object] = {
        "provider": "deepseek",
        "model": "deepseek-flash",
        "input_mode": "ordered_frames",
        "frame_fps": 0.5,
        "frame_timestamps_seconds": [1, 3],
        "group_frame_counts": [15, 15, 2],
        "group_duration_seconds": 30,
        "max_frames_per_group": 15,
        "fallback_transcript_model": "doubao-lite",
        "visual_model": "deepseek-flash",
        "refiner_model": "deepseek-flash",
        "fallback_reason": "content_filter",
        "primary_model": "doubao-pro",
    }
    provenance_values.update(provenance_updates or {})
    before = ContextSnapshot(
        snapshot_id="context-before",
        after_chunk_sequence_no=-1,
        compact_text="",
        recent_events=[],
    )
    after = ContextSnapshot(
        snapshot_id="context-after",
        after_chunk_sequence_no=0,
        compact_text="fallback summary",
        recent_events=[],
    )
    checkpoint = VideoChunkCheckpoint(
        transcript_model="doubao-lite",
        transcript_source="none",
        video_model="doubao-pro",
        chunk=VideoChunkParseResult(
            window=checkpoint_window,
            context_before=before,
            candidate_transcript=[],
            video_parse=VideoParseProvenance.model_validate(provenance_values),
            is_fallback=True,
            window_summary="fallback summary",
            observed_events=[],
            events=[],
            self_compact="fallback summary",
            context_after=after,
            context_before_compact="",
            context_after_compact="fallback summary",
        ),
    )
    path = output_dir / "segments" / "segment-0000.json"
    path.parent.mkdir(parents=True)
    path.write_text(checkpoint.model_dump_json(), encoding="utf-8")
    return window


def _resume(output_dir: Path, window: AnalysisWindow) -> list[VideoChunkParseResult]:
    return _load_completed_chunks(
        [window],
        output_dir=output_dir,
        transcript_model="doubao-lite",
        transcript_source="none",
        video_model="doubao-pro",
        fallback_provider="deepseek",
        fallback_model="deepseek-flash",
        fallback_transcript_model="doubao-lite",
        fallback_frame_fps=0.5,
        fallback_group_duration_seconds=30,
        fallback_max_frames_per_group=15,
    )


def test_load_completed_chunks_accepts_legacy_path_and_matching_strategy(
    tmp_path,
) -> None:
    window = _write_fallback_checkpoint(tmp_path)

    completed = _resume(tmp_path, window)

    assert len(completed) == 1
    assert completed[0].window == window
    assert completed[0].video_parse is not None
    assert completed[0].video_parse.provider == "deepseek"


@pytest.mark.parametrize(
    ("field", "changed_value"),
    [
        ("frame_fps", 1.0),
        ("group_duration_seconds", 20),
        ("max_frames_per_group", 10),
        ("fallback_transcript_model", "other-lite"),
        ("visual_model", "other-vision-model"),
        ("refiner_model", "other-refiner-model"),
    ],
)
def test_load_completed_chunks_rejects_changed_fallback_strategy(
    tmp_path,
    field,
    changed_value,
) -> None:
    window = _write_fallback_checkpoint(
        tmp_path,
        provenance_updates={field: changed_value},
    )

    with pytest.raises(RuntimeError, match="fallback strategy changed"):
        _resume(tmp_path, window)
