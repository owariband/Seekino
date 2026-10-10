from copy import deepcopy
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from gateway.config import GatewayConfig, load_config, required_api_key_env_names
from gateway.model_sources import ModelReference, ModelReferenceType

ROOT = Path(__file__).resolve().parents[2]
LEGACY = Path(__file__).parent / "fixtures/legacy-config.yaml"


def payload():
    return yaml.safe_load((ROOT / "config.yaml").read_text())


def test_default_changes_only_vision_controls_and_preserves_other_settings():
    old = load_config(LEGACY)
    new = load_config(ROOT / "config.yaml")
    old_data, new_data = old.model_dump(), new.model_dump()
    # Version/source links differ from the legacy fixture; optional tracing stays disabled.
    old_data["config_version"] = 2
    old_data["video_parser"]["transcription_source"] = "ark_default"
    old_data["video_parser"]["vision_source"] = "ark_default"
    assert old_data["video_parser"]["vision_max_output_tokens"] is None
    assert old_data["video_parser"]["vision_reasoning_effort"] is None
    old_data["video_parser"]["vision_max_output_tokens"] = 65536
    old_data["video_parser"]["vision_reasoning_effort"] = "none"
    assert not old_data["tracing"]["enabled"] and not new_data["tracing"]["enabled"]
    assert new_data == old_data
    assert required_api_key_env_names(new) == required_api_key_env_names(old)
    for stage in ("transcription", "vision"):
        assert new.stage_source(stage) == old.stage_source(stage)


def test_vision_controls_accept_larger_budget_and_preserve_explicit_reasoning():
    data = payload()
    data["models"]["vision"].update(max_output_tokens=131072, reasoning_effort="low")
    config = GatewayConfig.model_validate(data)
    assert config.video_parser.vision_max_output_tokens == 131072
    assert config.video_parser.vision_reasoning_effort == "low"
    assert GatewayConfig.model_validate(config.model_dump()) == config


def test_vision_reasoning_conflict_is_rejected_before_runtime():
    data = payload()
    data["video_parser"]["vision_reasoning_effort"] = "high"
    with pytest.raises(ValidationError, match="configure only"):
        GatewayConfig.model_validate(data)


def test_unsupported_vision_adapter_rejects_reasoning_controls():
    data = payload()
    data["model_sources"]["external_vision"] = {
        "adapter": "openai_vision", "provider": "vision_vendor",
        "base_url": "https://vision.example/v1", "api_key_env": "VISION_KEY",
    }
    data["models"]["vision"].update(source="external_vision", reasoning_effort="low")
    with pytest.raises(ValidationError, match="openai_vision does not support reasoning_effort"):
        GatewayConfig.model_validate(data)


def test_old_parsing_keeps_implicit_ark_credentials_independent_of_agent_sources():
    data = yaml.safe_load(LEGACY.read_text())
    data["model_sources"]["ark_default"]["api_key_env"] = "CUSTOM_AGENT_ARK"
    config = GatewayConfig.model_validate(data)
    for stage in ("transcription", "vision"):
        assert config.stage_source(stage).api_key_env == "ARK_API_KEY"
    assert "CUSTOM_AGENT_ARK" in required_api_key_env_names(config)
    assert "ARK_API_KEY" in required_api_key_env_names(config)


def test_independent_asr_and_vision_sources_and_only_selected_keys():
    data = payload()
    data["model_sources"].update(
        {
            "asr": {
                "adapter": "openai_audio",
                "provider": "speech_vendor",
                "base_url": "https://speech.example/v1",
                "api_key_env": "SPEECH_KEY",
            },
            "vision": {
                "adapter": "openai_vision",
                "provider": "vision_vendor",
                "base_url": "https://vision.example/v1",
                "api_key_env": "VISION_KEY",
            },
            "unused": {
                "adapter": "openai_audio",
                "provider": "unused",
                "base_url": "https://unused.example/v1",
                "api_key_env": "UNUSED_KEY",
            },
        }
    )
    data["models"]["transcription"] = {
        "source": "asr",
        "model": "speech",
        "request_timeout_seconds": 45,
    }
    data["models"]["vision"] = {"source": "vision", "model": "vision"}
    data["models"]["vision_fallback"] = {"source": "vision", "model": "other-vision"}
    original = deepcopy(data)
    config = GatewayConfig.model_validate(data)
    assert data == original  # Loading must not mutate YAML or caller-owned data.
    assert config.video_parser.transcription_timeout_seconds == 45
    assert config.stage_source("vision").provider == "vision_vendor"
    assert config.video_parser.fallback.deepseek.protocol == "openai_chat"
    assert required_api_key_env_names(config) == (
        "ARK_API_KEY",
        "DEEPSEEK_API_KEY",
        "SPEECH_KEY",
        "VISION_KEY",
    )
    assert GatewayConfig.model_validate(config.model_dump()) == config


@pytest.mark.parametrize("stage", ["transcription", "vision"])
def test_unknown_source_is_rejected_before_runtime(stage):
    data = payload()
    data["models"][stage]["source"] = "missing"
    with pytest.raises(ValidationError, match="not present"):
        GatewayConfig.model_validate(data)


