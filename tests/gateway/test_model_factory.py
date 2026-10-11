from __future__ import annotations

import importlib
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

import gateway.model_factory as model_factory
from gateway.config import (
    ArkModelSourceConfig, ClaudeCodeModelSourceConfig, CodexModelSourceConfig,
    EmbeddingModelBinding, ModelBinding, OpenAIAudioSourceConfig,
    OpenAIModelSourceConfig, OpenAIVisionSourceConfig, load_config,
)
from gateway.errors import ContentFilterError
from gateway.multimodal import ImageInput, TextInput, VideoInput


class Result(BaseModel):
    answer: str


def _config():
    config = load_config(Path(__file__).parent / "fixtures" / "legacy-config.yaml")
    config.model_sources.update({
        "native": CodexModelSourceConfig(),
        "claude": ClaudeCodeModelSourceConfig(),
        "api": ArkModelSourceConfig(api_key_env="API_KEY"),
        "vision_api": OpenAIVisionSourceConfig(
            provider="vision-provider", base_url="https://vision.example/v1", api_key_env="VISION_KEY",
        ),
        "deepseek_api": OpenAIModelSourceConfig(
            provider="deepseek", profile="deepseek", base_url="https://deepseek.example/v1", api_key_env="DS_KEY",
        ),
        "standard_api": OpenAIModelSourceConfig(
            provider="portable", base_url="https://portable.example/v1", api_key_env="PORTABLE_KEY",
        ),
        "audio": OpenAIAudioSourceConfig(
            provider="speech", base_url="https://speech.example/v1", api_key_env="SPEECH_KEY",
        ),
    })
    return config


def _binding(source="native", **kwargs):
    return ModelBinding(source=source, model="configured-model", request_timeout_seconds=10, **kwargs)


class _Delegate:
    def __init__(self, **kwargs):
        self.options = kwargs
        self.calls = []
        self.closed = 0

    def generate(self, inputs, **kwargs):
        self.calls.append((inputs, kwargs))
        return kwargs["result_type"](answer="ok")

    def close(self):
        self.closed += 1


def _replace(monkeypatch, module, name, constructed):
    def construct(*args, **kwargs):
        delegate = _Delegate(**kwargs)
        constructed.append(delegate)
        return delegate
    monkeypatch.setattr(importlib.import_module(module), name, construct)


@pytest.mark.parametrize(("source", "module", "name"), [
    ("api", "gateway.ark.multimodal", "ArkMultimodalModel"),
    ("vision_api", "gateway.openai.vision", "OpenAIVisionModel"),
    ("deepseek_api", "gateway.openai.multimodal", "DeepSeekMultimodalModel"),
    ("standard_api", "gateway.openai.vision", "OpenAIVisionModel"),
    ("native", "gateway.codex.agent", "CodexAgentModel"),
])
def test_factory_selects_configured_protocol_and_controls(monkeypatch, source, module, name):
    constructed = []
    _replace(monkeypatch, module, name, constructed)
    client = model_factory.GatewayClient(_config(), environ={
        "API_KEY": "ark-key", "VISION_KEY": "vision-key", "DS_KEY": "ds-key", "PORTABLE_KEY": "portable-key",
    })
    model = client.create("generative", _binding(source, max_output_tokens=2048))
    try:
        assert model.generate([TextInput("question")], schema_name="answer", result_type=Result).answer == "ok"
        assert model.model_name == "configured-model"
        assert constructed[0].options["model"] == "configured-model"
        assert constructed[0].options["max_output_tokens"] == 2048
        assert constructed[0].options["request_timeout_seconds"] == 10
        assert constructed[0].calls[0][1]["context_window_policy"].output_reserve_tokens == 2048
    finally:
        model.close()
        client.close()
    assert constructed[0].closed == 1


