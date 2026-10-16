from pathlib import Path
from types import SimpleNamespace

import pytest

from engine.common.model_factory import build_asr_agent
from engine.common.transcription import (
    AudioTranscriptionAgent,
    TranscriptAnalysis,
    VideoASRAgent,
    normalize_transcription,
)
from gateway.config import GatewayConfig, load_config
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
        {"start_seconds": 0, "end_seconds": 1200, "text": "first", "speaker_id": None},
        {"start_seconds": 1500, "end_seconds": 3000, "text": "second", "speaker_id": None},
    ]


def test_json_only_asr_retains_honest_window_level_timing():
    result = normalize_transcription({"text": "hello"}, duration_seconds=2000)
    assert result.transcript[0].end_seconds == 2000
    assert result.transcript[0].speaker_id is None
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
    monkeypatch.setattr("gateway.transcription.transcribe_audio", transcribe)
    agent = AudioTranscriptionAgent(
        "speech",
        provider="custom",
        base_url="https://speech.example/v1",
        api_key="fake-key",
        request_timeout_seconds=10,
    )
    assert agent.transcribe(video, duration_seconds=1000).transcript[0].text == "hello"
    assert audio_paths and not audio_paths[0].exists()
    agent.close()
    agent.close()
    with pytest.raises(RuntimeError, match="closed"):
        agent.transcribe(video, duration_seconds=1000)


@pytest.mark.parametrize("duration_seconds", [None, 3000])
def test_video_asr_converts_model_seconds_once_and_clamps_to_millisecond_duration(
    monkeypatch, tmp_path, duration_seconds
):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    payload = {"transcript": [
        {"start_seconds": 1.5, "end_seconds": 4, "text": "second", "speaker_id": "speaker1"},
        {"start_seconds": 1.2346, "end_seconds": 1.7, "text": "rounded", "speaker_id": "speaker2"},
        {"start_seconds": 0, "end_seconds": 1, "text": " first ", "speaker_id": "speaker1"},
        {"start_seconds": 0.5, "end_seconds": 0.5, "text": "zero width", "speaker_id": None},
        {"start_seconds": 1.0001, "end_seconds": 1.0002, "text": "rounds to zero width", "speaker_id": None},
        {"start_seconds": 3, "end_seconds": 4, "text": "outside", "speaker_id": None},
    ]}
    originals = []
    probed = []

    def probe(path):
        probed.append(path)
        return 3000

    def generate(inputs, *, schema_name, result_type):
        assert schema_name == "candidate_transcript"
        assert result_type is not TranscriptAnalysis
        schema = result_type.model_json_schema()
        cue_schema = schema["$defs"][schema["properties"]["transcript"]["items"]["$ref"].rsplit("/", 1)[1]]
        assert cue_schema["properties"]["start_seconds"]["type"] == "number"
        assert cue_schema["properties"]["end_seconds"]["type"] == "number"
        assert "speaker_id" in cue_schema["required"]
        assert {"type": "null"} in cue_schema["properties"]["speaker_id"]["anyOf"]
        assert inputs[0] == VideoInput(path=video, fps=0.5)
        assert isinstance(inputs[1], TextInput)
        assert "秒" in inputs[1].text and "小数" in inputs[1].text
        assert "整数毫秒" not in inputs[1].text
        assert "不要根据画面、字幕" in inputs[1].text
        assert "speaker1、speaker2" in inputs[1].text
        assert "说话人切换时必须拆成不同条目" in inputs[1].text
        assert "只区分匿名说话人，不匹配真实姓名" in inputs[1].text
        assert "不要识别说话者身份" not in inputs[1].text
        original = result_type.model_validate(payload)
        originals.append((original, original.model_dump()))
        return original

    monkeypatch.setattr("engine.common.transcription.probe_video_duration_seconds", probe)
    agent = VideoASRAgent(SimpleNamespace(model_name="lite", generate=generate), fps=0.5)
    result = agent.transcribe(video, duration_seconds=duration_seconds)

    assert isinstance(result, TranscriptAnalysis)
    assert [cue.model_dump() for cue in result.transcript] == [
        {"start_seconds": 0, "end_seconds": 1000, "text": "first", "speaker_id": "speaker1"},
        {"start_seconds": 1235, "end_seconds": 1700, "text": "rounded", "speaker_id": "speaker2"},
        {"start_seconds": 1500, "end_seconds": 3000, "text": "second", "speaker_id": "speaker1"},
    ]
    assert all(type(cue.start_seconds) is type(cue.end_seconds) is int for cue in result.transcript)
    original, snapshot = originals[0]
    assert original.model_dump() == snapshot
    assert original.transcript[0].end_seconds == 4
    assert len(original.transcript) == len(payload["transcript"]) == 6
    assert probed == ([video] if duration_seconds is None else [])


