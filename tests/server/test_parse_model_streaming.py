"""Model transport choices survive the persisted Parse configuration boundary."""

from pathlib import Path

import pytest

from agent.video_parse_v2.input import GenerativeModelConfig
from engine.common.config import VideoParseRuntimeConfig
from gateway.config import ModelBinding, load_config
from server.config.media import ImageUploadConfig
from server.config.parse_profile import ParseConfig
from server.config.upload import UploadServiceConfig


def _profile(setting, *, temperature=None, kimi=False):
    gateway = load_config(Path(__file__).parents[1] / "gateway/fixtures/legacy-config.yaml")
    if kimi:
        from gateway.config import OpenAIModelSourceConfig
        gateway.model_sources["fixed"] = OpenAIModelSourceConfig(
            provider="custom-name", profile="kimi", base_url="https://model.example/v1", api_key_env="OFFLINE_KEY",
        )
    primary = ModelBinding(
        source="fixed" if kimi else "ark_default", model="primary", supports_vision_input=True,
        max_output_tokens=4096, request_timeout_seconds=60,
        **({} if temperature is None else {"temperature": temperature}),
        **({} if setting is None else {"stream": setting}),
        fallback=ModelBinding(
            source="ark_default", model="backup", stream=setting is False, temperature=0.8,
            supports_vision_input=True, max_output_tokens=4096, request_timeout_seconds=90,
        ),
    )
    gateway = gateway.model_copy(update={"normalized_models": {
        **gateway.normalized_models,
        "vision": primary, "ocr": primary,
        "transcription": primary.model_copy(update={"source": "ark_default", "kind": "transcription", "fallback": None}),
    }})
    return ParseConfig.from_defaults(
        runtime=VideoParseRuntimeConfig(), gateway=gateway,
        upload=UploadServiceConfig(), images=ImageUploadConfig(),
        embedding_model=None, embedding_dimensions=gateway.resolve_embedding_binding().dimensions,
        embedding_fps=None,
    )


@pytest.mark.parametrize("temperature", [None, 0, 0.2, 0.8])
@pytest.mark.parametrize("by_alias", [False, True])
@pytest.mark.parametrize("kimi", [False, True])
def test_parse_temperature_is_frozen_and_restored_for_every_model(temperature, by_alias, kimi):
    profile = _profile(None, temperature=temperature, kimi=kimi)
    saved = profile.model_dump(mode="json", by_alias=by_alias)
    restored = ParseConfig.model_validate(saved)
    expected = 1 if kimi else temperature or 0
    for role in ("vision", "ocr"):
        assert saved[role]["temperature"] == expected
    assert saved["asr"]["temperature"] == (temperature or 0)
    task = restored.to_video_parse_v2()
    task = type(task).model_validate_json(task.model_dump_json())
    for role in ("asr", "understanding", "ocr"):
        model = getattr(task.models, role)
        role_expected = temperature or 0 if role == "asr" else expected
        assert model.temperature == role_expected
        binding = ModelBinding.model_validate(model.model_dump(exclude={"mode", "scope", "fps"}, exclude_none=True))
        assert binding.temperature == role_expected
        if role != "asr":
            assert binding.fallback.temperature == 0.8


@pytest.mark.parametrize("setting", [None, True, False], ids=["default", "enabled", "disabled"])
@pytest.mark.parametrize("by_alias", [False, True])
def test_parse_task_freezes_and_restores_each_model_stream_setting(setting, by_alias):
    profile = _profile(setting)
    saved = profile.model_dump(mode="json", by_alias=by_alias)
    restored = ParseConfig.model_validate(saved)
    expected = setting is not False
    for role in ("asr", "vision", "ocr"):
        assert saved[role]["stream"] is expected
    assert saved["fallback"]["stream"] is not expected
    task = restored.to_video_parse_v2()
    task = type(task).model_validate_json(task.model_dump_json())
    for role in ("asr", "understanding", "ocr"):
        model = getattr(task.models, role)
        assert model.stream is expected
        assert model.model_dump()["stream"] is expected
        binding = ModelBinding.model_validate(model.model_dump(exclude={"mode", "scope", "fps"}, exclude_none=True))
        assert binding.stream is expected
        if role != "asr":
            assert model.fallback.stream is not expected
            assert binding.fallback.stream is not expected


@pytest.mark.parametrize("by_alias", [False, True])
def test_historical_profiles_and_task_snapshots_keep_their_serialized_shape(by_alias):
    profile = _profile(False)

    def without_stream(value):
        if isinstance(value, dict):
            return {key: without_stream(item) for key, item in value.items() if key not in {"stream", "temperature"}}
        return value

    historical_profile = without_stream(profile.model_dump(mode="json", by_alias=by_alias))
    restored_profile = ParseConfig.model_validate(historical_profile)
    assert restored_profile.model_dump(mode="json", by_alias=by_alias) == historical_profile
    assert restored_profile.vision.stream is True
    assert restored_profile.fallback.stream is True
    assert restored_profile.vision.temperature == restored_profile.fallback.temperature == 0
    task = profile.to_video_parse_v2()
    historical_task = without_stream(task.model_dump(mode="json"))
    restored_task = type(task).model_validate(historical_task)
    assert restored_task.model_dump(mode="json") == historical_task
    assert restored_task.models.understanding.stream is True
    assert restored_task.models.understanding.fallback.stream is True
    assert restored_task.models.understanding.temperature == restored_task.models.understanding.fallback.temperature == 0


@pytest.mark.parametrize("role", ["understanding", "refine", "ocr", "prefix_context", "voiceprint"])
def test_direct_task_model_bindings_retain_explicit_non_streaming(role):
    task = _profile(None).to_video_parse_v2()
    binding = GenerativeModelConfig(
        source="ark_default", model="stage-model", request_timeout_seconds=30, stream=False, temperature=0.2,
    )
    task = task.model_copy(update={"models": task.models.model_copy(update={role: binding})})
    restored = type(task).model_validate_json(task.model_dump_json())
    assert getattr(restored.models, role).stream is False
    assert getattr(restored.models, role).temperature == 0.2