def test_frozen_binding_never_reapplies_model_environment(monkeypatch):
    constructed = []
    _replace(monkeypatch, "gateway.codex.agent", "CodexAgentModel", constructed)
    client = model_factory.GatewayClient(_config(), environ={"ANIVAG_VIDEO_MODEL": "different-remote-endpoint"})
    binding = _binding(supports_vision_input=True)
    model = client.create_generative_model(binding)
    try:
        assert model.binding.model == "configured-model"
        assert model.visual_input_mode == "ordered_frames"
        assert constructed[0].options["model"] == "configured-model"
    finally:
        client.close()


def test_source_capability_error_precedes_model_construction(monkeypatch):
    constructed = []
    _replace(monkeypatch, "gateway.claude_code.agent", "ClaudeAgentModel", constructed)
    client = model_factory.GatewayClient(_config(), environ={})
    with pytest.raises(ValueError, match="does not support image input"):
        client.create_generative_model(_binding("claude", supports_vision_input=True))
    assert constructed == []
    client.close()


def test_generate_rejects_media_outside_configured_capability(monkeypatch, tmp_path):
    constructed = []
    _replace(monkeypatch, "gateway.codex.agent", "CodexAgentModel", constructed)
    client = model_factory.GatewayClient(_config(), environ={})
    text = client.create_generative_model(_binding())
    visual = client.create_generative_model(_binding(supports_vision_input=True))
    try:
        with pytest.raises(ValueError, match="does not support ImageInput"):
            text.generate([ImageInput(tmp_path / "frame.png")], schema_name="answer", result_type=Result)
        with pytest.raises(ValueError, match="ordered ImageInput frames"):
            visual.generate([VideoInput(tmp_path / "clip.mp4", 1)], schema_name="answer", result_type=Result)
        assert all(delegate.calls == [] for delegate in constructed)
    finally:
        client.close()


def test_api_tool_fallback_is_passed_into_existing_session(monkeypatch):
    api, fallback = [], []
    _replace(monkeypatch, "gateway.ark.multimodal", "ArkMultimodalModel", api)
    _replace(monkeypatch, "gateway.openai.vision", "OpenAIVisionModel", fallback)
    from langchain_core.tools import StructuredTool
    tool = StructuredTool.from_function(lambda value: value, name="read", description="Read evidence")
    client = model_factory.GatewayClient(_config(), environ={"API_KEY": "a", "VISION_KEY": "b"})
    model = client.create_generative_model(_binding("api", fallback=_binding("vision_api")))
    try:
        model.generate([TextInput("evidence")], schema_name="answer", result_type=Result, tools=[tool])
        arguments = api[0].calls[0][1]
        assert arguments["fallback_model"] is fallback[0]
        assert arguments["fallback_on_error"](ContentFilterError("filtered", code="content_filter"))
        assert not arguments["fallback_on_error"](RuntimeError("network failure"))
        assert fallback[0].calls == []
    finally:
        client.close()
    assert api[0].closed == fallback[0].closed == 1


def test_deepagents_dispatch_stays_in_gateway(monkeypatch):
    delegates = []
    _replace(monkeypatch, "gateway.ark.multimodal", "ArkMultimodalModel", delegates)
    calls = []
    def generate(adapter, inputs, **kwargs):
        calls.append((adapter, inputs, kwargs))
        return Result(answer="grounded")
    monkeypatch.setattr("gateway.agent_model.deepagents_multimodal.generate_with_deepagents", generate)
    from langchain_core.tools import StructuredTool
    tool = StructuredTool.from_function(lambda value: value, name="read", description="Read evidence")
    client = model_factory.GatewayClient(_config(), environ={"API_KEY": "a"})
    try:
        model = client.create_generative_model(_binding("api"))
        assert model.generate(
            [TextInput("evidence")], schema_name="answer", result_type=Result,
            tools=[tool], tool_harness="deepagents",
        ).answer == "grounded"
        assert calls[0][0] is delegates[0] and delegates[0].calls == []
    finally:
        client.close()


