from __future__ import annotations

from pathlib import Path

from engine.parse.analysis_windows import plan_analysis_windows
from engine.parse.checkpoints import write_json
from engine.parse.runtime import EngineVideoParseRuntime, VideoParseRuntimeConfig
from engine.parse.schemas import (
    ContextSnapshot,
    VideoChunkCheckpoint,
    VideoChunkParseResult,
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
