from copy import deepcopy
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from gateway.config import (
    EmbeddingModelBinding, GatewayConfig, ModelBinding, load_config,
    required_api_key_env_names, resolve_source_api_key,
)
from gateway.model_sources import ModelReference, ModelReferenceType

ROOT = Path(__file__).resolve().parents[2]
LEGACY = Path(__file__).parent / "fixtures/legacy-config.yaml"


def payload():
    return yaml.safe_load((ROOT / "config.yaml").read_text())


def test_default_model_migration_preserves_asr_embedding_agent_and_parse_strategy():
    old = load_config(LEGACY)
    new = load_config(ROOT / "config.yaml")
    assert new.agent_model == old.agent_model
    assert new.multimodal_embedding == old.multimodal_embedding
    assert new.agent_runtime == old.agent_runtime
    assert new.tracing == old.tracing
    assert new.video_parser.transcript_model == old.video_parser.transcript_model
    excluded = {
        "video_model", "vision_source", "transcription_source",
        "vision_max_output_tokens", "vision_reasoning_effort", "vision_timeout_seconds",
        "fallback",
    }
    assert new.video_parser.model_dump(exclude=excluded) == old.video_parser.model_dump(exclude=excluded)
    assert new.video_parser.fallback.model_dump(exclude={"deepseek"}) == old.video_parser.fallback.model_dump(exclude={"deepseek"})
    declared = payload()["models"]["vision"]
    vision = new.resolve_model_binding("vision", environ={})
    for key, value in declared.items():
        assert getattr(vision, key) == value
    assert vision.supports_vision_input
    assert required_api_key_env_names(new) == required_api_key_env_names(old)
    assert new.stage_source("transcription") == old.stage_source("transcription")
    assert new.stage_source("vision") == new.model_sources[declared["source"]]
    assert old.supervisor_harness == "deepagents"
    assert new.supervisor_harness == "codex"
    assert new.resolve_model_binding("supervisor", environ={}).source == "codex_local"


def test_omitted_supervisor_configuration_preserves_legacy_model_components():
    data = payload()
    data.pop("supervisor_harness")
    data["models"].pop("supervisor")
    config = GatewayConfig.model_validate(data)
    assert config.supervisor_harness == "deepagents"
    assert config.resolve_model_binding("supervisor", environ={}) == config.resolve_model_binding("agent", environ={})
    data["models"]["agent"].update(source="codex_local", model="gpt-6-astra", fallback=None)
    legacy_component = GatewayConfig.model_validate(data)
    assert "supervisor_harness" not in legacy_component.model_dump()
    assert GatewayConfig.model_validate(legacy_component.model_dump()) == legacy_component


def test_explicit_deepagents_selection_rejects_a_legacy_native_agent_source():
    data = payload()
    data["models"].pop("supervisor")
    data["supervisor_harness"] = "deepagents"
    data["models"]["agent"].update(source="codex_local", model="gpt-6-astra", fallback=None)
    with pytest.raises(ValidationError, match="requires an OpenAI-compatible"):
        GatewayConfig.model_validate(data)
    data["models"]["agent"] = deepcopy(payload()["models"]["agent"])
    config = GatewayConfig.model_validate(data)
    assert config.model_dump()["supervisor_harness"] == "deepagents"
    assert GatewayConfig.model_validate(config.model_dump()) == config


def test_independent_supervisor_keeps_agent_models_and_environment_overrides_separate():
    config = GatewayConfig.model_validate(payload())
    environment = {
        "SEEKINO_AGENT_CHAT_MODEL": "chat-override",
        "SEEKINO_AGENT_FALLBACK_MODEL": "fallback-override",
    }
    supervisor = config.resolve_model_binding("supervisor", environ=environment)
    assert supervisor == config.resolve_model_binding("supervisor", environ={})
    assert supervisor.source == "codex_local"
    assert supervisor.model == "gpt-6-astra"
    assert supervisor.reasoning_effort == "low"
    for role in ("query", "evaluator", "chat"):
        assert config.resolve_model_binding(role, environ={}).source == "deepseek_api"
    agent = config.resolve_model_binding("agent", environ=environment)
    assert agent.model == "chat-override"
    assert agent.fallback.model == "fallback-override"
    assert GatewayConfig.model_validate(config.model_dump()) == config