@pytest.mark.parametrize("tool_harness", ["legacy", "deepagents"])
def test_shared_api_fallback_preserves_smaller_frozen_context_limits(monkeypatch, tool_harness):
    primary, fallback, requests = [], [], []
    _replace(monkeypatch, "gateway.ark.multimodal", "ArkMultimodalModel", primary)
    _replace(monkeypatch, "gateway.openai.vision", "OpenAIVisionModel", fallback)
    def deepagents(adapter, inputs, **kwargs):
        requests.append(kwargs)
        return Result(answer="ok")
    monkeypatch.setattr("gateway.agent_model.deepagents_multimodal.generate_with_deepagents", deepagents)
    from langchain_core.tools import StructuredTool
    tool = StructuredTool.from_function(lambda: "{}", name="read", description="Read evidence")
    client = model_factory.GatewayClient(_config(), environ={"API_KEY": "a", "VISION_KEY": "b"})
    binding = _binding("api", context_window_tokens=128000, max_output_tokens=2048, max_input_chars=400000,
                       fallback=_binding("vision_api", context_window_tokens=16000,
                                         max_output_tokens=1024, max_input_chars=2000,
                                         auto_compact_tokens=9000, compact_target_tokens=6000))
    try:
        model = client.create_generative_model(binding)
        model.generate([TextInput("evidence")], schema_name="answer", result_type=Result,
                       tools=[tool], tool_harness=tool_harness)
        options = requests[0] if requests else primary[0].calls[0][1]
        assert options["fallback_model"] is fallback[0]
        policy = options["context_window_policy"]
        assert policy.context_window_tokens == 16000
        assert policy.output_reserve_tokens == 2048
        assert policy.auto_compact_tokens < policy.maximum_input_tokens
        assert policy.compact_target_tokens < policy.auto_compact_tokens
        assert options["max_input_chars"] == 2000
        from gateway.agent_model.errors import ModelInputTooLargeError
        with pytest.raises(ModelInputTooLargeError):
            model.generate([TextInput("x" * 2001)], schema_name="answer", result_type=Result,
                           tools=[tool], tool_harness=tool_harness)
        assert len(requests or primary[0].calls) == 1
        assert not fallback[0].calls
    finally:
        client.close()


@pytest.mark.parametrize("source", ["api", "native"])
@pytest.mark.parametrize("smaller_caller", [False, True])
def test_generate_context_policy_cannot_expand_binding_and_keeps_smaller_caller_policy(
    monkeypatch, source, smaller_caller,
):
    from gateway.agent_model.context import ContextWindowPolicy
    from gateway.agent_model.errors import ModelInputTooLargeError
    delegates = []
    module, name = (
        ("gateway.ark.multimodal", "ArkMultimodalModel") if source == "api"
        else ("gateway.codex.agent", "CodexAgentModel")
    )
    _replace(monkeypatch, module, name, delegates)
    client = model_factory.GatewayClient(_config(), environ={"API_KEY": "a"})
    binding = _binding(source, context_window_tokens=32000, max_output_tokens=2048,
                       context_safety_margin_tokens=1024, auto_compact_tokens=24000,
                       compact_target_tokens=12000, max_input_chars=400000)
    caller = (
        ContextWindowPolicy(16000, 1024, 2048, 10000, 8000) if smaller_caller
        else ContextWindowPolicy(128000, 8192, 4096, 100000, 50000)
    )
    try:
        model = client.create_generative_model(binding)
        model.generate([TextInput("evidence")], schema_name="answer", result_type=Result,
                       context_window_policy=caller)
        effective = delegates[0].calls[0][1]["context_window_policy"]
        assert effective.context_window_tokens == (16000 if smaller_caller else 32000)
        assert effective.output_reserve_tokens >= binding.max_output_tokens
        assert effective.safety_margin_tokens >= binding.context_safety_margin_tokens
        if smaller_caller:
            assert effective.auto_compact_tokens == 10000 and effective.compact_target_tokens == 8000
        with pytest.raises(ModelInputTooLargeError):
            model.generate([TextInput("x" * 100000)], schema_name="answer", result_type=Result,
                           context_window_policy=caller)
        assert len(delegates[0].calls) == 1
    finally:
        client.close()


