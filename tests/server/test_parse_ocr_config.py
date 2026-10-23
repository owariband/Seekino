"""Dedicated OCR must survive server defaults and the persisted task boundary."""

import pytest

from engine.common.config import VideoParseRuntimeConfig
from gateway.config import ModelBinding, load_config
from server.config.media import ImageUploadConfig
from server.config.parse_profile import ParseConfig
from server.config.upload import UploadServiceConfig


def _profile(gateway):
    return ParseConfig.from_defaults(
        runtime=VideoParseRuntimeConfig(), gateway=gateway,
        upload=UploadServiceConfig(), images=ImageUploadConfig(),
        embedding_model=None,
        embedding_dimensions=gateway.resolve_embedding_binding().dimensions,
        embedding_fps=None,
    )


@pytest.mark.parametrize("with_fallback", [False, True])
def test_ocr_medium_binding_is_independent_and_frozen_with_task(with_fallback):
    gateway = load_config()
    fallback = ModelBinding(
        kind="generative", source="codex_local", model="gpt-6-luna",
        reasoning_effort="medium", supports_vision_input=True,
        request_timeout_seconds=93, max_output_tokens=4096,
    ) if with_fallback else None
    binding = ModelBinding(
        source="codex_local", model="gpt-6-luna", reasoning_effort="medium",
        supports_vision_input=True, request_timeout_seconds=91, max_output_tokens=4096,
        fallback=fallback,
    )
    gateway = gateway.model_copy(update={
        "normalized_models": {**gateway.normalized_models, "ocr": binding},
    })
    profile = _profile(gateway)
    task = profile.to_video_parse_v2()
    restored = type(task).model_validate(task.model_dump(mode="json"))
    assert restored.ocr_enabled is True
    assert restored.models.ocr.source == binding.source
    assert restored.models.ocr.model == binding.model
    assert restored.models.ocr.reasoning_effort == "medium"
    assert restored.models.ocr.request_timeout_seconds == 91
    assert restored.models.ocr.max_output_tokens == 4096
    if with_fallback:
        assert restored.models.ocr.fallback.model == fallback.model
        assert restored.models.ocr.fallback.reasoning_effort == "medium"
        assert restored.models.ocr.fallback.request_timeout_seconds == 93
    else:
        assert restored.models.ocr.fallback is None
    assert restored.models.understanding.model == profile.vision.model
    assert restored.models.understanding.reasoning_effort == profile.vision.reasoning_effort
    assert restored.models.asr.model == profile.asr.model


def test_absent_ocr_preserves_old_profile_and_task_serialization():
    gateway = load_config()
    gateway = gateway.model_copy(update={
        "normalized_models": {key: value for key, value in gateway.normalized_models.items() if key != "ocr"},
    })
    profile = _profile(gateway)
    for by_alias in (False, True):
        payload = profile.model_dump(mode="json", by_alias=by_alias)
        assert "ocr" not in payload
        restored = ParseConfig.model_validate(payload)
        assert restored.model_dump(mode="json", by_alias=by_alias) == payload
        task = restored.to_video_parse_v2()
        assert task.ocr_enabled is False
        assert task.models.ocr is None


@pytest.mark.parametrize("invalid, message", [
    ({"supports_vision_input": False}, "OCR model must declare image input support"),
    ({"supports_vision_input": True, "fallback": {
        "source": "codex_local", "model": "gpt-6-luna", "supports_vision_input": True,
        "fallback": {"source": "codex_local", "model": "gpt-6-luna", "supports_vision_input": True},
    }}, "OCR supports one configured fallback"),
])
def test_invalid_ocr_binding_is_rejected_before_task_creation(invalid, message):
    gateway = load_config()
    gateway = gateway.model_copy(update={
        "normalized_models": {**gateway.normalized_models, "ocr": ModelBinding(
            source="codex_local", model="gpt-6-luna", **invalid,
        )},
    })
    with pytest.raises(ValueError, match=message):
        _profile(gateway)
