from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from gateway.config import load_config


def test_default_config_uses_doubao_models() -> None:
    config = load_config()

    assert (
        config.video_parser.transcript_model
        == "doubao-seed-2-0-lite-260428"
    )
    assert (
        config.video_parser.video_model
        == "doubao-seed-2-1-pro-260628"
    )
    assert (
        config.multimodal_embedding.model
        == "doubao-embedding-vision-251215"
    )
    assert config.multimodal_embedding.dimensions == 1024
    assert config.video_parser.local_fallback.enabled is True
    assert config.video_parser.local_fallback.provider == "ollama"
    assert (
        config.video_parser.local_fallback.model
        == "anivag-qwen3-vl:8b-q4-64k"
    )
    assert (
        config.video_parser.local_fallback.base_url
        == "http://127.0.0.1:11434"
    )
    assert config.video_parser.deepseek_vision is not None
    assert config.video_parser.deepseek_vision.provider == "deepseek"
    assert config.video_parser.deepseek_vision.model == "deepseek-flash"
    assert (
        config.video_parser.deepseek_vision.base_url
        == "https://api.deepseek.com"
    )
    assert (
        config.video_parser.deepseek_vision.api_key_env
        == "DEEPSEEK_API_KEY"
    )
    assert config.video_parser.deepseek_vision.reasoning_effort == "none"
    assert config.video_parser.fallback_refiner.provider == "ark"
    assert (
        config.video_parser.fallback_refiner.model
        == "deepseek-v4-flash-ga-260731"
    )
    assert (
        config.video_parser.fallback_refiner.base_url
        == "https://ark.cn-beijing.volces.com/api/v3"
    )
    assert config.video_parser.fallback_refiner.api_key_env == "ARK_API_KEY"


def test_config_rejects_unknown_fields(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
video_parser:
  transcript_model: test-transcript
  video_model: test-video
  transcript_fps: 1.0
  video_fps: 1.0
  unknown: true
  local_fallback:
    enabled: false
    model: test-local
    base_url: http://ollama.test
    frame_fps: 0.5
    max_frames: 8
    max_height: 720
    max_output_tokens: 1024
    request_timeout_seconds: 30
  fallback_refiner:
    provider: ark
    model: test-refiner
    base_url: https://ark.test/api/v3
    api_key_env: TEST_ARK_API_KEY
    max_output_tokens: 1024
    request_timeout_seconds: 30
multimodal_embedding:
  model: test-embedding
  dimensions: 1024
  fps: 1.0
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ValidationError):
        load_config(config_path)


def test_environment_can_select_config(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "doubao.yaml"
    config_path.write_text(
        """
video_parser:
  transcript_model: custom-transcript
  video_model: custom-video
  transcript_fps: 2.0
  video_fps: 0.5
  local_fallback:
    enabled: true
    model: custom-local
    base_url: http://ollama.test
    frame_fps: 0.5
    max_frames: 12
    max_height: 480
    max_output_tokens: 2048
    request_timeout_seconds: 60
  fallback_refiner:
    provider: ark
    model: custom-refiner
    base_url: https://ark.test/api/v3
    api_key_env: CUSTOM_ARK_API_KEY
    max_output_tokens: 4096
    request_timeout_seconds: 120
multimodal_embedding:
  model: custom-embedding
  dimensions: 512
  fps: 0.5
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("ANIVAG_MODEL_CONFIG", str(config_path))

    config = load_config()

    assert config.video_parser.transcript_model == "custom-transcript"
    assert config.video_parser.video_model == "custom-video"
    assert config.video_parser.local_fallback.model == "custom-local"
    assert config.video_parser.local_fallback.base_url == "http://ollama.test"
    assert config.video_parser.fallback_refiner.model == "custom-refiner"
    assert (
        config.video_parser.fallback_refiner.api_key_env
        == "CUSTOM_ARK_API_KEY"
    )
    assert config.multimodal_embedding.dimensions == 512


@pytest.mark.parametrize("missing_field", ["model", "base_url"])
def test_local_fallback_requires_model_and_base_url(
    missing_field: str,
    tmp_path: Path,
) -> None:
    payload = {
        "video_parser": {
            "transcript_model": "test-transcript",
            "video_model": "test-video",
            "transcript_fps": 1.0,
            "video_fps": 1.0,
            "local_fallback": {
                "enabled": True,
                "model": "test-local",
                "base_url": "http://ollama.test",
                "frame_fps": 0.5,
                "max_frames": 8,
                "max_height": 720,
                "max_output_tokens": 1024,
                "request_timeout_seconds": 30,
            },
            "fallback_refiner": {
                "provider": "ark",
                "model": "test-refiner",
                "base_url": "https://ark.test/api/v3",
                "api_key_env": "TEST_ARK_API_KEY",
                "max_output_tokens": 1024,
                "request_timeout_seconds": 30,
            },
        },
        "multimodal_embedding": {
            "model": "test-embedding",
            "dimensions": 1024,
            "fps": 1.0,
        },
    }
    del payload["video_parser"]["local_fallback"][missing_field]
    config_path = tmp_path / "missing-local-config.yaml"
    config_path.write_text(
        yaml.safe_dump(payload, sort_keys=False),
        encoding="utf-8",
    )

    with pytest.raises(ValidationError):
        load_config(config_path)


def test_local_fallback_rejects_unknown_provider(tmp_path: Path) -> None:
    payload = {
        "video_parser": {
            "transcript_model": "test-transcript",
            "video_model": "test-video",
            "transcript_fps": 1.0,
            "video_fps": 1.0,
            "local_fallback": {
                "enabled": True,
                "provider": "unknown",
                "model": "test-local",
                "base_url": "http://local.test",
                "frame_fps": 0.5,
                "max_frames": 8,
                "max_height": 720,
                "max_output_tokens": 1024,
                "request_timeout_seconds": 30,
            },
            "fallback_refiner": {
                "provider": "ark",
                "model": "test-refiner",
                "base_url": "https://ark.test/api/v3",
                "api_key_env": "TEST_ARK_API_KEY",
                "max_output_tokens": 1024,
                "request_timeout_seconds": 30,
            },
        },
        "multimodal_embedding": {
            "model": "test-embedding",
            "dimensions": 1024,
            "fps": 1.0,
        },
    }
    config_path = tmp_path / "unknown-provider.yaml"
    config_path.write_text(
        yaml.safe_dump(payload, sort_keys=False),
        encoding="utf-8",
    )

    with pytest.raises(ValidationError):
        load_config(config_path)