def test_outer_fallback_clamps_caller_policy_to_its_own_context_budget(monkeypatch):
    from gateway.agent_model.context import ContextWindowPolicy
    native, fallback = [], []
    _replace(monkeypatch, "gateway.codex.agent", "CodexAgentModel", native)
    _replace(monkeypatch, "gateway.ark.multimodal", "ArkMultimodalModel", fallback)
    client = model_factory.GatewayClient(_config(), environ={"API_KEY": "a"})
    binding = _binding(
        context_window_tokens=64000, max_output_tokens=2048,
        fallback=_binding("api", context_window_tokens=16000, max_output_tokens=1024,
                          context_safety_margin_tokens=1024, auto_compact_tokens=10000,
                          compact_target_tokens=5000),
    )
    try:
        model = client.create_generative_model(binding)
        def filtered(_inputs, **_kwargs):
            raise ContentFilterError("filtered", code="content_filter")
        native[0].generate = filtered
        assert model.generate([TextInput("evidence")], schema_name="answer", result_type=Result,
                              context_window_policy=ContextWindowPolicy(128000, 4096, 4096, 100000, 50000)).answer == "ok"
        effective = fallback[0].calls[0][1]["context_window_policy"]
        assert effective.context_window_tokens == 16000
        assert effective.maximum_input_tokens <= 16000 - 1024 - 1024
        assert effective.auto_compact_tokens <= 10000
        assert effective.compact_target_tokens <= 5000
    finally:
        client.close()


def test_actual_native_generate_uses_real_images_and_outer_fallback_deadline(monkeypatch, tmp_path):
    from gateway.codex.agent import CodexAgentModel
    from openai_codex import ImageInput as CodexImageInput
    clock = [100.0]
    monkeypatch.setattr(model_factory, "monotonic", lambda: clock[0])
    sessions = []
    class Session:
        tool_call_count = successful_tool_call_count = 0
        def __init__(self):
            self.closed = False
            self.inputs = None
        def run_turn(self, inputs, **kwargs):
            self.inputs = inputs
            clock[0] += 3
            raise ContentFilterError("filtered", code="content_filter")
        def close(self):
            self.closed = True
        interrupt = close
    def native(**kwargs):
        session = Session()
        sessions.append(session)
        return CodexAgentModel(**kwargs, session_factory=lambda **_kwargs: session)
    monkeypatch.setattr("gateway.codex.agent.CodexAgentModel", native)
    fallbacks = []
    _replace(monkeypatch, "gateway.ark.multimodal", "ArkMultimodalModel", fallbacks)
    client = model_factory.GatewayClient(_config(), environ={"API_KEY": "a"})
    binding = _binding(supports_vision_input=True, fallback=_binding("api", supports_vision_input=True))
    image = tmp_path / "frame.png"
    image.write_bytes(b"frame")
    model = client.create_generative_model(binding)
    try:
        result = model.generate(
            [TextInput("evidence"), ImageInput(image)], schema_name="answer", result_type=Result,
            request_timeout_seconds=5, tool_harness="deepagents",
        )
        assert result.answer == "ok"
        assert isinstance(sessions[0].inputs[1], CodexImageInput)
        assert sessions[0].closed
        kwargs = fallbacks[0].calls[0][1]
        assert kwargs["request_timeout_seconds"] == 2
    finally:
        client.close()
    assert fallbacks[0].closed == 1


def test_outer_fallback_does_not_hide_cancellation(monkeypatch):
    delegates = []
    _replace(monkeypatch, "gateway.codex.agent", "CodexAgentModel", delegates)
    fallbacks = []
    _replace(monkeypatch, "gateway.ark.multimodal", "ArkMultimodalModel", fallbacks)
    client = model_factory.GatewayClient(_config(), environ={"API_KEY": "a"})
    model = client.create_generative_model(_binding(fallback=_binding("api")))
    def cancelled():
        raise RuntimeError("cancelled")
    try:
        with pytest.raises(RuntimeError, match="cancelled"):
            model.generate([TextInput("evidence")], schema_name="answer", result_type=Result, check_control=cancelled)
        assert delegates[0].calls == fallbacks[0].calls == []
    finally:
        client.close()


