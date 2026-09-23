from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest

from gateway.multimodal import ImageInput, TextInput, VideoInput

from engine.parse.analysis_windows import plan_analysis_windows
from engine.parse.background import BackgroundImage
from engine.parse.checkpoints import write_json
from engine.parse.runtime import EngineVideoParseRuntime, VideoParseRuntimeConfig
from engine.parse.video_chunk_parse import OrderedFrameVideoUnderstandingAgent
from engine.parse.schemas import (
    ContextSnapshot,
    TranscriptAnalysis,
    VideoChunkCheckpoint,
    VideoChunkParseResult,
    VideoParseProvenance,
    VideoTimeline,
)


def test_runtime_single_chunk_contract_is_lazy_and_reuses_model_clients(
    tmp_path,
    monkeypatch,
) -> None:
    source = tmp_path / "episode.mp4"
    source.write_bytes(b"video fixture")
    window = plan_analysis_windows(
        source,
        duration_ms=1000,
        output_dir=tmp_path / "windows",
        chunk_duration_ms=1000,
        context_overlap_ms=0,
    )[0]
    window.media_path.parent.mkdir(parents=True)
    window.media_path.write_bytes(b"analysis fixture")
    constructed: list[str] = []

    class Model:
        provider_name = "ark"

        def __init__(self, model, **_kwargs):
            self.model_name = model
            constructed.append(model)

        def generate(self, _inputs, *, schema_name, result_type):
            if schema_name == "candidate_transcript":
                assert result_type is TranscriptAnalysis
                return TranscriptAnalysis(transcript=[])
            assert schema_name == "video_timeline"
            assert result_type is VideoTimeline
            return VideoTimeline(summary="片段完成", events=[])

    monkeypatch.setattr("engine.parse.runtime.ArkMultimodalModel", Model)
    runtime = EngineVideoParseRuntime(
        config=VideoParseRuntimeConfig(
            fallback_enabled=False,
            transcript_model="test-lite",
            video_model="test-pro",
        )
    )

    models = runtime.describe_models(asr_enabled=False)

    assert models.transcript is None
    assert models.fallback.transcript_model == "test-lite"
    assert constructed == []
    asr_agent = runtime.server_asr_agent
    understanding_agent = runtime.server_understanding_agent
    assert asr_agent is not understanding_agent
    assert asr_agent._runtime is runtime
    assert understanding_agent._runtime is runtime
    assert not hasattr(asr_agent, "understand_chunk")
    assert not hasattr(asr_agent, "describe_models")
    assert not hasattr(understanding_agent, "transcribe_chunk")
    assert not hasattr(understanding_agent, "describe_models")

    with ThreadPoolExecutor(max_workers=4) as executor:
        transcripts = list(executor.map(asr_agent.transcribe_chunk, [window] * 4))

    assert constructed == ["test-lite"]

    compact = ContextSnapshot(
        snapshot_id="context-initial",
        after_chunk_sequence_no=-1,
        compact_text="",
        recent_events=[],
    )
    parsed = understanding_agent.understand_chunk(
        compact,
        window,
        transcript=transcripts[0],
    )

    assert all(item == TranscriptAnalysis(transcript=[]) for item in transcripts)
    assert constructed == ["test-lite", "test-pro"]
    assert parsed.window == window
    assert parsed.window_summary == "片段完成"


def test_runtime_uses_ordered_frames_when_a_real_frame_cap_is_selected(monkeypatch) -> None:
    constructed = []

    class Model:
        provider_name = "ark"

        def __init__(self, model, **kwargs):
            self.model_name = model
            constructed.append(kwargs)

    monkeypatch.setattr("engine.parse.runtime.ArkMultimodalModel", Model)
    runtime = EngineVideoParseRuntime(config=VideoParseRuntimeConfig(
        fallback_enabled=False,
        video_model="test-vision",
        video_fps=0.5,
        video_input_mode="ordered_frames",
        video_max_frames_per_window=18,
        video_max_output_tokens=4096,
        max_height=480,
    ))

    agent = runtime._get_understanding_agent()
    assert isinstance(agent, OrderedFrameVideoUnderstandingAgent)
    assert agent.frame_fps == 0.5
    assert agent.max_frames == 18
    assert agent.max_height == 480
    assert constructed[0]["max_output_tokens"] == 4096


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
            video_parse=VideoParseProvenance(
                provider="deepseek",
                model="deepseek-flash",
                input_mode="ordered_frames",
                frame_fps=0.5,
                group_duration_ms=30_000,
                max_frames_per_group=15,
                fallback_transcript_model=None,
                visual_model="deepseek-flash",
                refiner_model="deepseek-flash",
                fallback_reason="content_filter",
            ),
            is_fallback=True,
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