def test_independent_api_supervisor_preserves_its_fallback_and_required_credentials():
    data = payload()
    data["supervisor_harness"] = "deepagents"
    data["model_sources"]["supervisor_api"] = {
        **data["model_sources"]["deepseek_api"], "api_key_env": "SUPERVISOR_API_KEY",
    }
    data["model_sources"]["supervisor_fallback"] = {
        "adapter": "ark", "provider": "ark", "api_key_env": "SUPERVISOR_FALLBACK_KEY",
    }
    data["model_sources"]["unused"] = {
        "adapter": "ark", "provider": "ark", "api_key_env": "UNUSED_KEY",
    }
    data["models"]["supervisor"] = {
        "source": "supervisor_api", "model": "supervisor-primary",
        "reasoning_effort": "high", "request_timeout_seconds": 360,
        "fallback": {"source": "supervisor_fallback", "model": "supervisor-fallback"},
    }
    config = GatewayConfig.model_validate(data)
    binding = config.resolve_model_binding("supervisor", environ={
        "SEEKINO_AGENT_CHAT_MODEL": "wrong-primary",
        "SEEKINO_AGENT_FALLBACK_MODEL": "wrong-fallback",
    })
    assert binding.model == "supervisor-primary"
    assert binding.fallback.model == "supervisor-fallback"
    assert binding.reasoning_effort == "high"
    assert binding.request_timeout_seconds == 360
    assert required_api_key_env_names(config) == (
        "ARK_API_KEY", "DEEPSEEK_API_KEY", "SUPERVISOR_API_KEY", "SUPERVISOR_FALLBACK_KEY",
    )
    assert GatewayConfig.model_validate(config.model_dump()) == config


@pytest.mark.parametrize(("harness", "binding", "message"), [
    ("other", {"source": "codex_local", "model": "test"}, "supervisor_harness"),
    ("codex", {"source": "deepseek_api", "model": "test"}, "requires a codex_app_server"),
    ("deepagents", {"source": "codex_local", "model": "test"}, "requires an OpenAI-compatible"),
    ("codex", {"source": "codex_local", "model": "test", "kind": "rerank"}, "must be a generative"),
    ("codex", {"source": "codex_local", "model": "test", "fallback": {
        "source": "deepseek_api", "model": "fallback",
    }}, "does not support fallback"),
    ("codex", {"source": "codex_local", "model": "test", "fallback": {
        "source": "codex_local", "model": "fallback",
    }}, "does not support fallback"),
    ("deepagents", {"source": "deepseek_api", "model": "test", "fallback": {
        "source": "codex_local", "model": "fallback",
    }}, "fallback requires an Ark API source"),
    ("deepagents", {"source": "deepseek_api", "model": "test", "fallback": {
        "source": "ark_default", "model": "fallback", "kind": "transcription",
    }}, "fallback must be a generative"),
    ("deepagents", {"source": "deepseek_api", "model": "test", "fallback": {
        "source": "ark_default", "model": "fallback", "fallback": {
            "source": "ark_default", "model": "recursive",
        },
    }}, "support one configured fallback"),
])
def test_invalid_supervisor_harness_bindings_fail_during_config_loading(harness, binding, message):
    data = payload()
    data["supervisor_harness"] = harness
    data["models"]["supervisor"] = binding
    with pytest.raises(ValidationError, match=message):
        GatewayConfig.model_validate(data)


def test_codex_supervisor_without_independent_binding_requires_the_agent_source_to_match():
    data = payload()
    data["models"].pop("supervisor")
    with pytest.raises(ValidationError, match="requires a codex_app_server"):
        GatewayConfig.model_validate(data)


def test_vision_controls_accept_larger_budget_and_preserve_explicit_reasoning():
    data = payload()
    data["models"]["vision"].update(
        max_output_tokens=131072, reasoning_effort="low", context_window_tokens=262144,
    )
    config = GatewayConfig.model_validate(data)
    assert config.video_parser.vision_max_output_tokens == 131072
    assert config.video_parser.vision_reasoning_effort == "low"
    assert config.resolve_model_binding("vision", environ={}).max_output_tokens == 131072
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


def test_deepseek_source_supports_visual_understanding():
    data = payload()
    data["models"]["vision"].update(source="deepseek_api", model="deepseek-flash")
    config = GatewayConfig.model_validate(data)
    assert config.stage_source("vision") == config.model_sources["deepseek_api"]
    assert config.normalized_models["vision"].supports_vision_input


def test_chat_protocol_cannot_be_used_for_transcription():
    data = payload()
    data["models"]["transcription"]["source"] = "deepseek_api"
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