@pytest.mark.parametrize("stage", ["transcription", "vision"])
def test_chat_protocol_cannot_be_used_for_unrelated_capabilities(stage):
    data = payload()
    data["models"][stage]["source"] = "deepseek_api"
    with pytest.raises(ValidationError, match="does not support adapter"):
        GatewayConfig.model_validate(data)


@pytest.mark.parametrize(
    "stage,section,key",
    [
        ("agent", "agent_model", None),
        ("transcription", "video_parser", "transcript_model"),
        ("vision", "video_parser", "video_model"),
        ("vision_fallback", "video_parser", "fallback"),
    ],
)
def test_conflicting_old_and_new_fields_are_never_silently_overridden(
    stage, section, key
):
    data = payload()
    if stage == "vision_fallback":
        data["video_parser"]["fallback"]["deepseek"] = {}
    elif key is None:
        data[section] = data["models"][stage]
    else:
        data[section][key] = "old-model"
    with pytest.raises(ValidationError, match="configure only"):
        GatewayConfig.model_validate(data)


def test_models_requires_version_and_rejects_typos():
    data = payload()
    del data["config_version"]
    with pytest.raises(ValidationError, match="config_version"):
        GatewayConfig.model_validate(data)
    data["config_version"] = 2
    data["models"]["transcripton"] = data["models"].pop("transcription")
    with pytest.raises(ValidationError, match="unknown models"):
        GatewayConfig.model_validate(data)


def test_existing_agent_chat_alias_still_loads():
    data = yaml.safe_load(LEGACY.read_text())
    data["agent_chat"] = data.pop("agent_model")
    assert (
        GatewayConfig.model_validate(data).agent_model
        == load_config(LEGACY).agent_model
    )


@pytest.mark.parametrize(
    "request_model,encoder_model,reference_type,model",
    [
        ("doubao-embedding-vision-250615", None, ModelReferenceType.MODEL,
         "doubao-embedding-vision-250615"),
        ("doubao-embedding-vision-250615", "doubao-embedding-vision-250615",
         ModelReferenceType.MODEL, "doubao-embedding-vision-250615"),
        ("ep-configured-a", "doubao-embedding-vision-250615",
         ModelReferenceType.ENDPOINT, "doubao-embedding-vision-250615"),
        ("ep-configured-b", "doubao-embedding-vision-251215",
         ModelReferenceType.ENDPOINT, "doubao-embedding-vision-251215"),
    ],
)
def test_model_reference_preserves_request_route_and_declared_encoder_identity(
    request_model, encoder_model, reference_type, model,
):
    reference = ModelReference.resolve(request_model, encoder_model=encoder_model)

    assert reference.type is reference_type
    assert ModelReferenceType.for_model(request_model) is reference_type
    assert reference.request_model == request_model
    assert reference.model == model


@pytest.mark.parametrize(
    "request_model,encoder_model",
    [
        ("ep-configured", None),
        ("ep-configured", "ep-another-endpoint"),
        ("doubao-embedding-vision-250615", "doubao-embedding-vision-251215"),
    ],
)
def test_model_reference_rejects_unproven_or_conflicting_encoder_identity(
    request_model, encoder_model,
):
    with pytest.raises(ValueError):
        ModelReference.resolve(request_model, encoder_model=encoder_model)


@pytest.mark.parametrize("request_model", ["", " ", "\t"])
def test_blank_model_reference_route_is_rejected(request_model):
    with pytest.raises(ValueError):
        ModelReferenceType.for_model(request_model)
    with pytest.raises(ValueError):
        ModelReference.resolve(request_model)


@pytest.mark.parametrize("encoder_model", ["", " ", "\t"])
def test_endpoint_model_reference_requires_nonblank_encoder_identity(encoder_model):
    with pytest.raises(ValueError):
        ModelReference.resolve("ep-configured", encoder_model=encoder_model)


@pytest.mark.parametrize(
    "request_model,encoder_model,default_encoder_model,model",
    [
        ("ep-legacy", None, "doubao-embedding-vision-250615",
         "doubao-embedding-vision-250615"),
        ("ep-configured", "doubao-embedding-vision-250615",
         "doubao-embedding-vision-251215", "doubao-embedding-vision-250615"),
        ("custom-direct-model", None, "doubao-embedding-vision-251215",
         "custom-direct-model"),
    ],
)
def test_model_reference_default_preserves_explicit_and_direct_identity(
    request_model, encoder_model, default_encoder_model, model,
):
    reference = ModelReference.resolve(
        request_model, encoder_model=encoder_model,
        default_encoder_model=default_encoder_model,
    )

    assert reference.type is ModelReferenceType.for_model(request_model)
    assert reference.request_model == request_model
    assert reference.model == model


def test_endpoint_model_reference_rejects_endpoint_as_default_identity():
    with pytest.raises(ValueError):
        ModelReference.resolve("ep-legacy", default_encoder_model="ep-another-endpoint")
