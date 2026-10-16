from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
import httpx
from arkruntime import Ark
from openai import OpenAI
from pydantic import BaseModel

from agent.chat.common.tool import build_agent_tool
from agent.chat.common.tool_result import JsonToolResult
from gateway.agent_model.context import measure_context
from gateway.ark.agent import ArkFunctionCallingFallback, ArkFunctionCallingModel, ArkStructuredFallback, ArkStructuredToolAgentModel
from gateway.claude_code.tools import ToolInvocationState, _build_sdk_tool
from gateway.codex.agent import CodexAgentModel
from gateway.codex.protocol import CodexToolBridge
from gateway.media import ToolImageArtifact, prepare_input_messages, require_vision_input
from gateway.openai.agent import OpenAIAgentModel
from gateway.openai.structured import OpenAIStructuredToolAgentModel


class Arguments(BaseModel):
    query: str


def _tool(tmp_path, *, image_count=1):
    images = []
    for index in range(image_count):
        path = tmp_path / ("frame.png" if index == 0 else f"frame-{index}.png")
        path.write_bytes(b"\x89PNG\r\n\x1a\n" + bytes([97 + index]) * 20000)
        images.append((path, f"Frame at {1234 + index} ms"))
    artifact = ToolImageArtifact.from_paths(images)
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
def test_provider_fallback_preserves_images_or_rejects_unsupported_input(
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
    if not fallback_vision:
        with pytest.raises(ValueError, match="fallback.*does not support image input"):
            model.run_agent_loop(
                "Explain lambda", tools=[_tool(tmp_path)], max_model_calls=4,
                handle_text=answers.append,
            )
        assert all(request["model"] != "fallback" for request in fallback.requests)
        assert answers == []
        return
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
        assert any(isinstance(item.get("content"), list) for item in messages)
    assert answers == ["Fallback answer."]


@pytest.mark.parametrize("tail", [[], [{"role": "assistant", "content": "Earlier answer"}]])
def test_prepare_input_messages_preserves_history_and_appends_current_images(tail):
    history = [{"role": "user", "content": [{"type": "text", "text": "Earlier question"}],
                "imageUrls": ["data:image/png;base64,AAAA"]}, *tail]
    before = deepcopy(history)
    messages = prepare_input_messages("Current instruction", initial_messages=history,
                                      image_urls=["data:image/png;base64,BBBB"])
    assert history == before
    assert "imageUrls" not in messages[0]
    assert messages[0]["content"][1]["image_url"]["url"] == "data:image/png;base64,AAAA"
    assert messages[-1]["role"] == "user"
    assert messages[-1]["content"][-1]["image_url"]["url"] == "data:image/png;base64,BBBB"
    if tail:
        assert messages[-1]["content"][0]["text"] == "Current instruction"
    else:
        assert len(messages) == 1
    messages[0]["content"][0]["text"] = "Changed copy"
    assert history == before


def test_prepare_input_messages_keeps_text_defaults_and_handles_no_user_context():
    assert prepare_input_messages("Prompt") == [{"role": "user", "content": "Prompt"}]
    context = [{"role": "system", "content": "Instructions"}]
    assert prepare_input_messages("Ignored", initial_messages=context) == context
    messages = prepare_input_messages("Current", initial_messages=context, image_urls=["https://example.test/image.png"])
    assert messages[-1]["role"] == "user" and messages[-1]["content"][0]["text"] == "Current"
    with pytest.raises(ValueError, match="must not be empty"):
        prepare_input_messages("Prompt", initial_messages=[])


@pytest.mark.parametrize("provider", ["openai", "ark"])
@pytest.mark.parametrize("vision", [False, True])
def test_agent_loop_current_and_historical_images_require_explicit_support(tmp_path, provider, vision):
    completions = Completions()
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    common = dict(max_output_tokens=1024, request_timeout_seconds=30, client=client,
                  supports_vision_input=vision)
    model = (OpenAIAgentModel("configured-model", base_url="https://example.test", api_key="test", **common)
             if provider == "openai" else ArkFunctionCallingModel("configured-model", fallback_model=None, **common))
    history = [{"role": "user", "content": "Earlier", "imageUrls": ["data:image/png;base64,AAAA"]},
               {"role": "assistant", "content": "Earlier answer"}]
    before = deepcopy(history)
    options = dict(tools=[_tool(tmp_path)], max_model_calls=3, handle_text=lambda _text: None,
                   initial_messages=history, image_urls=["data:image/png;base64,BBBB"])
    if not vision:
        with pytest.raises(ValueError, match="configured-model.*does not support image input"):
            model.run_agent_loop("Current", **options)
        assert completions.requests == []
    else:
        model.run_agent_loop("Current", **options)
        messages = completions.requests[0]["messages"]
        assert messages[0]["content"][-1]["image_url"]["url"] == "data:image/png;base64,AAAA"
        assert messages[-1]["content"][-1]["image_url"]["url"] == "data:image/png;base64,BBBB"
    assert history == before


def test_require_vision_input_checks_blocks_instead_of_image_words_or_data_urls():
    require_vision_input([{"role": "user", "content": "Discuss image data:image/png;base64,AAAA"}],
                         supports_vision_input=False, model_name="text")
    for block in ({"type": "input_image", "image_url": "https://example.test/frame.png"},
                  {"type": "image", "source": {"type": "url", "url": "https://example.test/frame.png"}}):
        with pytest.raises(ValueError, match="text.*does not support image input"):
            require_vision_input([{"role": "user", "content": [block]}], supports_vision_input=False, model_name="text")


def _structured_completion(calls):
    return {"id": "structured", "model": "vision", "created": 1, "object": "chat.completion",
            "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
                "role": "assistant", "content": "", "tool_calls": [
                    {"id": identity, "type": "function", "function": {
                        "name": name, "arguments": json.dumps(arguments),
                    }} for identity, name, arguments in calls
                ],
            }}]}