def test_models_requires_version_and_validates_each_named_binding():
    data = payload()
    del data["config_version"]
    with pytest.raises(ValidationError, match="config_version"):
        GatewayConfig.model_validate(data)
    data["config_version"] = 2
    data["models"]["document_reader"] = {"source": "deepseek_api", "model": "test", "kind": "unknown"}
    with pytest.raises(ValidationError, match="kind"):
        GatewayConfig.model_validate(data)


def test_existing_agent_chat_alias_still_loads():
    data = yaml.safe_load(LEGACY.read_text())
    data["agent_chat"] = data.pop("agent_model")
    assert (
        GatewayConfig.model_validate(data).agent_model
        == load_config(LEGACY).agent_model
    )


def test_arbitrary_model_references_and_explicit_query_do_not_use_business_aliases():
    data = payload()
    data["models"]["query"] = {"source": "ark_default", "model": "explicit-query"}
    data["models"]["document_reader"] = {
        "source": "deepseek_api", "model": "custom-reader", "request_timeout_seconds": 73,
    }
    config = GatewayConfig.model_validate(data)
    assert config.resolve_model_binding("query", environ={}).model == "explicit-query"
    reader = config.resolve_model_binding("document_reader", environ={"SEEKINO_AGENT_CHAT_MODEL": "other"})
    assert reader.model == "custom-reader" and reader.request_timeout_seconds == 73
    assert not reader.supports_vision_input
    assert GatewayConfig.model_validate(config.model_dump()) == config


def test_image_declaration_requires_adapter_capability_and_is_preserved_for_codex():
    data = payload()
    data["model_sources"]["local_codex"] = {"adapter": "codex_app_server", "provider": "codex"}
    data["models"]["image_reader"] = {
        "source": "local_codex", "model": "gpt-6-astra", "supports_vision_input": True,
        "request_timeout_seconds": 600, "max_output_tokens": 8192,
        "context_window_tokens": 128000, "max_input_chars": 400000,
    }
    config = GatewayConfig.model_validate(data)
    assert config.resolve_model_binding("image_reader", environ={}).supports_vision_input
    data["model_sources"]["text_only"] = {"adapter": "claude_sdk", "provider": "claude_code"}
    data["models"]["image_reader"]["source"] = "text_only"
    with pytest.raises(ValidationError, match="does not support image input"):
        GatewayConfig.model_validate(data)


def test_frozen_bindings_preserve_all_parameters_and_skip_environment_overrides():
    config = GatewayConfig.model_validate(payload())
    frozen = ModelBinding(
        source="ark_default", model="frozen-vision", kind="generative", supports_vision_input=True,
        request_timeout_seconds=91, max_output_tokens=65536, reasoning_effort="none",
        context_window_tokens=131072, context_safety_margin_tokens=1024,
        auto_compact_tokens=50000, compact_target_tokens=30000, max_input_chars=500000,
        fallback=ModelBinding(
            source="deepseek_api", model="frozen-fallback", supports_vision_input=True,
            request_timeout_seconds=72, max_output_tokens=4096, reasoning_effort="low",
        ),
    )
    resolved = config.resolve_model_binding(frozen, environ={
        "SEEKINO_VIDEO_MODEL": "later-vision", "SEEKINO_FALLBACK_ENABLED": "false",
        "SEEKINO_AGENT_FALLBACK_MODEL": "later-fallback",
    })
    assert resolved.model_dump(exclude={"fallback"}) == frozen.model_dump(exclude={"fallback"})
    assert resolved.fallback.model == "frozen-fallback"
    assert resolved.fallback.request_timeout_seconds == 72
    assert resolved.fallback.max_output_tokens == 4096
    assert resolved.fallback.reasoning_effort == "low"
    assert config.resolve_model_binding(resolved, environ={"SEEKINO_VIDEO_MODEL": "ignored"}) == resolved


def test_old_vision_output_budget_uses_visual_context_compatibility_defaults():
    config = load_config(LEGACY)
    binding = config.resolve_model_binding(ModelBinding(
        source="ark_default", model="old-vision", supports_vision_input=True, max_output_tokens=65536,
    ), environ={})
    assert binding.context_window_tokens == 128000
    assert binding.context_safety_margin_tokens == 2048
    assert binding.max_input_chars == 400000
    assert 0 < binding.compact_target_tokens < binding.auto_compact_tokens < 128000 - 65536 - 2048