def test_runtime_pipelines_candidate_asr_ahead_of_serial_parse(
    tmp_path,
    monkeypatch,
) -> None:
    source = tmp_path / "episode.mp4"
    source.write_bytes(b"video fixture")
    asr_started = [Event() for _ in range(3)]
    pro_started = Event()
    pro_sequences: list[int] = []

    class Model:
        provider_name = "ark"

        def __init__(self, model, **_kwargs):
            self.model_name = model

        def generate(self, inputs, *, schema_name, result_type):
            video = next(item for item in inputs if isinstance(item, VideoInput))
            sequence_no = int(Path(video.path).name.split("-", 2)[1].split(".")[0])
            if schema_name == "candidate_transcript":
                assert result_type is TranscriptAnalysis
                asr_started[sequence_no].set()
                if sequence_no == 1:
                    assert pro_started.wait(timeout=5)
                return TranscriptAnalysis(transcript=[])

            assert schema_name == "video_timeline"
            assert result_type is VideoTimeline
            pro_sequences.append(sequence_no)
            if sequence_no == 0:
                pro_started.set()
                assert asr_started[1].wait(timeout=5)
            return VideoTimeline(summary=f"chunk {sequence_no}", events=[])

    def materialize(windows, **_kwargs):
        for window in windows:
            window.media_path.parent.mkdir(parents=True, exist_ok=True)
            window.media_path.write_bytes(b"analysis fixture")

    monkeypatch.setattr("engine.parse.runtime.ArkMultimodalModel", Model)
    monkeypatch.setattr(
        "engine.parse.runtime.materialize_analysis_windows",
        materialize,
    )
    output = tmp_path / "job"

    result = EngineVideoParseRuntime(
        config=VideoParseRuntimeConfig(
            chunk_duration_ms=1000,
            context_overlap_ms=0,
            asr_concurrency=1,
            fallback_enabled=False,
            transcript_model="test-lite",
            video_model="test-pro",
        )
    ).run(
        source,
        duration_ms=3000,
        output_dir=output,
    )

    assert len(result.chunks) == 3
    assert pro_sequences == [0, 1, 2]
    assert all(event.is_set() for event in asr_started)
    assert sorted(path.name for path in (output / "transcripts").iterdir()) == [
        "chunk-0000.json",
        "chunk-0001.json",
        "chunk-0002.json",
    ]


def test_runtime_resumes_each_stage_after_later_asr_failure(
    tmp_path,
    monkeypatch,
) -> None:
    source = tmp_path / "episode.mp4"
    source.write_bytes(b"video fixture")
    calls: list[tuple[str, int]] = []
    fail_second_asr = True

    class Model:
        provider_name = "ark"

        def __init__(self, model, **_kwargs):
            self.model_name = model

        def generate(self, inputs, *, schema_name, result_type):
            video = next(item for item in inputs if isinstance(item, VideoInput))
            sequence_no = int(Path(video.path).name.split("-", 2)[1].split(".")[0])
            calls.append((schema_name, sequence_no))
            if schema_name == "candidate_transcript":
                if sequence_no == 1 and fail_second_asr:
                    raise RuntimeError("transient ASR failure")
                return TranscriptAnalysis(transcript=[])
            return VideoTimeline(summary=f"chunk {sequence_no}", events=[])

    def materialize(windows, **_kwargs):
        for window in windows:
            window.media_path.parent.mkdir(parents=True, exist_ok=True)
            window.media_path.write_bytes(b"analysis fixture")

    monkeypatch.setattr("engine.parse.runtime.ArkMultimodalModel", Model)
    monkeypatch.setattr(
        "engine.parse.runtime.materialize_analysis_windows",
        materialize,
    )
    output = tmp_path / "job"
    runtime = EngineVideoParseRuntime(
        config=VideoParseRuntimeConfig(
            chunk_duration_ms=1000,
            context_overlap_ms=0,
            asr_concurrency=1,
            fallback_enabled=False,
            transcript_model="test-lite",
            video_model="test-pro",
        )
    )

    with pytest.raises(RuntimeError, match="candidate ASR failed for chunk 1"):
        runtime.run(source, duration_ms=2000, output_dir=output)

    assert (output / "transcripts" / "chunk-0000.json").is_file()
    assert not (output / "transcripts" / "chunk-0001.json").exists()
    assert (output / "chunks" / "chunk-0000.json").is_file()
    assert not (output / "chunks" / "chunk-0001.json").exists()
    assert calls.count(("candidate_transcript", 0)) == 1
    assert calls.count(("video_timeline", 0)) == 1
    assert calls.count(("candidate_transcript", 1)) == 1
    assert calls.count(("video_timeline", 1)) == 0

    fail_second_asr = False
    result = runtime.run(source, duration_ms=2000, output_dir=output)

    assert len(result.chunks) == 2
    assert calls.count(("candidate_transcript", 0)) == 1
    assert calls.count(("video_timeline", 0)) == 1
    assert calls.count(("candidate_transcript", 1)) == 2
    assert calls.count(("video_timeline", 1)) == 1
