from pathlib import Path
from types import SimpleNamespace

import pytest

from engine.common.model_factory import build_asr_agent
from engine.common.transcription import (
    AudioTranscriptionAgent,
    TranscriptAnalysis,
    TranscriptCue,
    VideoASRAgent,
    normalize_transcription,
)
from gateway.config import ArkModelSourceConfig, load_config
from gateway.multimodal import TextInput, VideoInput


def test_asr_segments_are_sorted_clamped_and_converted_to_integer_milliseconds():
    result = normalize_transcription(
        {
            "segments": [
                {"start": 1.5, "end": 4, "text": "second"},
                {"start": -0.1, "end": 1.2, "text": " first "},
                {"start": 5, "end": 6, "text": "outside"},
            ]
        },
        duration_seconds=3000,
    )
    assert [cue.model_dump() for cue in result.transcript] == [
        {"start_seconds": 0, "end_seconds": 1200, "text": "first"},
        {"start_seconds": 1500, "end_seconds": 3000, "text": "second"},
    ]


def test_json_only_asr_retains_honest_window_level_timing():
    result = normalize_transcription({"text": "hello"}, duration_seconds=2000)
    assert result.transcript[0].end_seconds == 2000
    assert normalize_transcription({"text": " "}, duration_seconds=2000).transcript == []
    assert (
        normalize_transcription(
            {"text": "hello", "segments": []}, duration_seconds=2000
        ).transcript
        == result.transcript
    )


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"segments": [{"start": 3, "end": 2, "text": "bad"}]},
        {"segments": [{"start": float("nan"), "end": 2, "text": "bad"}]},
    ],
)
def test_asr_rejects_malformed_responses(payload):
    with pytest.raises(ValueError):
        normalize_transcription(payload, duration_seconds=2000)


def test_asr_extracts_audio_and_cleans_temporary_files(monkeypatch, tmp_path):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    audio_paths = []

    def extract(command, **kwargs):
        assert command[0] == "ffmpeg" and "-vn" in command
        assert str(video) in command
        audio = Path(command[-1])
        audio.write_bytes(b"audio")
        audio_paths.append(audio)

    def transcribe(path, **kwargs):
        assert path.read_bytes() == b"audio"
        assert kwargs["model"] == "speech"
        return {"text": "hello"}

    monkeypatch.setattr("engine.common.transcription.subprocess.run", extract)
    monkeypatch.setattr("engine.common.transcription.transcribe_audio", transcribe)
    agent = AudioTranscriptionAgent(
        "speech",
        provider="custom",
        base_url="https://speech.example/v1",
        api_key="fake-key",
        request_timeout_seconds=10,
    )
    assert agent.transcribe(video, duration_seconds=1000).transcript[0].text == "hello"
    assert audio_paths and not audio_paths[0].exists()


@pytest.mark.parametrize("duration_seconds", [None, 3000])
def test_video_asr_preserves_cues_and_clamps_using_millisecond_duration(
    monkeypatch, tmp_path, duration_seconds
):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    original = TranscriptAnalysis(transcript=[
        TranscriptCue(start_seconds=1500, end_seconds=4000, text="second"),
        TranscriptCue(start_seconds=500, end_seconds=500, text="short"),
        TranscriptCue(start_seconds=3000, end_seconds=4000, text="outside"),
    ])
    probed = []

    def probe(path):
        probed.append(path)
        return 3000

    def generate(inputs, *, schema_name, result_type):
        assert schema_name == "candidate_transcript"
        assert result_type is TranscriptAnalysis
        assert inputs[0] == VideoInput(path=video, fps=0.5)
        assert isinstance(inputs[1], TextInput)
        assert "整数毫秒" in inputs[1].text
        assert "不要根据画面、字幕" in inputs[1].text
        return original

    monkeypatch.setattr("engine.common.transcription.probe_video_duration_seconds", probe)
    agent = VideoASRAgent(SimpleNamespace(model_name="lite", generate=generate), fps=0.5)
    result = agent.transcribe(video, duration_seconds=duration_seconds)

    assert [cue.model_dump() for cue in result.transcript] == [
        {"start_seconds": 1500, "end_seconds": 3000, "text": "second"},
        {"start_seconds": 500, "end_seconds": 500, "text": "short"},
    ]
    assert original.transcript[0].end_seconds == 4000
    assert len(original.transcript) == 3
    assert probed == ([video] if duration_seconds is None else [])


def test_asr_factory_keeps_default_ark_source_independent_of_agent_source(monkeypatch):
    config = load_config()
    config = config.model_copy(update={
        "video_parser": config.video_parser.model_copy(update={"transcription_source": None}),
        "model_sources": {
            **config.model_sources,
            "ark_default": ArkModelSourceConfig(api_key_env="CUSTOM_ARK_KEY"),
        },
    })
    monkeypatch.delenv("CUSTOM_ARK_KEY", raising=False)
    requests = []

    def build(model, **kwargs):
        requests.append((model, kwargs))
        return SimpleNamespace(model_name=model)

    agent = build_asr_agent(
        config, model="lite", fps=0.5, timeout=45, ark_model_type=build,
    )
    assert isinstance(agent, VideoASRAgent)
    assert requests == [("lite", {
        "request_timeout_seconds": 45,
        "max_output_tokens": None,
    })]