def test_context_budget_reserves_the_largest_fallback_output():
    config = GatewayConfig.model_validate(payload())
    binding = config.resolve_model_binding(ModelBinding(
        source="ark_default", model="main", supports_vision_input=True,
        context_window_tokens=32768, max_output_tokens=1024,
        fallback=ModelBinding(source="ark_default", model="fallback", supports_vision_input=True, max_output_tokens=8192),
    ), environ={})
    assert binding.auto_compact_tokens < 32768 - 8192 - binding.context_safety_margin_tokens
    with pytest.raises(ValueError, match="output budget"):
        config.resolve_model_binding(binding.model_copy(update={"context_window_tokens": 8192}), environ={})
    with pytest.raises(ValueError, match="compaction budgets"):
        config.resolve_model_binding(binding.model_copy(update={"auto_compact_tokens": 30000}), environ={})


def test_model_name_environment_priority_and_fallback_switch_remain_at_resolver():
    config = GatewayConfig.model_validate(payload())
    binding = config.resolve_model_binding("agent", environ={
        "SEEKINO_AGENT_CHAT_MODEL": " preferred ", "SEEKINO_DEEPSEEK_CHAT_MODEL": "older",
        "SEEKINO_AGENT_FALLBACK_MODEL": "fallback-override",
    })
    assert binding.model == "preferred" and binding.fallback.model == "fallback-override"
    assert config.resolve_model_binding("query", environ={"SEEKINO_AGENT_QUERY_MODEL": "query-only"}).model == "query-only"
    assert config.resolve_model_binding("vision", environ={"SEEKINO_FALLBACK_ENABLED": "off"}).fallback is None
    with pytest.raises(ValueError, match="boolean"):
        config.resolve_model_binding("vision", environ={"SEEKINO_FALLBACK_ENABLED": "invalid"})
    data = payload()
    data["models"]["vision"]["fallback"] = None
    assert GatewayConfig.model_validate(data).resolve_model_binding("vision", environ={"SEEKINO_FALLBACK_ENABLED": "true"}).fallback is None
    data = payload()
    data["video_parser"]["fallback"]["enabled"] = False
    config = GatewayConfig.model_validate(data)
    restored = GatewayConfig.model_validate(config.model_dump())
    assert config.resolve_model_binding("vision", environ={"SEEKINO_FALLBACK_ENABLED": "true"}) == restored.resolve_model_binding("vision", environ={"SEEKINO_FALLBACK_ENABLED": "true"})


def test_embedding_binding_keeps_endpoint_encoder_identity_and_frozen_fps():
    data = payload()
    data.pop("multimodal_embedding", None)
    data["models"]["embedding"] = {
        "source": "ark_default", "model": "ep-configured", "encoder_model": "doubao-embedding-vision-250615",
        "dimensions": 1536, "video_fps": 0.5, "request_timeout_seconds": 87,
    }
    config = GatewayConfig.model_validate(data)
    binding = config.resolve_embedding_binding(environ={})
    assert isinstance(binding, EmbeddingModelBinding) and binding.kind == "embedding"
    assert binding.model == "ep-configured" and binding.encoder_model == "doubao-embedding-vision-250615"
    assert binding.dimensions == 1536 and binding.request_timeout_seconds == 87
    assert config.resolve_embedding_binding(binding, environ={
        "SEEKINO_VIDEO_EMBEDDING_MODEL": "ep-later", "SEEKINO_VIDEO_EMBEDDING_FPS": "invalid",
    }) == binding
    assert GatewayConfig.model_validate(config.model_dump()) == config
    del data["models"]["embedding"]["encoder_model"]
    with pytest.raises(ValueError, match="encoder_model"):
        GatewayConfig.model_validate(data).resolve_embedding_binding(environ={})


def test_legacy_embedding_environment_endpoint_override_uses_configured_encoder():
    config = load_config(LEGACY)
    binding = config.resolve_embedding_binding(environ={
        "SEEKINO_VIDEO_EMBEDDING_MODEL": "ep-override", "SEEKINO_VIDEO_EMBEDDING_FPS": "2",
    })
    assert binding.model == "ep-override"
    assert binding.encoder_model == config.multimodal_embedding.model
    assert binding.video_fps == 2
    assert config.source(binding.source).api_key_env == "ARK_API_KEY"
    frozen = config.normalized_models["embedding"].model_copy(update={"model": "ep-old-task"})
    assert config.resolve_embedding_binding(frozen, environ={}).encoder_model == config.multimodal_embedding.model


