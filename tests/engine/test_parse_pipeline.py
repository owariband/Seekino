from collections.abc import Sequence
from pathlib import Path

from engine.parse.background import BackgroundImage
from engine.parse.schemas import (
    AnalysisWindow,
    TimelineEvent,
    TranscriptAnalysis,
    VideoEvidence,
    VideoChunkCheckpoint,
    VideoChunkParseResult,
    VideoParseProvenance,
    VideoParseResult,
)
from engine.parse.video_parse import ParsePipeline


def _event(
    start_ms: int,
    end_ms: int,
    description: str,
) -> TimelineEvent:
    return TimelineEvent(
        start_ms=start_ms,
        end_ms=end_ms,
        event_type="ACTION",
        description=description,
        text=None,
    )


class FakeParser:
    def __init__(self) -> None:
        self.contexts: list[str] = []
        self.background_images: list[tuple[BackgroundImage, ...]] = []
        self._events = [
            [
                _event(1000, 3000, "第一段已确认动作"),
                _event(9000, 11_000, "跨越第一条边界的动作"),
            ],
            [
                _event(1000, 3000, "跨越第一条边界的动作"),
                _event(11_500, 13_500, "跨越第二条边界的动作"),
            ],
            [
                _event(1500, 3500, "跨越第二条边界的动作"),
                _event(5000, 6000, "第三段已确认动作"),
            ],
        ]

    def analyze(
        self,
        video_path: str,
        *,
        context: str | None = None,
        duration_ms: int | None = None,
        candidate_transcript: TranscriptAnalysis | None = None,
        background_images: Sequence[BackgroundImage] = (),
    ) -> VideoEvidence:
        index = int(Path(video_path).stem.rsplit("-", 1)[-1])
        assert duration_ms is not None
        assert Path(video_path).name == f"window-{index}.mp4"
        self.contexts.append(context or "")
        self.background_images.append(tuple(background_images))
        return VideoEvidence(
            duration_ms=duration_ms,
            candidate_transcript=[],
            summary=f"window {index}",
            events=self._events[index],
        )


class FakeFallbackParser(FakeParser):
    def analyze(
        self,
        video_path: str,
        *,
        context: str | None = None,
        duration_ms: int | None = None,
        candidate_transcript: TranscriptAnalysis | None = None,
        background_images: Sequence[BackgroundImage] = (),
    ) -> VideoEvidence:
        evidence = super().analyze(
            video_path,
            context=context,
            duration_ms=duration_ms,
            candidate_transcript=candidate_transcript,
            background_images=background_images,
        )
        return evidence.model_copy(
            update={
                "provenance": VideoParseProvenance(
                    provider="ollama",
                    model="local-vision-model",
                    input_mode="ordered_frames",
                    fallback_reason="content_filter",
                    primary_model="doubao-pro",
                )
            }
        )


def _windows() -> list[AnalysisWindow]:
    source = Path("episode.mp4")
    return [
        AnalysisWindow(
            sequence_no=0,
            source_video=source,
            media_path=Path("window-0.mp4"),
            chunk_start_ms=0,
            chunk_end_ms=10_000,
            analysis_start_ms=0,
            analysis_end_ms=12_000,
        ),
        AnalysisWindow(
            sequence_no=1,
            source_video=source,
            media_path=Path("window-1.mp4"),
            chunk_start_ms=10_000,
            chunk_end_ms=20_000,
            analysis_start_ms=8_000,
            analysis_end_ms=22_000,
        ),
        AnalysisWindow(
            sequence_no=2,
            source_video=source,
            media_path=Path("window-2.mp4"),
            chunk_start_ms=20_000,
            chunk_end_ms=30_000,
            analysis_start_ms=18_000,
            analysis_end_ms=30_000,
        ),
    ]


