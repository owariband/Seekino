from __future__ import annotations

from pathlib import Path

import pytest
from engine.parse.schemas import (
    AnalysisWindow,
    ContextEvent,
    ContextSnapshot,
    OwnedTimelineEvent,
    SegmentParseResult,
    VideoParseModels,
    VideoParseResult,
)

from server.database import (
    DatabaseEnvironment,
    create_database_engine,
    create_schema,
    create_session_factory,
)
from server.service.parse_import import (
    PreparedVideoParseImport,
    prepare_video_parse_import,
)
from server.repository.video import VideoRepository


@pytest.fixture
def prepared_import(tmp_path: Path) -> PreparedVideoParseImport:
    source_video = tmp_path / "episode.mp4"
    source_video.write_bytes(b"source-video")
    canonical_directory = tmp_path / "canonical"
    canonical_directory.mkdir()
    analysis_directory = tmp_path / "analysis"
    analysis_directory.mkdir()

    initial = ContextSnapshot(
        snapshot_id="context-initial",
        after_segment_sequence_no=-1,
        compact_text="",
        recent_events=[],
    )
    segments: list[SegmentParseResult] = []
    all_events: list[OwnedTimelineEvent] = []
    context_before = initial
    for sequence_no in range(2):
        start_ms = sequence_no * 10_000
        end_ms = (sequence_no + 1) * 10_000
        analysis_path = analysis_directory / f"window-{sequence_no}.mp4"
        analysis_path.write_bytes(f"analysis-{sequence_no}".encode())
        canonical_path = canonical_directory / (
            f"segment-{sequence_no:04d}.analysis-"
            f"{start_ms:010d}-{end_ms:010d}.mp4"
        )
        canonical_path.write_bytes(f"canonical-{sequence_no}".encode())

        event = OwnedTimelineEvent(
            start_ms=start_ms + 1000,
            end_ms=start_ms + 3000,
            event_type="ACTION",
            description=f"第 {sequence_no + 1} 段动作",
            text=None,
            source_segment_sequence_no=sequence_no,
        )
        compact = (
            f"[{start_ms + 1000}-{start_ms + 3000}] "
            f"第 {sequence_no + 1} 段动作"
        )
        context_after = ContextSnapshot(
            snapshot_id=f"engine-context-{sequence_no}",
            after_segment_sequence_no=sequence_no,
            compact_text=compact,
            recent_events=[
                ContextEvent(
                    segment_sequence_no=sequence_no,
                    start_ms=event.start_ms,
                    end_ms=event.end_ms,
                    event_type=event.event_type,
                    description=event.description,
                    text=None,
                )
            ],
        )
        segment = SegmentParseResult(
            window=AnalysisWindow(
                sequence_no=sequence_no,
                source_video=source_video,
                media_path=analysis_path,
                segment_start_ms=start_ms,
                segment_end_ms=end_ms,
                analysis_start_ms=start_ms,
                analysis_end_ms=end_ms,
            ),
            context_before=context_before,
            candidate_transcript=[],
            is_fallback=False,
            window_summary=f"第 {sequence_no + 1} 段摘要",
            observed_events=[],
            events=[event],
            self_compact=compact,
            context_after=context_after,
        )
        segments.append(segment)
        all_events.append(event)
        context_before = context_after

    parse_result = VideoParseResult(
        models=VideoParseModels(
            transcript=None,
            transcript_mode="none",
            video_parser="test-video-model",
            fallback={
                "enabled": False,
                "transcript_model": "test-transcript-model",
                "provider": "deepseek",
                "visual_model": "test-visual-model",
                "refiner_model": "test-refiner-model",
                "input_mode": "ordered_frames",
                "frame_fps": 0.5,
                "group_duration_ms": 30_000,
                "max_frames_per_group": 15,
                "max_height": 720,
            },
        ),
        source_video=source_video,
        duration_ms=20_000,
        segment_duration_ms=10_000,
        context_overlap_ms=0,
        segments=segments,
        events=all_events,
    )
    parse_path = tmp_path / "pipeline.parse.json"
    parse_path.write_text(parse_result.model_dump_json(), encoding="utf-8")
    return prepare_video_parse_import(
        parse_path,
        canonical_directory,
        series_name="Test Series",
        series_description="isolated fixture",
        episode_order=7,
        pipeline_version="test-pipeline-v1",
    )


@pytest.fixture
def repository():
    engine = create_database_engine(
        "sqlite+pysqlite:///:memory:",
        environment=DatabaseEnvironment.TEST,
    )
    create_schema(engine)
    return engine, VideoRepository(create_session_factory(engine))