def test_transcription_does_not_inherit_agent_output_limits():
    config = load_config(LEGACY)
    assert config.resolve_model_binding("transcription", environ={}).max_output_tokens is None


@pytest.mark.parametrize("kind", ["embedding", "transcription"])
def test_non_generative_bindings_reject_unimplemented_fallbacks(kind):
    data = payload()
    if kind == "embedding":
        data.pop("multimodal_embedding", None)
        data["models"][kind] = {
            "source": "ark_default", "model": "encoder", "dimensions": 2048, "video_fps": 1,
        }
    data["models"][kind]["fallback"] = {"source": "ark_default", "model": "other"}
    with pytest.raises(ValidationError, match=f"{kind} does not support fallback"):
        GatewayConfig.model_validate(data)


def test_embedding_cannot_borrow_generative_vision_declaration():
    data = payload()
    data.pop("multimodal_embedding", None)
    data["models"]["embedding"] = {
        "source": "ark_default", "model": "encoder", "dimensions": 2048,
        "video_fps": 1, "supports_vision_input": True,
    }
    with pytest.raises(ValidationError, match="embedding does not use generative"):
        GatewayConfig.model_validate(data)


def test_rerank_is_an_independent_structured_generation_binding():
    data = payload()
    data["models"]["rerank"] = {"source": "ark_default", "model": "reranker", "request_timeout_seconds": 51}
    config = GatewayConfig.model_validate(data)
    rerank = config.resolve_model_binding("rerank", environ={})
    assert rerank.kind == "rerank" and rerank.model == "reranker" and rerank.request_timeout_seconds == 51
    assert config.resolve_model_binding("agent", environ={}).model != rerank.model
    legacy = load_config(LEGACY)
    assert legacy.resolve_model_binding("rerank", environ={}).model == legacy.agent_model.model


def test_large_explicit_agent_and_fallback_budgets_survive_legacy_projection():
    data = payload()
    budget = {
        "max_output_tokens": 65536, "context_window_tokens": 196608,
        "context_safety_margin_tokens": 2048, "auto_compact_tokens": 90000,
        "compact_target_tokens": 60000,
    }
    data["models"]["agent"].update(budget)
    data["models"]["agent"]["fallback"].update(budget)
    data["models"]["vision_fallback"].update(budget)
    config = GatewayConfig.model_validate(data)
    agent = config.resolve_model_binding("agent", environ={})
    assert agent.max_output_tokens == agent.fallback.max_output_tokens == 65536
    assert agent.context_window_tokens == agent.fallback.context_window_tokens == 196608
    assert config.resolve_model_binding("vision_fallback", environ={}).max_output_tokens == 65536
    assert GatewayConfig.model_validate(config.model_dump()) == config


def test_canonical_generation_can_fallback_to_another_model_on_the_same_connection():
    data = payload()
    data["models"]["agent"]["fallback"].update(source="deepseek_api", model="another-model")
    config = GatewayConfig.model_validate(data)
    agent = config.resolve_model_binding("agent", environ={})
    assert agent.source == agent.fallback.source and agent.model != agent.fallback.model


def test_credentials_include_each_active_recursive_fallback_but_not_unused_sources():
    data = payload()
    data["model_sources"]["secondary"] = {
        "adapter": "ark", "provider": "ark", "api_key_env": "SECONDARY_ARK_KEY",
    }
    data["model_sources"]["unused"] = {
        "adapter": "ark", "provider": "ark", "api_key_env": "UNUSED_ARK_KEY",
    }
    data["models"]["agent"]["fallback"]["fallback"] = {"source": "secondary", "model": "last-model"}
    names = required_api_key_env_names(GatewayConfig.model_validate(data))
    assert "SECONDARY_ARK_KEY" in names and "UNUSED_ARK_KEY" not in names


def test_source_credentials_resolve_from_the_supplied_snapshot_only():
    config = GatewayConfig.model_validate(payload())
    source = config.source("ark_default")
    assert resolve_source_api_key(source, environ={"ARK_API_KEY": " test-token "}) == "test-token"
    with pytest.raises(RuntimeError, match="ARK_API_KEY"):
        resolve_source_api_key(source, environ={})
    data = payload()
    data["model_sources"]["local_codex"] = {"adapter": "codex_app_server", "provider": "codex"}
    assert resolve_source_api_key(GatewayConfig.model_validate(data).source("local_codex"), environ={}) is None


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