def test_factory_releases_closed_models_and_closes_every_remaining_resource(monkeypatch):
    delegates = []
    _replace(monkeypatch, "gateway.codex.agent", "CodexAgentModel", delegates)
    client = model_factory.GatewayClient(_config(), environ={})
    first = client.create_generative_model(_binding())
    client.create_generative_model(_binding())
    first.close()
    assert len(client._resources) == 1
    client.close()
    client.close()
    assert [delegate.closed for delegate in delegates] == [1, 1]
    assert client._resources == set()
    with pytest.raises(RuntimeError, match="closed"):
        client.create_generative_model(_binding())


def test_failure_building_fallback_closes_primary(monkeypatch):
    delegates = []
    _replace(monkeypatch, "gateway.codex.agent", "CodexAgentModel", delegates)
    client = model_factory.GatewayClient(_config(), environ={})
    with pytest.raises(RuntimeError, match="API_KEY"):
        client.create_generative_model(_binding(fallback=_binding("api")))
    assert delegates[0].closed == 1 and client._resources == set()
    client.close()


def test_client_close_attempts_every_resource_after_one_close_fails(monkeypatch):
    delegates = []
    _replace(monkeypatch, "gateway.codex.agent", "CodexAgentModel", delegates)
    client = model_factory.GatewayClient(_config(), environ={})
    client.create_generative_model(_binding())
    client.create_generative_model(_binding())
    def fail():
        delegates[0].closed += 1
        raise RuntimeError("close failed")
    delegates[0].close = fail
    with pytest.raises(RuntimeError, match="close failed"):
        client.close()
    assert [delegate.closed for delegate in delegates] == [1, 1]
    assert client._resources == set()


def test_embedding_and_transcription_use_resolved_typed_bindings(monkeypatch):
    calls = []
    def build(binding, source, **kwargs):
        calls.append((binding, source, kwargs))
        return SimpleNamespace(close=lambda: None, model_name=binding.model)
    monkeypatch.setattr("gateway.embedding.build_embedding_model", build)
    monkeypatch.setattr("gateway.transcription.build_transcription_model", build)
    client = model_factory.GatewayClient(_config(), environ={
        "API_KEY": "a", "SPEECH_KEY": "s", "ANIVAG_VIDEO_EMBEDDING_MODEL": "ignored-endpoint",
    })
    embedding = EmbeddingModelBinding(
        source="api", model="frozen-encoder", dimensions=1024, video_fps=0.5,
    )
    try:
        assert client.create("embedding", embedding).model_name == "frozen-encoder"
        assert calls[0][2]["api_key"] == "a"
        speech = client.create("transcription", _binding("audio", kind="transcription"))
        assert speech.model_name == "configured-model" and calls[1][2]["api_key"] == "s"
        info = client.describe_embedding(embedding)
        assert info.encoder_model == "frozen-encoder" and info.dimensions == 1024
    finally:
        client.close()


def test_generic_rerank_wraps_generation_and_releases_delegate(monkeypatch):
    delegates = []
    _replace(monkeypatch, "gateway.codex.agent", "CodexAgentModel", delegates)
    import gateway.rerank
    monkeypatch.setattr(gateway.rerank, "StructuredRerankModel", lambda delegate: SimpleNamespace(
        delegate=delegate, close=delegate.close,
    ))
    client = model_factory.GatewayClient(_config(), environ={})
    model = client.create("rerank", _binding(kind="rerank"))
    assert model.delegate.model_name == "configured-model"
    model.close()
    assert client._resources == set()
    client.close()
    assert delegates[0].closed == 1


