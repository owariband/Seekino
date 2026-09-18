from __future__ import annotations

import json

from engine.parse.schemas import (
    AnalysisWindow,
    ContextSnapshot,
    ObservedTimelineEvent,
    TimelineEvent,
    TranscriptCue,
    VideoChunkCheckpoint,
    VideoChunkParseResult,
    VideoParseProvenance,
)
from training.qwen3_vl.dataset import (
    build_swift_record,
    select_training_series_context,
)
from training.qwen3_vl.validate_dataset import validate_dataset


def _checkpoint() -> VideoChunkCheckpoint:
    event = TimelineEvent(
        start_ms=1200,
        end_ms=3400,
        event_type="ACTION",
        description="人物拿起吉他",
        text=None,
    )
    context_before = ContextSnapshot(
        snapshot_id="context-before",
        after_chunk_sequence_no=-1,
        compact_text="",
        recent_events=[],
    )
    context_after = ContextSnapshot(
        snapshot_id="context-after",
        after_chunk_sequence_no=0,
        compact_text="人物拿起吉他",
        recent_events=[],
    )
    chunk = VideoChunkParseResult(
        window=AnalysisWindow(
            sequence_no=0,
            source_video="episode.mp4",
            media_path="window.mp4",
            chunk_start_ms=0,
            chunk_end_ms=60_000,
            analysis_start_ms=0,
            analysis_end_ms=63_000,
            analysis_duration_ms=63_000,
            relative_chunk_start_ms=0,
            relative_chunk_end_ms=60_000,
        ),
        context_before=context_before,
        candidate_transcript=[
            TranscriptCue(start_ms=900, end_ms=1600, text="开始吧")
        ],
        video_parse=VideoParseProvenance(
            provider="doubao",
            model="teacher",
            input_mode="video",
        ),
        window_summary="人物准备演奏。",
        observed_events=[
            ObservedTimelineEvent(
                relative_event=event,
                absolute_start_ms=1200,
                absolute_end_ms=3400,
                owned_by_current_chunk=True,
                kept_after_deduplication=True,
            )
        ],
        events=[],
        self_compact="人物拿起吉他",
        context_after=context_after,
        context_before_compact="",
        context_after_compact="人物拿起吉他",
    )
    return VideoChunkCheckpoint(
        transcript_model="lite-asr",
        video_model="teacher",
        chunk=chunk,
    )


def test_build_swift_record_reconstructs_direct_video_input_and_raw_timeline():
    record = build_swift_record(
        _checkpoint(),
        video_reference="videos/job/window-0000.mp4",
        series_bible="角色使用吉他。",
    )

    assert record["videos"] == ["videos/job/window-0000.mp4"]
    assert record["messages"][0]["content"].startswith("<video>\n")
    assert "不能读取原视频音轨" in record["messages"][0]["content"]
    assert "角色使用吉他" in record["messages"][0]["content"]
    target = json.loads(record["messages"][1]["content"])
    assert target == {
        "summary": "人物准备演奏。",
        "events": [
            {
                "start_ms": 1200,
                "end_ms": 3400,
                "event_type": "ACTION",
                "description": "人物拿起吉他",
                "text": None,
            }
        ],
    }


def test_select_training_series_context_uses_bounded_runtime_section():
    context = select_training_series_context(
        "# Full document\n"
        "<!-- ANIVAG_RUNTIME_CONTEXT_START -->\n"
        "角色紧凑表\n"
        "<!-- ANIVAG_RUNTIME_CONTEXT_END -->\n"
        "大量训练时不需要的来源资料"
    )

    assert context == "角色紧凑表"


def test_validate_dataset_rejects_episode_leakage(tmp_path):
    checkpoint = _checkpoint()
    for split in ("train", "validation"):
        video = tmp_path / "videos" / split / "window.mp4"
        video.parent.mkdir(parents=True, exist_ok=True)
        video.write_bytes(b"not-empty")
        record = build_swift_record(
            checkpoint,
            video_reference=f"videos/{split}/window.mp4",
            series_bible=None,
        )
        (tmp_path / f"{split}.jsonl").write_text(
            json.dumps(record, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    manifest = []
    for split in ("train", "validation"):
        manifest.append(
            {
                "sample_id": f"{split}:0000",
                "split": split,
                "video_id": "same-episode",
                "video": f"videos/{split}/window.mp4",
                "analysis_duration_ms": 63_000,
                "teacher_provider": "doubao",
                "teacher_input_mode": "video",
                "is_fallback": False,
            }
        )
    (tmp_path / "manifest.jsonl").write_text(
        "".join(json.dumps(item) + "\n" for item in manifest),
        encoding="utf-8",
    )

    report = validate_dataset(tmp_path)

    assert not report.valid
    assert any("episode leakage" in error for error in report.errors)
