import json

import httpx
import pytest
from arkruntime import Ark
from langchain.agents.middleware.model_call_limit import ModelCallLimitExceededError
from langchain_core.tools import StructuredTool
from openai import OpenAI
from pydantic import BaseModel, ConfigDict

from gateway.agent_model import ModelInputTooLargeError, SDKQueryAgentModel


class QueryDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_context: str
    message: str
    selected_video_chunk_ids: list[str]


def _completion(name: str, arguments: dict, *, reason: str = "tool_calls") -> dict:
    return {
        "id": "completion-test",
        "model": "ark-test",
        "created": 1,
        "object": "chat.completion",
        "choices": [
            {
                "index": 0,
                "finish_reason": reason,
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": json.dumps(arguments),
                            },
                        }
                    ],
                },
            }
        ],
    }


def _tool(function):
    return StructuredTool.from_function(
        function,
        name="search_video_chunk_evidence",
        description="Retrieve actual video evidence for a query.",
    )


def test_sdk_query_runs_actual_tool_round_trip_over_ark_transport() -> None:
    requests = []
    searches = []

    def retrieve(query: str) -> dict:
        searches.append(query)
        return {
            "hits": [{"video_chunk_id": "chunk-real", "video_id": "video-real"}]
        }

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        assert request.url.path == "/api/v3/chat/completions"
        assert request.headers["Authorization"] == "Bearer test-ark-key"
        assert payload["max_completion_tokens"] == 400
        if len(requests) == 1:
            completion = _completion(
                "search_video_chunk_evidence", {"query": "摔杯子"}
            )
        else:
            tool_messages = [m for m in payload["messages"] if m["role"] == "tool"]
            assert "chunk-real" in tool_messages[0]["content"]
            assert "video-real" in tool_messages[0]["content"]
            completion = _completion(
                "QueryDraft",
                {
                    "plan_context": "确认摔杯子的动作",
                    "message": "找到了片段",
                    "selected_video_chunk_ids": ["chunk-real"],
                },
            )
        return httpx.Response(200, json=completion)

    client = Ark(
        api_key="test-ark-key",
        http_client=httpx.Client(transport=httpx.MockTransport(respond)),
    )
    model = SDKQueryAgentModel(
        "ark-test", request_timeout_seconds=3, max_output_tokens=400, client=client
    )
    result = model.run_query(
        "找到摔杯子的片段",
        tools=[_tool(retrieve)],
        result_type=QueryDraft,
        max_model_calls=2,
    )
    assert result.selected_video_chunk_ids == ["chunk-real"]
    assert searches == ["摔杯子"]
    assert len(requests) == 2
    client.close()


def test_sdk_query_checks_complete_messages_before_second_model_request() -> None:
    requests = []
    searches = []
    large_query = "下雨场景" + "x" * 3_000

    def retrieve(query: str) -> dict:
        searches.append(query)
        return {"query": query, "hits": []}

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json=_completion("search_video_chunk_evidence", {"query": large_query}),
        )

    client = Ark(
        api_key="test-ark-key",
        http_client=httpx.Client(transport=httpx.MockTransport(respond)),
    )
    model = SDKQueryAgentModel(
        "ark-test", request_timeout_seconds=3, max_output_tokens=400,
        max_input_chars=5_000, client=client,
    )

    with pytest.raises(ModelInputTooLargeError, match="message character budget"):
        model.run_query(
            "检索", tools=[_tool(retrieve)], result_type=QueryDraft, max_model_calls=3,
        )

    assert len(requests) == 1
    assert searches == [large_query]
    client.close()


def test_sdk_query_budget_stops_before_extra_model_request() -> None:
    requests = []

    def retrieve(query: str) -> dict:
        return {"query": query, "hits": []}

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json=_completion("search_video_chunk_evidence", {"query": "再找"}),
        )

    client = Ark(
        api_key="test-ark-key",
        http_client=httpx.Client(transport=httpx.MockTransport(respond)),
    )
    model = SDKQueryAgentModel(
        "ark-test", request_timeout_seconds=3, max_output_tokens=400, client=client
    )
    with pytest.raises(ModelCallLimitExceededError):
        model.run_query(
            "检索", tools=[_tool(retrieve)], result_type=QueryDraft, max_model_calls=1
        )
    assert len(requests) == 1
    client.close()


def test_sdk_query_propagates_tool_stop_without_retry() -> None:
    requests = []

    class ExecutionStopped(RuntimeError):
        pass

    def retrieve(query: str) -> dict:
        raise ExecutionStopped("execution cancelled")

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json=_completion("search_video_chunk_evidence", {"query": "找"}),
        )

    client = Ark(
        api_key="test-ark-key",
        http_client=httpx.Client(transport=httpx.MockTransport(respond)),
    )
    model = SDKQueryAgentModel(
        "ark-test", request_timeout_seconds=3, max_output_tokens=400, client=client
    )
    with pytest.raises(ExecutionStopped, match="cancelled"):
        model.run_query(
            "检索", tools=[_tool(retrieve)], result_type=QueryDraft, max_model_calls=3
        )
    assert len(requests) == 1
    client.close()


