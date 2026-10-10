from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from agent.chat.common.tool import build_agent_tool
from agent.chat.common.tool_result import JsonToolResult
from gateway.agent_model.context import measure_context
from gateway.ark.agent import ArkFunctionCallingFallback, ArkFunctionCallingModel
from gateway.claude_code.tools import ToolInvocationState, _build_sdk_tool
from gateway.codex.protocol import CodexToolBridge
from gateway.media import ToolImageArtifact, text_only_messages
from gateway.openai.agent import OpenAIAgentModel


class Arguments(BaseModel):
    query: str


def _tool(tmp_path):
    path = tmp_path / "frame.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"a" * 20000)
    artifact = ToolImageArtifact.from_paths([(path, "Frame at 1234 ms")])
    return build_agent_tool(
        func=lambda query: JsonToolResult(
            success=True, payload={"summary": "The formula uses lambda."}, artifact=artifact,
        ), name="current_video", description="Read the current source-video evidence.",
        args_schema=Arguments, maximum_result_bytes=1024,
    ).as_langchain_tool()


def _response(*, calls=None, content=None):
    return SimpleNamespace(id="test", usage=None, choices=[SimpleNamespace(
        finish_reason="tool_calls" if calls else "stop",
        message=SimpleNamespace(role="assistant", content=content, tool_calls=calls),
    )])


class Completions:
    def __init__(self, tool_count=1):
        self.requests = []
        self.tool_count = tool_count

    def create(self, **kwargs):
        self.requests.append(deepcopy(kwargs))
        if len(self.requests) == 1:
            return _response(calls=[SimpleNamespace(
                id=f"current-{index}", type="function",
                function=SimpleNamespace(name="current_video", arguments='{"query":"lambda"}'),
            ) for index in range(1, self.tool_count + 1)])
        return _response(content="Lambda explanation.")


@pytest.mark.parametrize("provider", ["openai", "ark"])
@pytest.mark.parametrize("vision", [False, True])
@pytest.mark.parametrize("tool_count", [1, 2])
def test_provider_receives_actual_images_only_when_supported(
    tmp_path, provider, vision, tool_count,
):
    completions = Completions(tool_count)
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    if provider == "openai":
        model = OpenAIAgentModel(
            "configured-model", base_url="https://example.org", api_key="test",
            max_output_tokens=1024, request_timeout_seconds=30, client=client,
            supports_vision_input=vision,
        )
    else:
        model = ArkFunctionCallingModel(
            "configured-model", fallback_model=None, max_output_tokens=1024,
            request_timeout_seconds=30, client=client, supports_vision_input=vision,
        )
    answers = []
    model.run_agent_loop(
        "Explain lambda", tools=[_tool(tmp_path)], max_model_calls=3,
        handle_text=answers.append, max_input_chars=2000,
    )
    messages = completions.requests[1]["messages"]
    blocks = [block for item in messages if isinstance(item.get("content"), list)
              for block in item["content"]]
    images = [block for block in blocks if block.get("type") == "image_url"]
    assert bool(images) is vision
    tool_messages = [item for item in messages if item["role"] == "tool"]
    assert [item["tool_call_id"] for item in tool_messages] == [
        f"current-{index}" for index in range(1, tool_count + 1)
    ]
    if vision:
        assert len(images) == tool_count
        assert images[0]["image_url"]["url"].startswith("data:image/png;base64,")
        assert messages[-1]["role"] == "user"
        assert messages[-2]["role"] == "tool"
    assert any("The formula uses lambda" in str(item["content"]) for item in messages)
    assert answers == ["Lambda explanation."]