@pytest.mark.parametrize("provider", ["openai", "ark"])
def test_structured_tool_images_survive_batches_and_state_reentry(tmp_path, provider):
    class ImageReview(BaseModel):
        answer: str

    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        calls = ([("frame-1", "current_video", {"query": "first"}),
                  ("frame-2", "current_video", {"query": "second"})] if len(requests) == 1
                 else [("frame-3", "current_video", {"query": "third"})] if len(requests) == 2
                 else [("result", "ImageReview", {"answer": "Reviewed"})])
        return httpx.Response(200, json=_structured_completion(calls))

    http = httpx.Client(transport=httpx.MockTransport(respond))
    client = (Ark(api_key="test", http_client=http) if provider == "ark"
              else OpenAI(api_key="test", base_url="https://example.test/v1", http_client=http))
    model_type = ArkStructuredToolAgentModel if provider == "ark" else OpenAIStructuredToolAgentModel
    model = model_type("vision", request_timeout_seconds=3, max_output_tokens=400, client=client,
                       supports_vision_input=True, max_input_chars=4000)
    try:
        assert model.run_structured("Review frames", tools=[_tool(tmp_path)], result_type=ImageReview,
                                    max_model_calls=3).answer == "Reviewed"
        assert len(requests) == 3
        for request, count in zip(requests[1:], [2, 3]):
            messages = request["messages"]
            images = [block for message in messages if isinstance(message.get("content"), list)
                      for block in message["content"] if block.get("type") == "image_url"]
            assert len(images) == count
            assert all(image["image_url"]["url"].startswith("data:image/png;base64,") for image in images)
            assert messages[-1]["role"] == "user" and messages[-2]["role"] == "tool"
            assert any("The formula uses lambda" in message.get("content", "")
                       for message in messages if message["role"] == "tool")
    finally:
        client.close()