def test_agent_factory_preserves_existing_api_with_frozen_selection():
    client = model_factory.GatewayClient(_config(), environ={"ANIVAG_AGENT_CHAT_MODEL": "ignored"})
    try:
        factory = client.create_agent_factory(_binding())
        assert factory._config.model == "configured-model"
        assert factory.provider_name == "codex"
        assert callable(factory.build_structured) and callable(factory.build_loop)
    finally:
        client.close()


@pytest.mark.parametrize("builder", ["build_structured", "build_loop", "build_deep_agent"])
@pytest.mark.parametrize("smaller_runtime", [False, True])
def test_agent_factory_bounds_canonical_controls_and_keeps_smaller_runtime_values(
    monkeypatch, builder, smaller_runtime,
):
    from gateway.agent_model.context import ContextWindowPolicy
    constructed = []

    class Model:
        def __init__(self, *args, **options):
            self.options = options
            self.model_name = args[0] if args else options.get("model")
            self.calls = []
            constructed.append(self)

        def run_structured(self, *_args, **options):
            self.calls.append(options)
            return Result(answer="bounded")

        def run_agent_loop(self, *_args, **options):
            self.calls.append(options)

        run_agent = run_agent_loop

    monkeypatch.setattr("gateway.agent_model.factory.ArkStructuredToolAgentModel", Model)
    monkeypatch.setattr("gateway.agent_model.factory.ArkFunctionCallingModel", Model)
    monkeypatch.setattr("gateway.agent_model.factory.DeepAgentsSupervisorModel", Model)
    config = _config()
    config.model_sources["api_fallback"] = ArkModelSourceConfig(api_key_env="API_KEY")
    binding = _binding(
        "api", max_output_tokens=2048, max_input_chars=2000,
        context_window_tokens=32000, context_safety_margin_tokens=1024,
        auto_compact_tokens=24000, compact_target_tokens=12000,
        fallback=_binding("api_fallback", max_output_tokens=1024, max_input_chars=1500,
                          context_window_tokens=16000,
                          context_safety_margin_tokens=1024, auto_compact_tokens=10000,
                          compact_target_tokens=5000).model_copy(update={"request_timeout_seconds": 7}),
    )
    client = model_factory.GatewayClient(config, environ={"API_KEY": "a"})
    runtime = (
        dict(request_timeout_seconds=3, max_output_tokens=512, max_input_chars=1000,
             context_window_policy=ContextWindowPolicy(12000, 512, 1024, 8000, 4000))
        if smaller_runtime else
        dict(request_timeout_seconds=60, max_output_tokens=4096, max_input_chars=200000,
             context_window_policy=ContextWindowPolicy(64000, 4096, 4096, 48000, 24000))
    )
    try:
        factory = client.create_agent_factory(binding)
        model = getattr(factory, builder)(model="legacy-role-model", reasoning_effort="none", **runtime)
        assert constructed[0].model_name == "legacy-role-model"
        assert constructed[0].options["max_output_tokens"] == (512 if smaller_runtime else 2048)
        assert constructed[0].options["request_timeout_seconds"] == (3 if smaller_runtime else 10)
        assert constructed[0].options.get("fallback_max_output_tokens", 0) <= 1024
        assert constructed[0].options.get("fallback_request_timeout_seconds", 0) <= 7
        if builder == "build_structured":
            assert constructed[0].options["fallback_adapter"].max_output_tokens == (512 if smaller_runtime else 1024)
            assert constructed[0].options["fallback_adapter"].request_timeout_seconds == (3 if smaller_runtime else 7)
        policy = constructed[0].options["context_window_policy"]
        assert policy.context_window_tokens == (12000 if smaller_runtime else 16000)
        assert policy.auto_compact_tokens < policy.maximum_input_tokens
        assert policy.compact_target_tokens < policy.auto_compact_tokens
        if smaller_runtime:
            assert policy.auto_compact_tokens == 8000 and policy.compact_target_tokens == 4000
        if builder == "build_deep_agent":
            assert constructed[1].options["agent_model"] is constructed[0]
            assert constructed[1].options["max_input_chars"] == (1000 if smaller_runtime else 1500)
            method = model.run_agent
        elif builder == "build_loop":
            method = model.run_agent_loop
        else:
            assert constructed[0].options["max_input_chars"] == (1000 if smaller_runtime else 1500)
            method = model.run_structured
        delegate = constructed[-1]
        method("prompt", request_timeout_seconds=100, max_input_chars=400000)
        assert delegate.calls[-1]["request_timeout_seconds"] == (3 if smaller_runtime else 10)
        assert delegate.calls[-1]["max_input_chars"] == (1000 if smaller_runtime else 1500)
        method("prompt", request_timeout_seconds=1, max_input_chars=500)
        assert delegate.calls[-1]["request_timeout_seconds"] == 1
        assert delegate.calls[-1]["max_input_chars"] == 500
    finally:
        client.close()