def test_pipeline_passes_only_owned_past_events_to_the_next_chunk() -> None:
    parser = FakeParser()
    checkpoints = []
    result = ParsePipeline(parser).run(
        source_video="episode.mp4",
        duration_ms=30_000,
        chunk_duration_ms=10_000,
        context_overlap_ms=2_000,
        windows=_windows(),
        series_bible="角色术语表",
        on_chunk_complete=checkpoints.append,
    )

    assert len(checkpoints) == 3
    assert "角色术语表" in parser.contexts[0]
    assert "首段，无历史事件" in parser.contexts[0]
    assert "第一段已确认动作" in parser.contexts[1]
    assert "跨越第一条边界的动作" not in parser.contexts[1]
    assert "跨越第一条边界的动作" in parser.contexts[2]
    assert "跨越第二条边界的动作" not in parser.contexts[2]

    assert [event.description for event in result.events] == [
        "第一段已确认动作",
        "跨越第一条边界的动作",
        "跨越第二条边界的动作",
        "第三段已确认动作",
    ]
    assert [(event.start_ms, event.end_ms) for event in result.events] == [
        (1000, 3000),
        (9000, 11_000),
        (19_500, 21_500),
        (23_000, 24_000),
    ]
    assert (
        result.chunks[1].context_before.snapshot_id
        == result.chunks[0].context_after.snapshot_id
    )
    assert result.chunks[0].observed_events[1].owned_by_current_chunk is False
    assert result.chunks[1].observed_events[0].owned_by_current_chunk is True
    restored = VideoParseResult.model_validate_json(result.model_dump_json())
    assert restored == result


def test_context_snapshot_is_bounded_to_recent_events() -> None:
    parser = FakeParser()
    result = ParsePipeline(
        parser,
        max_context_events=2,
        max_context_chars=256,
    ).run(
        source_video="episode.mp4",
        duration_ms=30_000,
        chunk_duration_ms=10_000,
        context_overlap_ms=2_000,
        windows=_windows(),
    )

    final_context = result.chunks[-1].context_after
    assert len(final_context.recent_events) == 2
    assert len(final_context.compact_text) <= 256
    assert "第一段已确认动作" not in final_context.compact_text


def test_pipeline_resumes_after_the_last_completed_chunk() -> None:
    initial = ParsePipeline(FakeParser()).run(
        source_video="episode.mp4",
        duration_ms=30_000,
        chunk_duration_ms=10_000,
        context_overlap_ms=2_000,
        windows=_windows(),
    )
    resumed_parser = FakeParser()
    resumed = ParsePipeline(resumed_parser).run(
        source_video="episode.mp4",
        duration_ms=30_000,
        chunk_duration_ms=10_000,
        context_overlap_ms=2_000,
        windows=_windows(),
        completed_chunks=initial.chunks[:1],
    )

    assert resumed == initial
    assert len(resumed_parser.contexts) == 2
    assert "第一段已确认动作" in resumed_parser.contexts[0]


def test_pipeline_keeps_candidate_transcript_mapping_compatibility() -> None:
    windows = _windows()
    transcripts = {
        window.sequence_no: TranscriptAnalysis(transcript=[])
        for window in windows
    }

    result = ParsePipeline(FakeParser()).run(
        source_video="episode.mp4",
        duration_ms=30_000,
        chunk_duration_ms=10_000,
        context_overlap_ms=2_000,
        windows=windows,
        candidate_transcripts=transcripts,
    )

    assert len(result.chunks) == 3


def test_pipeline_passes_complete_background_and_images_to_every_window(tmp_path) -> None:
    images = (
        BackgroundImage(image_id="tomori_01", path=tmp_path / "tomori.png"),
        BackgroundImage(image_id="anon-02", path=tmp_path / "anon.png"),
    )
    background = (
        "# 完整系列背景\n\n"
        "![灯](asset://tomori_01)\n![爱音](asset://anon-02)\n"
        '{"customSchema": {"emotion": ["困惑", "平静"]}}'
    )
    parser = FakeParser()

    result = ParsePipeline(parser).run(
        source_video="episode.mp4",
        duration_ms=30_000,
        chunk_duration_ms=10_000,
        context_overlap_ms=2_000,
        windows=_windows(),
        series_bible=background,
        background_images=images,
    )

    assert len(result.chunks) == 3
    assert parser.background_images == [images, images, images]
    assert all(background in context for context in parser.contexts)


def test_resumed_pipeline_passes_images_only_to_unfinished_windows(tmp_path) -> None:
    images = (BackgroundImage(image_id="tomori_01", path=tmp_path / "tomori.png"),)
    background = "![灯](asset://tomori_01)"
    initial = ParsePipeline(FakeParser()).run(
        source_video="episode.mp4",
        duration_ms=30_000,
        chunk_duration_ms=10_000,
        context_overlap_ms=2_000,
        windows=_windows(),
        series_bible=background,
        background_images=images,
    )
    parser = FakeParser()

    resumed = ParsePipeline(parser).run(
        source_video="episode.mp4",
        duration_ms=30_000,
        chunk_duration_ms=10_000,
        context_overlap_ms=2_000,
        windows=_windows(),
        series_bible=background,
        background_images=images,
        completed_chunks=initial.chunks[:1],
    )

    assert resumed == initial
    assert parser.background_images == [images, images]
    assert all(background in context for context in parser.contexts)


