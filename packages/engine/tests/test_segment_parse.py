from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from gateway import ContentFilterError, ImageInput, TextInput, VideoInput

from anivag_engine.segment_parse import (
    ContentFilterFallbackSegmentParser,
    OrderedFrameSegmentParser,
    SegmentParser,
)
from anivag_engine.schemas import (
    TimelineEvent,
    TranscriptAnalysis,
    TranscriptCue,
    VideoEvidence,
    VideoParseProvenance,
    VideoTimeline,
)


class FakeModel:
    def __init__(
        self,
        model_name: str,
        results: dict[str, Any],
        *,
        provider_name: str = "ark",
    ) -> None:
        self.provider_name = provider_name
        self.model_name = model_name
        self.results = results
        self.calls: list[dict[str, Any]] = []

    def generate(
        self,
        inputs,
        *,
        schema_name: str,
        result_type,
    ):
        self.calls.append(
            {
                "inputs": list(inputs),
                "schema_name": schema_name,
                "result_type": result_type,
            }
        )
        return self.results[schema_name]


def _transcript() -> TranscriptAnalysis:
    return TranscriptAnalysis(
        transcript=[
            TranscriptCue(
                start_ms=400,
                end_ms=1100,
                text="真是高高在上呢",
            )
        ]
    )


def _timeline() -> VideoTimeline:
    return VideoTimeline(
        summary="一名人物在房间内说话，随后面对另一人跪下。",
        events=[
            TimelineEvent(
                start_ms=1200,
                end_ms=8400,
                event_type="ACTION",
                description="一名人物面对另一人双膝跪地",
                text=None,
            ),
            TimelineEvent(
                start_ms=400,
                end_ms=1100,
                event_type="DIALOGUE",
                description="一名人物说出一句带有讽刺意味的话",
                text="真是高高在上呢",
            ),
        ],
    )


def test_segment_parser_fuses_candidate_transcript_with_video(tmp_path) -> None:
    transcript_model = FakeModel(
        "test-lite",
        {"candidate_transcript": _transcript()},
    )
    video_model = FakeModel("test-pro", {"video_timeline": _timeline()})
    video_path = tmp_path / "segment.mp4"
    video_path.write_bytes(b"fake-video")

    result = SegmentParser(
        transcript_model,
        video_model,
        transcript_fps=0.5,
        video_fps=2.0,
    ).analyze(
        video_path,
        context="角色穿红色外套",
        duration_ms=8000,
    )

    assert result.duration_ms == 8000
    assert result.candidate_transcript[0].text == "真是高高在上呢"
    assert [event.event_type for event in result.events] == ["DIALOGUE", "ACTION"]
    assert result.events[1].end_ms == 8000
    assert result.provenance == VideoParseProvenance(
        provider="doubao",
        model="test-pro",
        input_mode="video",
    )

    transcript_inputs = transcript_model.calls[0]["inputs"]
    assert transcript_model.calls[0]["schema_name"] == "candidate_transcript"
    assert isinstance(transcript_inputs[0], VideoInput)
    assert transcript_inputs[0].fps == 0.5
    assert isinstance(transcript_inputs[1], TextInput)
    assert "不要识别说话者身份" in transcript_inputs[1].text

    video_inputs = video_model.calls[0]["inputs"]
    assert video_model.calls[0]["schema_name"] == "video_timeline"
    assert isinstance(video_inputs[0], VideoInput)
    assert video_inputs[0].fps == 2.0
    instruction = video_inputs[1].text
    assert "候选 ASR 与自己从视频画面" in instruction
    assert "真是高高在上呢" in instruction
    assert "实际时长为 8000 ms" in instruction
    assert "角色穿红色外套" in instruction
    assert "不能替代当前视频证据" in instruction
    assert "脱离前后文单独正确理解" in instruction
    assert "开始学习吉他的年龄" in instruction
    assert "text 写 null" in instruction


def test_segment_parser_reuses_precomputed_candidate_transcript(tmp_path) -> None:
    transcript_model = FakeModel("test-lite", {})
    video_model = FakeModel("test-pro", {"video_timeline": _timeline()})
    video_path = tmp_path / "segment.mp4"
    video_path.write_bytes(b"fake-video")

    result = SegmentParser(
        transcript_model,
        video_model,
        transcript_fps=1.0,
        video_fps=1.0,
    ).analyze(
        video_path,
        duration_ms=8000,
        candidate_transcript=_transcript(),
    )

    assert transcript_model.calls == []
    assert len(video_model.calls) == 1
    assert result.candidate_transcript == _transcript().transcript


def test_segment_parser_supports_silent_local_direct_video(tmp_path) -> None:
    video_model = FakeModel(
        "qwen-local",
        {"video_timeline": _timeline()},
        provider_name="llama_cpp",
    )
    video_path = tmp_path / "segment.mp4"
    video_path.write_bytes(b"fake-video")

    result = SegmentParser(
        FakeModel("unused-transcript", {}),
        video_model,
        transcript_fps=1.0,
        video_fps=0.5,
        video_audio_available=False,
    ).analyze(
        video_path,
        duration_ms=8000,
        candidate_transcript=_transcript(),
    )

    inputs = video_model.calls[0]["inputs"]
    assert isinstance(inputs[0], VideoInput)
    assert inputs[0].fps == 0.5
    assert "不能读取原视频音轨" in inputs[1].text
    assert "视频画面或硬字幕证据" in inputs[1].text
    assert result.provenance == VideoParseProvenance(
        provider="llama_cpp",
        model="qwen-local",
        input_mode="video",
        frame_fps=0.5,
    )