@pytest.mark.parametrize("provider", ["openai", "ark"])
@pytest.mark.parametrize("fallback_vision", [False, True])
def test_provider_fallback_projects_images_for_its_own_capability(
    tmp_path, provider, fallback_vision,
):
    class Responses:
        def __init__(self, values):
            self.values = iter(values)
            self.requests = []

        def create(self, **kwargs):
            self.requests.append(deepcopy(kwargs))
            response = next(self.values)
            if isinstance(response, Exception):
                raise response
            return response

    def tool_call(call_id):
        return _response(calls=[SimpleNamespace(
            id=call_id, type="function", function=SimpleNamespace(
                name="current_video", arguments='{"query":"lambda"}',
            ),
        )])

    primary_responses = [tool_call("primary-call"), _response(content=None)]
    fallback_responses = [tool_call("fallback-call"), _response(content="Fallback answer.")]
    if provider == "openai":
        primary = Responses(primary_responses)
        fallback = Responses(fallback_responses)
        model = OpenAIAgentModel(
            "primary-vision", base_url="https://example.org", api_key="test",
            max_output_tokens=1024, request_timeout_seconds=30,
            client=SimpleNamespace(chat=SimpleNamespace(completions=primary)),
            supports_vision_input=True,
            fallback_adapter=ArkFunctionCallingFallback(
                "fallback", max_output_tokens=1024, request_timeout_seconds=30,
                client=SimpleNamespace(chat=SimpleNamespace(completions=fallback)),
                supports_vision_input=fallback_vision,
            ),
        )
    else:
        primary = fallback = Responses(primary_responses + fallback_responses)
        model = ArkFunctionCallingModel(
            "primary-vision", fallback_model="fallback", max_output_tokens=1024,
            request_timeout_seconds=30,
            client=SimpleNamespace(chat=SimpleNamespace(completions=primary)),
            supports_vision_input=True, fallback_supports_vision_input=fallback_vision,
        )
    answers = []
    model.run_agent_loop(
        "Explain lambda", tools=[_tool(tmp_path)], max_model_calls=4,
        handle_text=answers.append,
    )
    assert any(isinstance(message.get("content"), list)
               for message in primary.requests[1]["messages"])
    fallback_requests = [request for request in fallback.requests
                         if request["model"] == "fallback"]
    assert len(fallback_requests) == 2
    for request in fallback_requests:
        messages = request["messages"]
        assert any("The formula uses lambda" in str(item["content"]) for item in messages)
        assert any(isinstance(item.get("content"), list) for item in messages) is fallback_vision
        if not fallback_vision:
            assert "Images are unavailable" in json.dumps(messages)
            assert "base64" not in json.dumps(messages)
    assert answers == ["Fallback answer."]


def test_sdk_bridges_preserve_native_image_payloads(tmp_path):
    tool = _tool(tmp_path)
    codex = CodexToolBridge([tool]).handle("item/tool/call", {
        "callId": "test-1", "tool": tool.name, "arguments": {"query": "lambda"},
    })
    assert codex["success"]
    assert codex["contentItems"][1] == {"type": "inputText", "text": "Frame at 1234 ms"}
    assert codex["contentItems"][2]["type"] == "inputImage"
    assert codex["contentItems"][2]["imageUrl"].startswith("data:image/png;base64,")
    sdk_tool = _build_sdk_tool(tool, ToolInvocationState())
    claude = asyncio.run(sdk_tool.handler({"query": "lambda"}))
    assert claude["content"][1] == {"type": "text", "text": "Frame at 1234 ms"}
    assert claude["content"][2]["type"] == "image"
    assert claude["content"][2]["mimeType"] == "image/png"
    assert claude["content"][2]["data"] == codex["contentItems"][2]["imageUrl"].split(",", 1)[1]


def test_text_fallback_keeps_evidence_and_explicit_visual_limitation(tmp_path):
    artifact = _tool(tmp_path).invoke({
        "type": "tool_call", "id": "t1", "name": "current_video", "args": {"query": "lambda"},
    }).artifact
    messages = [
        {"role": "tool", "tool_call_id": "t1", "content": "The formula uses lambda."},
        {"role": "user", "content": artifact.openai_content()},
    ]
    fallback = text_only_messages(messages)
    assert fallback[0] == messages[0]
    assert "Frame at 1234 ms" in fallback[1]["content"]
    assert "Images are unavailable" in fallback[1]["content"]
    assert "base64" not in json.dumps(fallback)
    assert isinstance(messages[1]["content"], list)


def test_image_budgets_do_not_count_base64_as_text(tmp_path):
    tool = _tool(tmp_path)
    output = tool.invoke({
        "type": "tool_call", "id": "t1", "name": tool.name, "args": {"query": "lambda"},
    })
    assert len(output.content.encode()) < 1024
    measurement = measure_context([{"role": "user", "content": output.artifact.openai_content()}], [])
    assert measurement.characters < 300
    assert 2048 <= measurement.estimated_tokens < 2300
    path = tmp_path / "frame.png"
    with pytest.raises(ValueError, match="count"):
        ToolImageArtifact.from_paths([(path, "")] * 5)
    with pytest.raises(ValueError, match="bytes"):
        ToolImageArtifact.from_paths([(path, "")], maximum_bytes=100)