def test_pipeline_marks_content_filter_result_as_fallback() -> None:
    checkpoints: list[VideoChunkParseResult] = []
    result = ParsePipeline(FakeFallbackParser()).run(
        source_video="episode.mp4",
        duration_ms=30_000,
        chunk_duration_ms=10_000,
        context_overlap_ms=2_000,
        windows=_windows(),
        on_chunk_complete=checkpoints.append,
    )

    assert all(chunk.is_fallback for chunk in result.chunks)
    assert all(chunk.is_fallback for chunk in checkpoints)


def test_fallback_marker_is_backward_compatible_and_round_trips() -> None:
    result = ParsePipeline(FakeFallbackParser()).run(
        source_video="episode.mp4",
        duration_ms=30_000,
        chunk_duration_ms=10_000,
        context_overlap_ms=2_000,
        windows=_windows(),
    )
    chunk = result.chunks[0]
    legacy_payload = chunk.model_dump()
    legacy_payload["needs_regeneration"] = legacy_payload.pop("is_fallback")

    restored_legacy = VideoChunkParseResult.model_validate(legacy_payload)
    assert restored_legacy.is_fallback is True

    missing_marker_payload = chunk.model_dump()
    missing_marker_payload.pop("is_fallback")
    restored_without_marker = VideoChunkParseResult.model_validate(
        missing_marker_payload
    )
    assert restored_without_marker.is_fallback is False

    checkpoint = VideoChunkCheckpoint(
        transcript_model="transcript-model",
        video_model="video-model",
        chunk=chunk,
    )
    restored_checkpoint = VideoChunkCheckpoint.model_validate_json(
        checkpoint.model_dump_json()
    )
    assert restored_checkpoint.chunk.is_fallback is True
    assert "needs_regeneration" not in restored_checkpoint.model_dump_json()


def test_video_parse_result_model_metadata_round_trips_and_remains_optional() -> None:
    legacy_result = ParsePipeline(FakeFallbackParser()).run(
        source_video="episode.mp4",
        duration_ms=30_000,
        chunk_duration_ms=10_000,
        context_overlap_ms=2_000,
        windows=_windows(),
    )
    payload = legacy_result.model_dump(mode="json")
    payload["models"] = {
        "transcript": None,
        "transcript_mode": "none",
        "video_parser": "doubao-pro",
        "fallback": {
            "enabled": True,
            "transcript_model": "doubao-lite",
            "provider": "deepseek",
            "visual_model": "deepseek-flash",
            "refiner_model": "deepseek-flash",
            "input_mode": "ordered_frames",
            "frame_fps": 0.5,
            "group_duration_ms": 30_000,
            "max_frames_per_group": 15,
            "max_height": 720,
        },
    }

    restored = VideoParseResult.model_validate(payload)
    round_tripped = VideoParseResult.model_validate_json(restored.model_dump_json())

    assert round_tripped.models is not None
    assert round_tripped.models.transcript is None
    assert round_tripped.models.transcript_mode == "none"
    assert round_tripped.models.video_parser == "doubao-pro"
    assert round_tripped.models.fallback.enabled is True
    assert round_tripped.models.fallback.transcript_model == "doubao-lite"
    assert round_tripped.models.fallback.provider == "deepseek"
    assert round_tripped.models.fallback.visual_model == "deepseek-flash"
    assert round_tripped.models.fallback.refiner_model == "deepseek-flash"
    assert round_tripped.models.fallback.input_mode == "ordered_frames"
    assert round_tripped.models.fallback.frame_fps == 0.5
    assert round_tripped.models.fallback.group_duration_ms == 30_000
    assert round_tripped.models.fallback.max_frames_per_group == 15
    assert round_tripped.models.fallback.max_height == 720

    restored_legacy = VideoParseResult.model_validate(
        legacy_result.model_dump(mode="json", exclude={"models"})
    )
    assert restored_legacy.models is None
