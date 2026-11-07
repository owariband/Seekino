import json

import httpx
import pytest
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from pydantic import SecretStr

from gateway.personal_endpoint import PersonalModelTarget
from gateway.personal_models import _PersonalChatModel, PersonalModelRequestError


@tool
def lookup_video(query: str) -> str:
    """Find a video by text."""
    return query


@pytest.mark.parametrize(("protocol", "suffix", "auth"), [
    ("openai_chat", "/chat/completions", "authorization"), ("openai_responses", "/responses", "authorization"),
    ("anthropic_messages", "/messages", "x-api-key"), ("gemini", ":generateContent", "x-goog-api-key"),
])
def test_custom_model_sends_selected_protocol_and_continues_tool_history(protocol, suffix, auth):
    requests = []
    model = "team/custom" if protocol != "gemini" else "my-gemini"

    def provider(request):
        payload = json.loads(request.content)
        requests.append(payload)
        assert request.url.path.endswith(suffix)
        assert request.url.host == "selected.example" and request.headers[auth].removeprefix("Bearer ") == "personal-key"
        assert model in str(request.url) if protocol == "gemini" else payload["model"] == model
        assert "lookup_video" in json.dumps(payload["tools"])
        first = len(requests) == 1
        if not first:
            body = json.dumps(payload)
            assert "fixture result" in body and "lookup_video" in body
            assert {"openai_chat": '"tool_call_id"', "openai_responses": '"function_call_output"',
                    "anthropic_messages": '"tool_result"', "gemini": '"functionResponse"'}[protocol] in body
        if protocol == "openai_chat":
            msg = {"role": "assistant", "content": "received" if not first else None}
            if first:
                msg["tool_calls"] = [{"id": "call_1", "type": "function", "function": {"name": "lookup_video", "arguments": '{"query":"q"}'}}]
            reply = {"choices": [{"message": msg}]}
        elif protocol == "openai_responses":
            item = ({"type": "function_call", "id": "fc_1", "call_id": "call_1", "name": "lookup_video", "arguments": '{"query":"q"}'} if first else
                    {"type": "message", "id": "msg_1", "status": "completed", "role": "assistant",
                     "content": [{"type": "output_text", "text": "received", "annotations": []}]})
            reply = dict(id="resp_1", object="response", output=[item], status="completed", model=model)
        elif protocol == "anthropic_messages":
            reply = {"content": [{"type": "tool_use", "id": "call_1", "name": "lookup_video", "input": {"query": "q"}} if first else
                                 {"type": "text", "text": "received"}]}
        else:
            reply = {"candidates": [{"content": {"role": "model", "parts": [
                {"functionCall": {"name": "lookup_video", "args": {"query": "q"}}} if first else {"text": "received"}]},
                "finishReason": "STOP"}]}
        return httpx.Response(200, json=reply)

    target = PersonalModelTarget(protocol, "https://selected.example/v1", model, SecretStr("personal-key"), True)
    with httpx.Client(transport=httpx.MockTransport(provider)) as client:
        custom = _PersonalChatModel(target, client, 200)
        history = [SystemMessage("Use your tools."), HumanMessage("Find the video.")]
        first = custom._generate(history, tools=[lookup_video]).generations[0].message
        call, = first.tool_calls
        assert call["name"] == "lookup_video" and call["args"] == {"query": "q"}
        second = custom._generate([*history, first, ToolMessage("fixture result", tool_call_id=call["id"], name="lookup_video")],
                                  tools=[lookup_video]).generations[0].message
        assert second.text == "received"


def test_upstream_cannot_echo_credentials_through_model_errors():
    target = PersonalModelTarget("openai_chat", "https://selected.example/v1", "any", SecretStr("personal-key"), False)
    with httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(401, text="echo personal-key"))) as client:
        with pytest.raises(PersonalModelRequestError, match="401") as error:
            _PersonalChatModel(target, client, 64)._generate([HumanMessage("hello")])
        assert "personal-key" not in str(error.value)
