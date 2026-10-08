from pathlib import Path

import pytest

from engine.parsev1.audio_transcription import (
    AudioTranscriptionAgent,
    normalize_transcription,
)


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

    monkeypatch.setattr("engine.parsev1.audio_transcription.subprocess.run", extract)
    monkeypatch.setattr("engine.parsev1.audio_transcription.transcribe_audio", transcribe)
    agent = AudioTranscriptionAgent(
        "speech",
        provider="custom",
        base_url="https://speech.example/v1",
        api_key="fake-key",
        request_timeout_seconds=10,
    )
    assert agent.transcribe(video, duration_seconds=1000).transcript[0].text == "hello"
    assert audio_paths and not audio_paths[0].exists()
