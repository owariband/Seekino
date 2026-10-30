"""Parse output limits must reach both execution paths without changing Chat."""

from pathlib import Path

import pytest
from pydantic import BaseModel

from agent.utils.tool import build_agent_tool
from engine.common.config import VideoParseRuntimeConfig
from gateway.config import load_config
from gateway.model_factory import GatewayClient
from gateway.multimodal import TextInput
from server.config.media import ImageUploadConfig
from server.config.parse_profile import ParseConfig
from server.config.upload import UploadServiceConfig
from server.service.video_parse import VideoParseRuntimeService


class Completion(BaseModel):
    complete: bool


class ReadArguments(BaseModel):
    reference: str


def _defaults():
    gateway = load_config(Path(__file__).resolve().parents[2] / "config.yaml")
    profile = ParseConfig.from_defaults(
        runtime=VideoParseRuntimeConfig(), gateway=gateway,
        upload=UploadServiceConfig(), images=ImageUploadConfig(),
        embedding_model=None,
        embedding_dimensions=gateway.resolve_embedding_binding(environ={}).dimensions,
        embedding_fps=None,
    )
    return gateway, profile.to_video_parse_v2()


def test_default_parse_output_budget_is_independent_of_chat_and_observation_models():
    gateway, task = _defaults()
    assert task.models.understanding.max_output_tokens == 30_000
    assert task.models.understanding.fallback.max_output_tokens == 30_000
    assert task.models.refine is None  # Refine inherits the frozen Understanding binding.
    assert gateway.resolve_model_binding("agent", environ={}).max_output_tokens == 8192
    assert gateway.resolve_model_binding("supervisor", environ={}).max_output_tokens == 8192
    assert task.models.ocr.max_output_tokens == 8192
    assert task.models.asr.max_output_tokens is None


@pytest.mark.parametrize("stage", ["understanding", "refine"])
@pytest.mark.parametrize("stored_budget", [30_000, 8192])
def test_parse_output_budget_survives_snapshot_and_model_invocation(monkeypatch, stage, stored_budget):
    gateway, task = _defaults()
    payload = task.model_dump(mode="json")
    payload["models"]["understanding"]["max_output_tokens"] = stored_budget
    payload["models"]["understanding"]["fallback"]["max_output_tokens"] = stored_budget
    restored = type(task).model_validate(payload)
    binding = (restored.models.refine or restored.models.understanding) if stage == "refine" else restored.models.understanding
    constructions, requests = [], []

    class Adapter:
        def __init__(self, model, **options):
            self.model_name = model
            self.max_output_tokens = options["max_output_tokens"]
            constructions.append(options)

        def generate(self, _inputs, **options):
            assert stage == "understanding"
            assert not options["tools"]
            requests.append(options)
            return Completion(complete=True)

        def close(self):
            pass

    def refine_generate(adapter, _inputs, **options):
        assert stage == "refine"
        assert adapter.max_output_tokens == stored_budget
        assert options["tools"]
        requests.append(options)
        return Completion(complete=True)

    monkeypatch.setattr("gateway.ark.multimodal.ArkMultimodalModel", Adapter)
    monkeypatch.setattr("gateway.openai.multimodal.DeepSeekMultimodalModel", Adapter)
    monkeypatch.setattr("gateway.agent_model.deepagents_multimodal.generate_with_deepagents", refine_generate)
    runtime = object.__new__(VideoParseRuntimeService)
    runtime._gateway = GatewayClient(gateway, environ={"ARK_API_KEY": "offline", "DEEPSEEK_API_KEY": "offline"})
    tools = [build_agent_tool(
        func=lambda reference: reference, name="get_context_canvas", description="Read context.",
        args_schema=ReadArguments, maximum_result_bytes=1024,
    ).as_langchain_tool()] if stage == "refine" else []
    try:
        result = runtime._invoke_multimodal(
            binding, [TextInput(stage)], schema_name="completion", result_type=Completion,
            tools=tools, tool_harness="deepagents" if stage == "refine" else "legacy",
        )
    finally:
        runtime._gateway.close()

    assert result.complete
    assert len(constructions) == 2  # Primary and configured fallback retain the same stored limit.
    assert all(item["max_output_tokens"] == stored_budget for item in constructions)
    assert len(requests) == 1
    policy = requests[0]["context_window_policy"]
    assert policy.output_reserve_tokens == stored_budget
    assert policy.compact_target_tokens < policy.auto_compact_tokens < policy.maximum_input_tokens