def test_agent_factory_rejects_nested_fallback_instead_of_losing_binding():
    client = model_factory.GatewayClient(_config(), environ={})
    try:
        with pytest.raises(ValueError, match="one configured fallback"):
            client.create_agent_factory(_binding(fallback=_binding("api", fallback=_binding("vision_api"))))
    finally:
        client.close()


def test_agent_fallback_policy_reserves_effective_smaller_runtime_output(monkeypatch):
    from gateway.agent_model.context import ContextWindowPolicy
    received = []
    monkeypatch.setattr("gateway.agent_model.factory.ArkFunctionCallingModel",
                        lambda *args, **options: received.append(options) or SimpleNamespace())
    config = _config()
    config.model_sources["api_fallback"] = ArkModelSourceConfig(api_key_env="API_KEY")
    binding = _binding(
        "api", max_output_tokens=32000, context_window_tokens=64000,
        context_safety_margin_tokens=1024, auto_compact_tokens=20000, compact_target_tokens=10000,
        fallback=_binding("api_fallback", max_output_tokens=1024, context_window_tokens=16000,
                          context_safety_margin_tokens=1024, auto_compact_tokens=10000,
                          compact_target_tokens=5000),
    )
    client = model_factory.GatewayClient(config, environ={"API_KEY": "a"})
    try:
        factory = client.create_agent_factory(binding)
        factory.build_loop(model="role-model", reasoning_effort="none", request_timeout_seconds=5,
                           max_output_tokens=512, max_input_chars=200000,
                           context_window_policy=ContextWindowPolicy(64000, 512, 4096, 48000, 24000))
        assert received[0]["max_output_tokens"] == received[0]["fallback_max_output_tokens"] == 512
        policy = received[0]["context_window_policy"]
        assert policy.context_window_tokens == 16000 and policy.output_reserve_tokens == 512
        assert policy.compact_target_tokens < policy.auto_compact_tokens < policy.maximum_input_tokens
    finally:
        client.close()


@pytest.mark.parametrize("builder", ["build_structured", "build_loop", "build_deep_agent"])
def test_closed_gateway_agent_factory_rejects_all_model_builders(builder):
    from gateway.agent_model.context import ContextWindowPolicy
    client = model_factory.GatewayClient(_config(), environ={})
    factory = client.create_agent_factory(_binding())
    factory.close()
    try:
        with pytest.raises(RuntimeError, match="factory is closed"):
            getattr(factory, builder)(
                model="role-model", reasoning_effort="none", request_timeout_seconds=5,
                max_output_tokens=2048, max_input_chars=200000,
                context_window_policy=ContextWindowPolicy(64000, 2048, 4096, 48000, 24000),
            )
        assert factory._codex_executor is None
    finally:
        client.close()


def test_model_factory_import_does_not_load_native_or_graph_runtime():
    script = """import sys
import gateway.model_factory
assert not any(name == 'langgraph' or name.startswith('langgraph.') for name in sys.modules)
assert 'gateway.codex.agent' not in sys.modules
assert 'gateway.claude_code.agent' not in sys.modules
"""
    completed = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=15)
    assert completed.returncode == 0, completed.stderr
