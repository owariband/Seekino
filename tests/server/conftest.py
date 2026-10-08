from __future__ import annotations

from pathlib import Path

import pytest
from engine.parsev1.schemas import (
    AnalysisWindow,
    ContextEvent,
    ContextSnapshot,
    OwnedTimelineEvent,
    VideoChunkParseResult,
    VideoParseModels,
    VideoParseResult,
)

from server.database import (
    DatabaseEnvironment,
    create_database_engine,
    create_schema,
    create_session_factory,
)
from server.model.dto.parseDTO import PreparedVideoParseImport
from server.service.parsev1.parse_import import prepare_video_parse_import
from server.repository.video import VideoRepository


@pytest.fixture
def prepared_import(tmp_path: Path, monkeypatch) -> PreparedVideoParseImport:
    source_video = tmp_path / "episode.mp4"
    source_video.write_bytes(b"source-video")
    canonical_directory = tmp_path / "canonical"
    canonical_directory.mkdir()
    analysis_directory = tmp_path / "analysis"
    analysis_directory.mkdir()

    initial = ContextSnapshot(
        snapshot_id="context-initial",
        after_chunk_sequence_no=-1,
        compact_text="",
        recent_events=[],
    )
    chunks: list[VideoChunkParseResult] = []
    all_events: list[OwnedTimelineEvent] = []
    context_before = initial
    for sequence_no in range(2):
        start_seconds = sequence_no * 10
        end_seconds = (sequence_no + 1) * 10
        analysis_path = analysis_directory / f"window-{sequence_no}.mp4"
        analysis_path.write_bytes(f"analysis-{sequence_no}".encode())
        canonical_path = canonical_directory / f"chunk-{sequence_no:04d}.mp4"
        canonical_path.write_bytes(f"canonical-{sequence_no}".encode())

        event = OwnedTimelineEvent(
            start_seconds=start_seconds + 1,
            end_seconds=start_seconds + 3,
            event_type="ACTION",
            description=f"第 {sequence_no + 1} 段动作",
            text=None,
            source_chunk_sequence_no=sequence_no,
        )
        compact = (
            f"[{start_seconds + 1}-{start_seconds + 3}] "
            f"第 {sequence_no + 1} 段动作"
        )
        context_after = ContextSnapshot(
            snapshot_id=f"engine-context-{sequence_no}",
            after_chunk_sequence_no=sequence_no,
            compact_text=compact,
            recent_events=[
                ContextEvent(
                    chunk_sequence_no=sequence_no,
                    start_seconds=event.start_seconds,
                    end_seconds=event.end_seconds,
                    event_type=event.event_type,
                    description=event.description,
                    text=None,
                )
            ],
        )
        chunk = VideoChunkParseResult(
            window=AnalysisWindow(
                sequence_no=sequence_no,
                source_video=source_video,
                media_path=analysis_path,
                chunk_start_seconds=start_seconds,
                chunk_end_seconds=end_seconds,
                analysis_start_seconds=start_seconds,
                analysis_end_seconds=end_seconds,
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
        chunks.append(chunk)
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
                "group_duration_seconds": 30,
                "max_frames_per_group": 15,
                "max_height": 720,
            },
        ),
        source_video=source_video,
        duration_seconds=20,
        chunk_duration_seconds=10,
        context_overlap_seconds=0,
        chunks=chunks,
        events=all_events,
    )
    parse_path = tmp_path / "pipeline.parse.json"
    parse_path.write_text(parse_result.model_dump_json(), encoding="utf-8")
    # Fake source bytes test contracts; the declared duration and coordinates are milliseconds.
    monkeypatch.setattr(
        "server.service.parsev1.parse_import.probe_video_duration_seconds",
        lambda _path: parse_result.duration_seconds,
    )
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
