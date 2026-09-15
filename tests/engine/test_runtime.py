from __future__ import annotations

from pathlib import Path

from gateway.models import ImageInput, TextInput, VideoInput

from engine.parse.analysis_windows import plan_analysis_windows
from engine.parse.background import BackgroundImage
from engine.parse.checkpoints import write_json
from engine.parse.runtime import EngineVideoParseRuntime, VideoParseRuntimeConfig
from engine.parse.schemas import (
    ContextSnapshot,
    VideoChunkCheckpoint,
    VideoChunkParseResult,
    VideoTimeline,
)


def test_runtime_resumes_a_fully_parsed_video_without_model_credentials(
    tmp_path,
    monkeypatch,
) -> None:
    source = tmp_path / "episode.mp4"
    source.write_bytes(b"video fixture")
    output = tmp_path / "job"
    windows = plan_analysis_windows(
        source,
        duration_ms=10_000,
        output_dir=output / "windows",
        chunk_duration_ms=60_000,
        context_overlap_ms=0,
    )
    window = windows[0]
    window.media_path.parent.mkdir(parents=True)
    window.media_path.write_bytes(b"analysis fixture")
    before = ContextSnapshot(
        snapshot_id="context-initial",
        after_chunk_sequence_no=-1,
        compact_text="",
        recent_events=[],
    )
    after = ContextSnapshot(
        snapshot_id="context-0000",
        after_chunk_sequence_no=0,
        compact_text="片段完成",
        recent_events=[],
    )
    checkpoint = VideoChunkCheckpoint(
        transcript_model="doubao-seed-2-0-lite-260428",
        transcript_source="none",
        video_model="doubao-seed-2-1-pro-260628",
        chunk=VideoChunkParseResult(
            window=window,
            context_before=before,
            candidate_transcript=[],
            is_fallback=False,
            window_summary="片段完成",
            observed_events=[],
            events=[],
            self_compact="片段完成",
            context_after=after,
        ),
    )
    write_json(checkpoint, output / "chunks" / "chunk-0000.json")

    def materialize(current_windows, **_kwargs):
        assert current_windows[0].media_path == window.media_path

    monkeypatch.setattr(
        "engine.parse.runtime.materialize_analysis_windows",
        materialize,
    )
    monkeypatch.setattr(
        "engine.parse.runtime.ArkMultimodalModel",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("a completed resume must not construct model clients")
        ),
    )
    monkeypatch.delenv("ARK_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    result = EngineVideoParseRuntime(
        config=VideoParseRuntimeConfig(
            chunk_duration_ms=60_000,
            context_overlap_ms=0,
            asr_mode="none",
            fallback_enabled=True,
        )
    ).run(
        source,
        duration_ms=10_000,
        output_dir=output,
    )

    assert len(result.chunks) == 1
    assert result.chunks[0].self_compact == "片段完成"
    assert (output / "pipeline.parse.json").is_file()


def test_runtime_passes_background_images_to_each_video_model_call(
    tmp_path,
    monkeypatch,
) -> None:
    source = tmp_path / "episode.mp4"
    source.write_bytes(b"video fixture")
    portrait = tmp_path / "portrait.png"
    portrait.write_bytes(b"portrait fixture")
    images = [BackgroundImage("tomori_01", portrait)]
    calls = []

    class Model:
        provider_name = "ark"

        def __init__(self, model, **_kwargs):
            self.model_name = model

        def generate(self, inputs, *, schema_name, result_type):
            assert self.model_name == "test-pro"
            assert schema_name == "video_timeline"
            assert result_type is VideoTimeline
            calls.append(inputs)
            return VideoTimeline(summary="片段完成", events=[])

    def materialize(windows, **_kwargs):
        for window in windows:
            window.media_path.parent.mkdir(parents=True, exist_ok=True)
            window.media_path.write_bytes(b"analysis fixture")

    monkeypatch.setattr("engine.parse.runtime.ArkMultimodalModel", Model)
    monkeypatch.setattr(
        "engine.parse.runtime.materialize_analysis_windows", materialize
    )
    result = EngineVideoParseRuntime(
        config=VideoParseRuntimeConfig(
            chunk_duration_ms=1000,
            context_overlap_ms=0,
            asr_mode="none",
            fallback_enabled=False,
            transcript_model="test-lite",
            video_model="test-pro",
        )
    ).run(
        source,
        duration_ms=2000,
        output_dir=tmp_path / "job",
        series_bible="完整背景正文 asset://tomori_01",
        background_images=images,
    )

    assert len(result.chunks) == len(calls) == 2
    for inputs in calls:
        assert isinstance(inputs[0], TextInput)
        assert "asset://tomori_01" in inputs[0].text
        assert isinstance(inputs[1], ImageInput)
        assert inputs[1].path == portrait
        assert isinstance(inputs[2], VideoInput)
        assert "完整背景正文 asset://tomori_01" in inputs[3].text