def test_sdk_query_does_not_accept_truncated_structured_tool_call() -> None:
    client = Ark(
        api_key="test-ark-key",
        http_client=httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    json=_completion(
                        "QueryDraft",
                        {
                            "plan_context": "partial",
                            "message": "partial",
                            "selected_video_chunk_ids": [],
                        },
                        reason="length",
                    ),
                )
            )
        ),
    )
    model = SDKQueryAgentModel(
        "ark-test", request_timeout_seconds=3, max_output_tokens=400, client=client
    )
    with pytest.raises(RuntimeError, match="length"):
        model.run_query("检索", tools=[], result_type=QueryDraft, max_model_calls=2)
    client.close()


def test_sdk_query_checks_control_after_model_before_running_tools() -> None:
    checks = 0
    searches = []

    def check_control():
        nonlocal checks
        checks += 1
        if checks == 3:
            raise RuntimeError("kick deadline exceeded")

    def retrieve(query: str) -> dict:
        searches.append(query)
        return {"hits": []}

    client = Ark(
        api_key="test-ark-key",
        http_client=httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    json=_completion("search_video_chunk_evidence", {"query": "找"}),
                )
            )
        ),
    )
    model = SDKQueryAgentModel(
        "ark-test", request_timeout_seconds=3, max_output_tokens=400, client=client
    )
    with pytest.raises(RuntimeError, match="deadline exceeded"):
        model.run_query(
            "检索",
            tools=[_tool(retrieve)],
            result_type=QueryDraft,
            max_model_calls=3,
            check_control=check_control,
        )
    assert checks == 3
    assert searches == []
    client.close()


def test_sdk_query_preserves_ark_ak_sk_authentication(monkeypatch) -> None:
    monkeypatch.delenv("ARK_API_KEY", raising=False)
    authorizations = []

    def respond(request):
        authorizations.append(request.headers["Authorization"])
        return httpx.Response(
            200,
            json=_completion(
                "QueryDraft",
                {
                    "plan_context": "检索完成",
                    "message": "无匹配片段",
                    "selected_video_chunk_ids": [],
                },
            ),
        )

    client = Ark(
        ak="test-ak",
        sk="test-sk",
        http_client=httpx.Client(transport=httpx.MockTransport(respond)),
    )
    monkeypatch.setattr(client, "_get_endpoint_sts_token", lambda *args: "test-sts")
    model = SDKQueryAgentModel(
        "ark-test", request_timeout_seconds=3, max_output_tokens=400, client=client
    )
    result = model.run_query("检索", tools=[], result_type=QueryDraft, max_model_calls=1)
    assert result.selected_video_chunk_ids == []
    assert authorizations == ["Bearer test-sts"]
    client.close()


def test_sdk_query_default_client_is_owned_per_invocation(monkeypatch) -> None:
    clients = []
    inputs = []

    def respond(request):
        inputs.append(json.loads(request.content)["messages"])
        return httpx.Response(
            200,
            json=_completion(
                "QueryDraft",
                {
                    "plan_context": "检索完成",
                    "message": "无匹配片段",
                    "selected_video_chunk_ids": [],
                },
            ),
        )

    def new_client(**options):
        assert options == {"timeout": 3, "max_retries": 0}
        transport = httpx.Client(transport=httpx.MockTransport(respond))
        clients.append(transport)
        return Ark(api_key="test-key", http_client=transport)

    monkeypatch.setattr(Ark, "volc", new_client)
    model = SDKQueryAgentModel(
        "ark-test", request_timeout_seconds=3, max_output_tokens=400
    )
    for prompt in ("会话一", "会话二"):
        model.run_query(prompt, tools=[], result_type=QueryDraft, max_model_calls=1)
    assert len(clients) == 2
    assert all(client.is_closed for client in clients)
    assert inputs == [
        [{"role": "user", "content": "会话一"}],
        [{"role": "user", "content": "会话二"}],
    ]


@pytest.mark.parametrize("provider", ["deepseek", "ollama", "llama_cpp"])
def test_sdk_query_openai_compatible_providers_use_their_token_parameter(provider):
    requests = []

    def respond(request):
        payload = json.loads(request.content)
        requests.append(payload)
        assert payload["max_tokens"] == 400
        assert "max_completion_tokens" not in payload
        return httpx.Response(
            200,
            json=_completion(
                "QueryDraft",
                {
                    "plan_context": "完成",
                    "message": "无片段",
                    "selected_video_chunk_ids": [],
                },
            ),
        )

    client = OpenAI(
        api_key="test-key",
        base_url="http://localhost:9000/v1",
        http_client=httpx.Client(transport=httpx.MockTransport(respond)),
    )
    model = SDKQueryAgentModel(
        "test-local",
        provider=provider,
        client=client,
        request_timeout_seconds=3,
        max_output_tokens=400,
    )
    result = model.run_query("检索", tools=[], result_type=QueryDraft, max_model_calls=1)
    assert result.selected_video_chunk_ids == []
    assert len(requests) == 1
    client.close()