@pytest.mark.parametrize("transcript", [
    [],
    [{"start_seconds": 0.5, "end_seconds": 0.5, "text": "zero width"}],
    [{"start_seconds": 1.0001, "end_seconds": 1.0002, "text": "rounds to zero width"}],
    [{"start_seconds": 3, "end_seconds": 4, "text": "outside"}],
    [{"start_seconds": 0, "end_seconds": 1, "text": " "}],
])
def test_video_asr_returns_no_cues_for_empty_or_unusable_seconds(transcript, tmp_path):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")

    def generate(_inputs, *, schema_name, result_type):
        assert schema_name == "candidate_transcript"
        return result_type.model_validate({"transcript": [
            {**cue, "speaker_id": None} for cue in transcript
        ]})

    agent = VideoASRAgent(SimpleNamespace(model_name="lite", generate=generate), fps=1)
    assert agent.transcribe(video, duration_seconds=3000).transcript == []


@pytest.mark.parametrize("field", ["start_seconds", "end_seconds"])
@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), -float("inf"), -1, True, "1.5"])
def test_video_asr_rejects_invalid_model_seconds(field, invalid, tmp_path):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    cue = {"start_seconds": 1, "end_seconds": 2, "text": "hello", "speaker_id": None, field: invalid}

    def generate(_inputs, *, schema_name, result_type):
        assert schema_name == "candidate_transcript"
        return result_type.model_validate({"transcript": [cue]})

    agent = VideoASRAgent(SimpleNamespace(model_name="lite", generate=generate), fps=1)
    with pytest.raises(ValueError):
        agent.transcribe(video, duration_seconds=3000)


def test_video_asr_rejects_reversed_model_seconds(tmp_path):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")

    def generate(_inputs, *, schema_name, result_type):
        assert schema_name == "candidate_transcript"
        return result_type.model_validate({"transcript": [{
            "start_seconds": 2, "end_seconds": 1, "text": "reversed", "speaker_id": None,
        }]})

    agent = VideoASRAgent(SimpleNamespace(model_name="lite", generate=generate), fps=1)
    with pytest.raises(ValueError, match="invalid segment timestamps"):
        agent.transcribe(video, duration_seconds=3000)


def test_asr_factory_keeps_default_ark_source_independent_of_agent_source(monkeypatch):
    path = Path(__file__).resolve().parents[2] / "gateway/fixtures/legacy-config.yaml"
    data = load_config(path).model_dump()
    del data["normalized_models"]
    data["model_sources"]["ark_default"]["api_key_env"] = "CUSTOM_ARK_KEY"
    config = GatewayConfig.model_validate(data)
    monkeypatch.delenv("CUSTOM_ARK_KEY", raising=False)
    requests = []

    class Client:
        def __init__(self, config):
            self.config = config

        def create_transcription_model(self, binding):
            requests.append(binding)
            assert self.config.source(binding.source).api_key_env == "ARK_API_KEY"
            return SimpleNamespace(model_name=binding.model, input_mode="video")

    monkeypatch.setattr("engine.common.model_factory.GatewayClient", Client)

    agent = build_asr_agent(
        config, model="lite", fps=0.5, timeout=45,
    )
    assert isinstance(agent, VideoASRAgent)
    assert len(requests) == 1
    assert requests[0].model == "lite" and requests[0].request_timeout_seconds == 45
    assert requests[0].max_output_tokens is None
