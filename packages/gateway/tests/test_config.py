from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from gateway.config import load_config


def _config_payload() -> dict[str, object]:
    return {
        "video_parser": {
            "transcript_model": "test-transcript",
            "video_model": "test-video",
            "transcript_fps": 1.0,
            "video_fps": 1.0,
            "fallback": {
                "enabled": True,
                "frame_fps": 0.5,
                "group_duration_ms": 30_000,
                "max_frames_per_group": 15,
                "max_height": 720,
                "deepseek": {
                    "provider": "deepseek",
                    "model": "test-deepseek",
                    "base_url": "https://api.deepseek.test",
                    "api_key_env": "TEST_DEEPSEEK_API_KEY",
                    "reasoning_effort": "none",
                    "max_output_tokens": 4096,
                    "request_timeout_seconds": 120,
                },
            },
        },
        "multimodal_embedding": {
            "model": "test-embedding",
            "dimensions": 1024,
            "fps": 1.0,
        },
    }


def _write_config(path: Path, payload: dict[str, object]) -> None:
    path.write_text(
        yaml.safe_dump(payload, sort_keys=False),
        encoding="utf-8",
    )


def test_default_config_uses_selected_models() -> None:
    config = load_config()

    assert config.video_parser.transcript_model == "doubao-seed-2-0-lite-260428"
    assert config.video_parser.video_model == "doubao-seed-2-1-pro-260628"
    assert config.multimodal_embedding.model == "doubao-embedding-vision-251215"
    assert config.multimodal_embedding.dimensions == 1024

    fallback = config.video_parser.fallback
    assert fallback.enabled is True
    assert fallback.frame_fps == 0.5
    assert fallback.group_duration_ms == 30_000
    assert fallback.max_frames_per_group == 15
    assert fallback.max_height == 720

    deepseek = fallback.deepseek
    assert deepseek.provider == "deepseek"
    assert deepseek.model == "deepseek-flash"
    assert deepseek.base_url == "https://api.deepseek.com"
    assert deepseek.api_key_env == "DEEPSEEK_API_KEY"
    assert deepseek.reasoning_effort == "none"
    assert deepseek.max_output_tokens == 8192
    assert deepseek.request_timeout_seconds == 1200


def test_config_rejects_unknown_fields(tmp_path: Path) -> None:
    payload = _config_payload()
    video_parser = payload["video_parser"]
    assert isinstance(video_parser, dict)
    video_parser["unknown"] = True
    config_path = tmp_path / "config.yaml"
    _write_config(config_path, payload)

    with pytest.raises(ValidationError):
        load_config(config_path)


def test_environment_can_select_config(monkeypatch, tmp_path: Path) -> None:
    payload = _config_payload()
    video_parser = payload["video_parser"]
    assert isinstance(video_parser, dict)
    video_parser["transcript_model"] = "custom-transcript"
    video_parser["video_model"] = "custom-video"
    fallback = video_parser["fallback"]
    assert isinstance(fallback, dict)
    fallback["group_duration_ms"] = 15_000
    deepseek = fallback["deepseek"]
    assert isinstance(deepseek, dict)
    deepseek["model"] = "custom-deepseek"
    deepseek["api_key_env"] = "CUSTOM_DEEPSEEK_API_KEY"
    embedding = payload["multimodal_embedding"]
    assert isinstance(embedding, dict)
    embedding["dimensions"] = 512

    config_path = tmp_path / "deepseek.yaml"
    _write_config(config_path, payload)
    monkeypatch.setenv("ANIVAG_MODEL_CONFIG", str(config_path))

    config = load_config()

    assert config.video_parser.transcript_model == "custom-transcript"
    assert config.video_parser.video_model == "custom-video"
    assert config.video_parser.fallback.group_duration_ms == 15_000
    assert config.video_parser.fallback.deepseek.model == "custom-deepseek"
    assert (
        config.video_parser.fallback.deepseek.api_key_env
        == "CUSTOM_DEEPSEEK_API_KEY"
    )
    assert config.multimodal_embedding.dimensions == 512


@pytest.mark.parametrize(
    ("section", "missing_field"),
    [
        ("fallback", "group_duration_ms"),
        ("fallback", "max_frames_per_group"),
        ("deepseek", "model"),
        ("deepseek", "base_url"),
        ("deepseek", "api_key_env"),
    ],
)
def test_fallback_requires_pipeline_and_model_fields(
    section: str,
    missing_field: str,
    tmp_path: Path,
) -> None:
    payload = _config_payload()
    video_parser = payload["video_parser"]
    assert isinstance(video_parser, dict)
    fallback = video_parser["fallback"]
    assert isinstance(fallback, dict)
    target = fallback if section == "fallback" else fallback["deepseek"]
    assert isinstance(target, dict)
    del target[missing_field]

    config_path = tmp_path / "missing-fallback-config.yaml"
    _write_config(config_path, payload)

    with pytest.raises(ValidationError):
        load_config(config_path)


def test_fallback_rejects_non_deepseek_provider(tmp_path: Path) -> None:
    payload = _config_payload()
    video_parser = payload["video_parser"]
    assert isinstance(video_parser, dict)
    fallback = video_parser["fallback"]
    assert isinstance(fallback, dict)
    deepseek = fallback["deepseek"]
    assert isinstance(deepseek, dict)
    deepseek["provider"] = "ark"

    config_path = tmp_path / "unknown-provider.yaml"
    _write_config(config_path, payload)

    with pytest.raises(ValidationError):
        load_config(config_path)
