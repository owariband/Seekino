from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from gateway.errors import ContentFilterError
from gateway.multimodal import ImageInput, TextInput, VideoInput

import engine.parsev1.video_chunk_parse as video_chunk_parse_module
from engine.parsev1.background import BackgroundImage
from engine.parsev1.video_chunk_parse import (
    ContentFilterVideoUnderstandingAgent,
    FrameFallbackVideoUnderstandingAgent,
    OrderedFrameVideoUnderstandingAgent,
    VideoASRAgent,
    VideoUnderstandingAgent,
)
from engine.parsev1.schemas import (
    NeutralVisualEvidence,
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


class SequencedVisionModel(FakeModel):
    def __init__(self) -> None:
        super().__init__("deepseek-flash", {}, provider_name="deepseek")

    def generate(
        self,
        inputs,
        *,
        schema_name: str,
        result_type,
    ):
        call_index = len(self.calls) + 1
        self.calls.append(
            {
                "inputs": list(inputs),
                "schema_name": schema_name,
                "result_type": result_type,
            }
        )
        return NeutralVisualEvidence.model_validate(
            {
                "observations": [
                    {
                        "evidence_frames": [1],
                        "subjects": [f"GROUP_{call_index}_SUBJECT"],
                        "visual_fact": f"GROUP_{call_index}_VISUAL_FACT",
                        "hard_subtitle": f"GROUP_{call_index}_HARD_SUBTITLE",
                        "visible_speaker": f"GROUP_{call_index}_SPEAKER",
                    }
                ]
            }
        )


def _transcript() -> TranscriptAnalysis:
    return TranscriptAnalysis(
        transcript=[
            TranscriptCue(
                start_seconds=0,
                end_seconds=2000,
                text="真是高高在上呢",
            )
        ]
    )


def _timeline() -> VideoTimeline:
    return VideoTimeline(
        summary="一名人物在房间内说话，随后面对另一人跪下。",
        events=[
            TimelineEvent(
                start_seconds=1000,
                end_seconds=9000,
                event_type="ACTION",
                description="一名人物面对另一人双膝跪地",
                text=None,
            ),
            TimelineEvent(
                start_seconds=0,
                end_seconds=2000,
                event_type="DIALOGUE",
                description="一名人物说出一句带有讽刺意味的话",
                text="真是高高在上呢",
            ),
        ],
    )


def _call_text(call: dict[str, Any]) -> str:
    return "\n".join(
        item.text for item in call["inputs"] if isinstance(item, TextInput)
    )


def _run_deepseek_fallback(
    tmp_path,
    monkeypatch,
    *,
    refiner_timeline: VideoTimeline | None = None,
    background_images=(),
    transcript: TranscriptAnalysis | None = None,
    asr_model_name: str | None = "test-lite",
):
    video_path = tmp_path / "chunk.mp4"
    video_path.write_bytes(b"fake-video")
    if transcript is None:
        transcript = TranscriptAnalysis(
            transcript=[
                TranscriptCue(
                    start_seconds=30000,
                    end_seconds=32000,
                    text="ASR_SECRET_LITE_TRANSCRIPT",
                )
            ]
        )
    vision_model = SequencedVisionModel()
    if refiner_timeline is None:
        refiner_timeline = VideoTimeline(
            summary="一名人物在室内说话。",
            events=[
                TimelineEvent(
                    start_seconds=30000,
                    end_seconds=32000,
                    event_type="DIALOGUE",
                    description="一名人物说话",
                    text="ASR_SECRET_LITE_TRANSCRIPT",
                )
            ],
        )
    refiner_model = FakeModel(
        "deepseek-flash",
        {"fallback_video_timeline": refiner_timeline},
        provider_name="deepseek",
    )

    def fake_extract_video_frames(
        video_path,
        timestamps_seconds,
        *,
        output_dir,
        max_height,
    ):
        del video_path
        assert max_height == 720
        timestamps = list(timestamps_seconds)
        directory = Path(output_dir)
        directory.mkdir(parents=True, exist_ok=True)
        frames = []
        for index, timestamp_seconds in enumerate(timestamps):
            frame_path = directory / f"frame-{index:03d}.jpg"
            frame_path.write_bytes(b"fake-frame")
            frames.append((timestamp_seconds, frame_path))
        return frames

    monkeypatch.setattr(
        video_chunk_parse_module,
        "extract_video_frames",
        fake_extract_video_frames,
    )
    context = (
        "SeriesBible:\nSERIES_BIBLE_SECRET\n\n"
        "contextBeforeCompact:\nCOMPACT_SECRET"
    )
    result = FrameFallbackVideoUnderstandingAgent(
        vision_model,
        refiner_model,
        asr_model_name=asr_model_name,
        frame_fps=0.5,
        group_duration_seconds=30,
        max_frames_per_group=15,
        max_height=720,
    ).understand(
        video_path,
        duration_seconds=66000,
        transcript=transcript,
        context=context,
        background_images=background_images,
    )
    return {
        "result": result,
        "vision_model": vision_model,
        "refiner_model": refiner_model,
    }


def test_asr_and_understanding_agents_exchange_an_explicit_transcript(tmp_path) -> None:
    transcript_model = FakeModel(
        "test-lite",
        {"candidate_transcript": _transcript()},
    )
    video_model = FakeModel("test-pro", {"video_timeline": _timeline()})
    video_path = tmp_path / "chunk.mp4"
    video_path.write_bytes(b"fake-video")

    transcript = VideoASRAgent(
        transcript_model,
        fps=0.5,
    ).transcribe(video_path, duration_seconds=8000)
    result = VideoUnderstandingAgent(
        video_model,
        fps=2.0,
    ).understand(
        video_path,
        transcript=transcript,
        context="角色穿红色外套",
        duration_seconds=8000,
    )

    assert result.duration_seconds == 8000
    assert result.candidate_transcript[0].text == "真是高高在上呢"
    assert [event.event_type for event in result.events] == ["DIALOGUE", "ACTION"]
    assert result.events[1].end_seconds == 8000
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
    assert "实际时长为 8000 毫秒" in instruction
    assert "角色穿红色外套" in instruction
    assert "不能替代当前视频证据" in instruction
    assert "脱离前后文单独正确理解" in instruction
    assert "开始学习吉他的年龄" in instruction
    assert "text 写 null" in instruction


def test_understanding_agent_sends_labeled_background_images_with_video(tmp_path) -> None:
    video_model = FakeModel("test-pro", {"video_timeline": _timeline()})
    video_path = tmp_path / "chunk.mp4"
    video_path.write_bytes(b"fake-video")
    images = [
        BackgroundImage("tomori_01", tmp_path / "tomori.png"),
        BackgroundImage("anon-02", tmp_path / "anon.png"),
    ]
    for image in images:
        image.path.write_bytes(b"background image")

    VideoUnderstandingAgent(
        video_model,
        fps=1.0,
    ).understand(
        video_path,
        duration_seconds=8000,
        transcript=_transcript(),
        context="人物背景正文与自定义字段",
        background_images=images,
    )

    inputs = video_model.calls[0]["inputs"]
    assert [type(item) for item in inputs] == [
        TextInput, ImageInput, TextInput, ImageInput, VideoInput, TextInput,
    ]
    assert "asset://tomori_01" in inputs[0].text
    assert "不是当前视频证据" in inputs[0].text
    assert inputs[1].path == images[0].path
    assert "asset://anon-02" in inputs[2].text
    assert inputs[3].path == images[1].path
    assert inputs[4].path == video_path
    assert "人物背景正文与自定义字段" in inputs[5].text


def test_understanding_agent_uses_the_explicit_transcript(tmp_path) -> None:
    video_model = FakeModel("test-pro", {"video_timeline": _timeline()})
    video_path = tmp_path / "chunk.mp4"
    video_path.write_bytes(b"fake-video")

    result = VideoUnderstandingAgent(
        video_model,
        fps=1.0,
    ).understand(
        video_path,
        duration_seconds=8000,
        transcript=_transcript(),
    )

    assert len(video_model.calls) == 1
    assert result.candidate_transcript == _transcript().transcript


def test_understanding_agent_supports_silent_local_direct_video(tmp_path) -> None:
    video_model = FakeModel(
        "qwen-local",
        {"video_timeline": _timeline()},
        provider_name="llama_cpp",
    )
    video_path = tmp_path / "chunk.mp4"
    video_path.write_bytes(b"fake-video")

    result = VideoUnderstandingAgent(
        video_model,
        fps=0.5,
        video_audio_available=False,
    ).understand(
        video_path,
        duration_seconds=8000,
        transcript=_transcript(),
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


def test_understanding_agent_preserves_ordered_frame_evidence(tmp_path) -> None:
    video_model = FakeModel("test-pro", {"video_timeline": _timeline()})
    parser = VideoUnderstandingAgent(
        video_model,
        fps=1.0,
    )
    first_frame = tmp_path / "frame-1.jpg"
    second_frame = tmp_path / "frame-2.jpg"
    first_frame.write_bytes(b"first-frame")
    second_frame.write_bytes(b"second-frame")

    result = parser.analyze_frames(
        [(1000, first_frame), (5000, second_frame)],
        duration_seconds=8000,
        transcript=TranscriptAnalysis(transcript=[]),
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
    assert "t=1000 milliseconds" in inputs[1].text
    assert inputs[2].path == first_frame
    assert "t=5000 milliseconds" in inputs[3].text
    assert inputs[4].path == second_frame
    assert "这些图片不包含音轨" in inputs[5].text
    assert "候选 ASR 为空时" in inputs[5].text
    assert "[floor(t), floor(t)+1)" in inputs[5].text
    assert "不能替代当前图片证据" in inputs[5].text
    assert result.candidate_transcript == []
    assert result.provenance is not None
    assert result.provenance.frame_timestamps_seconds == [1000, 5000]


def test_understanding_agent_rejects_non_increasing_frame_timestamps(tmp_path) -> None:
    frame = tmp_path / "frame.jpg"
    frame.write_bytes(b"frame")
    parser = VideoUnderstandingAgent(
        FakeModel("test-pro", {"video_timeline": _timeline()}),
        fps=1.0,
    )

    with pytest.raises(ValueError, match="strictly increasing"):
        parser.analyze_frames(
            [(1000, frame), (1000, frame)],
            duration_seconds=8000,
            transcript=TranscriptAnalysis(transcript=[]),
        )


def test_ordered_frame_understanding_agent_records_local_sampling(tmp_path) -> None:
    frame = tmp_path / "frame.jpg"
    frame.write_bytes(b"frame")
    model = FakeModel(
        "qwen-local",
        {"video_timeline": _timeline()},
        provider_name="ollama",
    )
    parser = OrderedFrameVideoUnderstandingAgent(
        model,
        frame_fps=0.5,
        max_frames=33,
        max_height=720,
    )

    result = parser.analyze_frames(
        [(1000, frame)],
        duration_seconds=8000,
        transcript=TranscriptAnalysis(transcript=[]),
    )

    assert isinstance(model.calls[0]["inputs"][1], TextInput)
    assert isinstance(model.calls[0]["inputs"][2], ImageInput)
    assert result.provenance is not None
    assert result.provenance.provider == "ollama"
    assert result.provenance.model == "qwen-local"
    assert result.provenance.input_mode == "ordered_frames"
    assert result.provenance.frame_fps == 0.5
    assert result.provenance.frame_timestamps_seconds == [1000]


def test_ordered_frames_preserve_integer_milliseconds_above_one_fps(tmp_path) -> None:
    first_frame = tmp_path / "frame-1.jpg"
    second_frame = tmp_path / "frame-2.jpg"
    first_frame.write_bytes(b"first")
    second_frame.write_bytes(b"second")
    model = FakeModel(
        "qwen-local",
        {"video_timeline": VideoTimeline(summary="片段完成", events=[])},
        provider_name="ollama",
    )
    parser = OrderedFrameVideoUnderstandingAgent(
        model,
        frame_fps=2.0,
        max_frames=2,
        max_height=720,
    )

    result = parser.analyze_frames(
        [(250, first_frame), (750, second_frame)],
        duration_seconds=1000,
        transcript=TranscriptAnalysis(transcript=[]),
    )

    assert "t=250 milliseconds" in model.calls[0]["inputs"][1].text
    assert "t=750 milliseconds" in model.calls[0]["inputs"][3].text
    assert result.provenance is not None
    assert result.provenance.frame_timestamps_seconds == [250, 750]


@pytest.mark.parametrize("mode", ["ark_frames", "ordered_frames", "ordered_video"])
def test_frame_paths_keep_background_images_separate_from_timed_frames(
    tmp_path,
    monkeypatch,
    mode,
) -> None:
    background = BackgroundImage("portrait_01", tmp_path / "portrait.png")
    background.path.write_bytes(b"background image")
    frame = tmp_path / "frame.jpg"
    frame.write_bytes(b"video frame")
    frames = [(1000, frame)]
    model = FakeModel("frame-model", {"video_timeline": _timeline()})
    if mode == "ark_frames":
        parser = VideoUnderstandingAgent(model, fps=1.0)
    else:
        parser = OrderedFrameVideoUnderstandingAgent(
            model, frame_fps=0.5, max_frames=33, max_height=720,
        )
    if mode == "ordered_video":
        video = tmp_path / "chunk.mp4"
        video.write_bytes(b"video")
        monkeypatch.setattr(
            video_chunk_parse_module, "extract_video_frames",
            lambda *_args, **_kwargs: frames,
        )
        result = parser.understand(
            video, duration_seconds=8000,
            transcript=TranscriptAnalysis(transcript=[]),
            background_images=[background],
        )
    else:
        result = parser.analyze_frames(
            frames, duration_seconds=8000,
            transcript=TranscriptAnalysis(transcript=[]),
            background_images=[background],
        )

    inputs = model.calls[0]["inputs"]
    assert "asset://portrait_01" in inputs[0].text
    assert inputs[1].path == background.path
    assert "视频采样帧" in inputs[2].text
    assert inputs[3].text == "Frame 1/1: t=1000 milliseconds"
    assert inputs[4].path == frame
    assert "不属于视频采样帧" in inputs[5].text
    assert result.provenance.frame_timestamps_seconds == [1000]


def test_deepseek_fallback_uses_background_images_only_in_refinement(
    tmp_path,
    monkeypatch,
) -> None:
    background = BackgroundImage("portrait_01", tmp_path / "portrait.png")
    background.path.write_bytes(b"background image")
    run = _run_deepseek_fallback(
        tmp_path, monkeypatch, background_images=[background],
    )

    for call in run["vision_model"].calls:
        assert "asset://portrait_01" not in _call_text(call)
        assert all(
            item.path != background.path
            for item in call["inputs"] if isinstance(item, ImageInput)
        )
    refiner_call = run["refiner_model"].calls[0]
    assert "asset://portrait_01" in refiner_call["inputs"][0].text
    assert isinstance(refiner_call["inputs"][1], ImageInput)
    assert refiner_call["inputs"][1].path == background.path
    refiner_prompt = _call_text(refiner_call)
    assert "看不到当前片段的原始视频帧" in refiner_prompt
    assert "背景图中的动作或文字不能作为当前事件或台词证据" in refiner_prompt
    assert "只能来自 visual_observations" in refiner_prompt


def test_background_images_cannot_supply_fallback_dialogue_text(
    tmp_path,
    monkeypatch,
) -> None:
    timeline = VideoTimeline(
        summary="背景图中的台词",
        events=[TimelineEvent(
            start_seconds=1000, end_seconds=2000, event_type="DIALOGUE",
            description="人物说话", text="来自背景图而不是视频的台词",
        )],
    )
    with pytest.raises(ValueError, match="not grounded in ASR or subtitles"):
        _run_deepseek_fallback(
            tmp_path, monkeypatch, refiner_timeline=timeline,
            background_images=[BackgroundImage("portrait_01", tmp_path / "portrait.png")],
        )


def test_deepseek_fallback_groups_frames_and_isolates_visual_stage(
    tmp_path,
    monkeypatch,
) -> None:
    run = _run_deepseek_fallback(tmp_path, monkeypatch)
    vision_model = run["vision_model"]

    assert [
        sum(isinstance(item, ImageInput) for item in call["inputs"])
        for call in vision_model.calls
    ] == [15, 15, 3]
    assert [call["schema_name"] for call in vision_model.calls] == [
        "neutral_visual_evidence",
        "neutral_visual_evidence",
        "neutral_visual_evidence",
    ]
    for call in vision_model.calls:
        visual_prompt = _call_text(call)
        assert "SERIES_BIBLE_SECRET" not in visual_prompt
        assert "COMPACT_SECRET" not in visual_prompt
        assert "ASR_SECRET_LITE_TRANSCRIPT" not in visual_prompt


def test_deepseek_fallback_refines_explicit_asr_and_mapped_evidence(
    tmp_path,
    monkeypatch,
) -> None:
    run = _run_deepseek_fallback(tmp_path, monkeypatch)
    result = run["result"]
    refiner_model = run["refiner_model"]

    assert len(refiner_model.calls) == 1
    assert refiner_model.calls[0]["schema_name"] == "fallback_video_timeline"
    refiner_prompt = _call_text(refiner_model.calls[0])
    assert "ASR_SECRET_LITE_TRANSCRIPT" in refiner_prompt
    assert "SERIES_BIBLE_SECRET" in refiner_prompt
    assert "COMPACT_SECRET" in refiner_prompt
    assert "GROUP_1_VISUAL_FACT" in refiner_prompt
    assert "GROUP_2_VISUAL_FACT" in refiner_prompt
    assert "GROUP_3_VISUAL_FACT" in refiner_prompt
    assert "evidence_timestamps_seconds" in refiner_prompt
    assert "support_intervals_seconds" in refiner_prompt
    assert "31000" in refiner_prompt

    assert [cue.text for cue in result.candidate_transcript] == [
        "ASR_SECRET_LITE_TRANSCRIPT"
    ]
    assert result.provenance is not None
    assert result.provenance.provider == "deepseek"
    assert result.provenance.model == "deepseek-flash"
    assert result.provenance.input_mode == "ordered_frames"
    assert result.provenance.frame_fps == 0.5
    assert result.provenance.frame_timestamps_seconds == list(range(1000, 66000, 2000))
    assert result.provenance.fallback_transcript_model == "test-lite"
    assert result.provenance.visual_model == "deepseek-flash"
    assert result.provenance.refiner_model == "deepseek-flash"
    assert result.provenance.group_frame_counts == [15, 15, 3]


def test_deepseek_fallback_keeps_empty_transcript_and_records_no_asr_model(
    tmp_path,
    monkeypatch,
) -> None:
    run = _run_deepseek_fallback(
        tmp_path,
        monkeypatch,
        transcript=TranscriptAnalysis(transcript=[]),
        asr_model_name=None,
        refiner_timeline=VideoTimeline(summary="无对白片段", events=[]),
    )

    result = run["result"]
    assert result.candidate_transcript == []
    assert result.provenance is not None
    assert result.provenance.fallback_transcript_model is None


def test_deepseek_fallback_rejects_unsupported_dialogue_text(
    tmp_path,
    monkeypatch,
) -> None:
    unsupported_timeline = VideoTimeline(
        summary="一名人物说出证据中不存在的话。",
        events=[
            TimelineEvent(
                start_seconds=30000,
                end_seconds=32000,
                event_type="DIALOGUE",
                description="一名人物说话",
                text="MODEL_HALLUCINATED_DIALOGUE",
            )
        ],
    )

    with pytest.raises(ValueError):
        _run_deepseek_fallback(
            tmp_path,
            monkeypatch,
            refiner_timeline=unsupported_timeline,
        )


def test_deepseek_fallback_rejects_event_outside_chunk_duration(
    tmp_path,
    monkeypatch,
) -> None:
    out_of_bounds_timeline = VideoTimeline(
        summary="片段末尾的动作越过了真实时长。",
        events=[
            TimelineEvent(
                start_seconds=65000,
                end_seconds=70000,
                event_type="ACTION",
                description="一名人物移动",
                text=None,
            )
        ],
    )

    with pytest.raises(ValueError):
        _run_deepseek_fallback(
            tmp_path,
            monkeypatch,
            refiner_timeline=out_of_bounds_timeline,
        )


class RaisingParser:
    def __init__(self, error: Exception) -> None:
        self.error = error
        self.calls = 0
        self.requests = []

    def understand(self, *args, **kwargs):
        self.calls += 1
        self.requests.append(kwargs)
        raise self.error


class ReturningParser:
    def __init__(self, evidence: VideoEvidence) -> None:
        self.evidence = evidence
        self.calls = 0
        self.requests = []

    def understand(self, *args, **kwargs) -> VideoEvidence:
        self.calls += 1
        self.requests.append(kwargs)
        return self.evidence


def test_content_filter_falls_back_once_and_preserves_diagnostics(tmp_path) -> None:
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
            duration_seconds=8000,
            candidate_transcript=[],
            summary="本地结果",
            events=[],
            provenance=VideoParseProvenance(
                provider="ollama",
                model="qwen-local",
                input_mode="ordered_frames",
                frame_fps=0.5,
                frame_timestamps_seconds=[1000, 3000],
            ),
        )
    )

    images = [BackgroundImage("portrait_01", tmp_path / "portrait.png")]
    result = ContentFilterVideoUnderstandingAgent(
        primary,
        fallback,
        primary_model="test-pro",
    ).understand(
        "chunk.mp4",
        transcript=TranscriptAnalysis(transcript=[]),
        duration_seconds=8000,
        background_images=images,
    )

    assert primary.calls == 1
    assert fallback.calls == 1
    assert primary.requests[0]["background_images"] == images
    assert fallback.requests[0]["background_images"] == images
    assert result.provenance is not None
    assert result.provenance.fallback_reason == "content_filter"
    assert result.provenance.primary_model == "test-pro"
    assert result.provenance.primary_error_code == primary_error.code
    assert result.provenance.primary_filter_type == "output"
    assert result.provenance.primary_filter_details == "policy"
    assert result.provenance.primary_response_id == "response-1"
    assert result.provenance.primary_partial_output_chars == 4094


def test_non_content_filter_error_does_not_fall_back() -> None:
    primary = RaisingParser(RuntimeError("network failed"))
    fallback = ReturningParser(
        VideoEvidence(
            duration_seconds=8000,
            candidate_transcript=[],
            summary="unused",
            events=[],
        )
    )

    parser = ContentFilterVideoUnderstandingAgent(
        primary,
        fallback,
        primary_model="test-pro",
    )
    with pytest.raises(RuntimeError, match="network failed"):
        parser.understand(
            "chunk.mp4",
            transcript=TranscriptAnalysis(transcript=[]),
            duration_seconds=8000,
        )

    assert primary.calls == 1
    assert fallback.calls == 0