def test_segment_parser_preserves_ordered_frame_evidence(tmp_path) -> None:
    video_model = FakeModel("test-pro", {"video_timeline": _timeline()})
    parser = SegmentParser(
        FakeModel("test-lite", {}),
        video_model,
        transcript_fps=1.0,
        video_fps=1.0,
    )
    first_frame = tmp_path / "frame-1.jpg"
    second_frame = tmp_path / "frame-2.jpg"
    first_frame.write_bytes(b"first-frame")
    second_frame.write_bytes(b"second-frame")

    result = parser.analyze_frames(
        [(1000, first_frame), (5000, second_frame)],
        duration_ms=8000,
        candidate_transcript=TranscriptAnalysis(transcript=[]),
        context="此前成员正在准备演出",
    )

    inputs = video_model.calls[0]["inputs"]
    assert [type(item) for item in inputs] == [
        TextInput,
        TextInput,
        ImageInput,
        TextInput,
        ImageInput,
        TextInput,
    ]
    assert "t=1000 ms" in inputs[1].text
    assert inputs[2].path == first_frame
    assert "t=5000 ms" in inputs[3].text
    assert inputs[4].path == second_frame
    assert "这些图片不包含音轨" in inputs[5].text
    assert "候选 ASR 为空时" in inputs[5].text
    assert "[t, t+1ms)" in inputs[5].text
    assert "不能替代当前图片证据" in inputs[5].text
    assert result.candidate_transcript == []
    assert result.provenance is not None
    assert result.provenance.frame_timestamps_ms == [1000, 5000]


def test_segment_parser_rejects_non_increasing_frame_timestamps(tmp_path) -> None:
    frame = tmp_path / "frame.jpg"
    frame.write_bytes(b"frame")
    parser = SegmentParser(
        FakeModel("test-lite", {}),
        FakeModel("test-pro", {"video_timeline": _timeline()}),
        transcript_fps=1.0,
        video_fps=1.0,
    )

    with pytest.raises(ValueError, match="strictly increasing"):
        parser.analyze_frames(
            [(1000, frame), (1000, frame)],
            duration_ms=8000,
            candidate_transcript=TranscriptAnalysis(transcript=[]),
        )


def test_ordered_frame_segment_parser_records_local_sampling(tmp_path) -> None:
    frame = tmp_path / "frame.jpg"
    frame.write_bytes(b"frame")
    model = FakeModel(
        "qwen-local",
        {"video_timeline": _timeline()},
        provider_name="ollama",
    )
    parser = OrderedFrameSegmentParser(
        model,
        frame_fps=0.5,
        max_frames=33,
        max_height=720,
    )

    result = parser.analyze_frames(
        [(1000, frame)],
        duration_ms=8000,
        candidate_transcript=TranscriptAnalysis(transcript=[]),
    )

    assert isinstance(model.calls[0]["inputs"][1], TextInput)
    assert isinstance(model.calls[0]["inputs"][2], ImageInput)
    assert result.provenance is not None
    assert result.provenance.provider == "ollama"
    assert result.provenance.model == "qwen-local"
    assert result.provenance.input_mode == "ordered_frames"
    assert result.provenance.frame_fps == 0.5
    assert result.provenance.frame_timestamps_ms == [1000]


class RaisingParser:
    def __init__(self, error: Exception) -> None:
        self.error = error
        self.calls = 0

    def analyze(self, *args, **kwargs):
        self.calls += 1
        raise self.error


class ReturningParser:
    def __init__(self, evidence: VideoEvidence) -> None:
        self.evidence = evidence
        self.calls = 0

    def analyze(self, *args, **kwargs) -> VideoEvidence:
        self.calls += 1
        return self.evidence


def test_content_filter_falls_back_once_and_preserves_diagnostics() -> None:
    primary_error = ContentFilterError(
        "filtered",
        code="OutputTextSensitiveContentDetected",
        response_id="response-1",
        partial_output_chars=4094,
        content_filter_type="output",
        content_filter_details="policy",
    )
    primary = RaisingParser(primary_error)
    fallback = ReturningParser(
        VideoEvidence(
            duration_ms=8000,
            candidate_transcript=[],
            summary="本地结果",
            events=[],
            provenance=VideoParseProvenance(
                provider="ollama",
                model="qwen-local",
                input_mode="ordered_frames",
                frame_fps=0.5,
                frame_timestamps_ms=[1000, 3000],
            ),
        )
    )

    result = ContentFilterFallbackSegmentParser(
        primary,
        fallback,
        primary_model="test-pro",
    ).analyze("segment.mp4", duration_ms=8000)

    assert primary.calls == 1
    assert fallback.calls == 1
    assert result.provenance is not None
    assert result.provenance.fallback_reason == "content_filter"
    assert result.provenance.primary_model == "test-pro"
    assert result.provenance.primary_error_code == primary_error.code
    assert result.provenance.primary_response_id == "response-1"
    assert result.provenance.primary_partial_output_chars == 4094


def test_non_content_filter_error_does_not_fall_back() -> None:
    primary = RaisingParser(RuntimeError("network failed"))
    fallback = ReturningParser(
        VideoEvidence(
            duration_ms=8000,
            candidate_transcript=[],
            summary="unused",
            events=[],
        )
    )

    parser = ContentFilterFallbackSegmentParser(
        primary,
        fallback,
        primary_model="test-pro",
    )
    with pytest.raises(RuntimeError, match="network failed"):
        parser.analyze("segment.mp4", duration_ms=8000)

    assert primary.calls == 1
    assert fallback.calls == 0