@pytest.mark.parametrize("fallback_vision", [False, True])
def test_structured_tool_images_reach_fallback_or_fail_explicitly(tmp_path, fallback_vision):
    class ImageReview(BaseModel):
        answer: str

    requests = []

    def respond(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if len(requests) == 1:
            return httpx.Response(200, json=_structured_completion([("frames", "current_video", {"query": "first"})]))
        if payload["model"] == "primary":
            return httpx.Response(500, json={"error": {"message": "Unavailable", "type": "server_error"}})
        return httpx.Response(200, json=_structured_completion([("result", "ImageReview", {"answer": "Reviewed"})]))

    primary = OpenAI(api_key="test", base_url="https://example.test/v1", max_retries=0,
                     http_client=httpx.Client(transport=httpx.MockTransport(respond)))
    fallback = Ark(api_key="test", http_client=httpx.Client(transport=httpx.MockTransport(respond)))
    model = OpenAIStructuredToolAgentModel(
        "primary", request_timeout_seconds=3, max_output_tokens=400, client=primary,
        supports_vision_input=True, fallback_adapter=ArkStructuredFallback(
            "fallback", max_output_tokens=400, request_timeout_seconds=3, client=fallback,
            supports_vision_input=fallback_vision,
        ),
    )
    try:
        options = dict(tools=[_tool(tmp_path)], result_type=ImageReview, max_model_calls=3)
        if fallback_vision:
            assert model.run_structured("Review frames", **options).answer == "Reviewed"
            assert [request["model"] for request in requests] == ["primary", "primary", "fallback"]
            assert requests[-1]["messages"][-1]["content"][-1]["type"] == "image_url"
        else:
            with pytest.raises(ValueError, match="fallback.*does not support image input"):
                model.run_structured("Review frames", **options)
            assert [request["model"] for request in requests] == ["primary", "primary"]
    finally:
        primary.close()
        fallback.close()


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


def test_structured_codex_tool_turn_receives_four_native_images(tmp_path):
    tool = _tool(tmp_path, image_count=4)
    receipts = []
    closed = []

    class Result(BaseModel):
        answer: str

    class Session:
        def __init__(self, **options):
            self.bridge = CodexToolBridge(options["tools"], check_control=options["check_control"])
            self.tool_call_count = 0
            self.successful_tool_call_count = 0

        def run_turn(self, prompt, *, output_schema=None):
            receipt = self.bridge.handle("item/tool/call", {
                "callId": "frames", "tool": tool.name, "arguments": {"query": "show frames"},
            })
            receipts.append((output_schema, receipt))
            self.tool_call_count = self.successful_tool_call_count = 1
            return SimpleNamespace(final_response='{"answer":"reviewed four frames"}', usage=None)

        def close(self):
            closed.append(True)

    model = CodexAgentModel(
        model="configured-vision-model", request_timeout_seconds=2,
        max_output_tokens=1024, max_input_chars=10_000, max_concurrency=1,
        session_factory=Session,
    )
    model.supports_vision_input = True
    try:
        result = model.run_structured(
            "Review the candidate frames", tools=[tool], result_type=Result, max_model_calls=3,
        )
    finally:
        model.close()

    assert result.answer == "reviewed four frames"
    assert closed == [True]
    assert len(receipts) == 1
    schema, receipt = receipts[0]
    assert schema["properties"]["answer"]["type"] == "string"
    assert receipt["success"] is True
    images = [item for item in receipt["contentItems"] if item["type"] == "inputImage"]
    captions = [item["text"] for item in receipt["contentItems"] if item["type"] == "inputText"]
    assert len(images) == 4
    assert len({item["imageUrl"] for item in images}) == 4
    assert all(item["imageUrl"].startswith("data:image/png;base64,") for item in images)
    assert captions[1:] == [f"Frame at {1234 + index} ms" for index in range(4)]


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
