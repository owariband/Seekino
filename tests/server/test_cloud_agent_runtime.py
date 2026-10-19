from __future__ import annotations

import json
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from agent.chat.common.errors import AgentModelInvocationError
from agent.chat.supervisor import SupervisorRuntime
from agent.chat.video_search.runtime import AgentRuntime
from tests.agent.chat.test_runtime import Query, Retrieval
from tests.server.test_cloud_access import _conversation, _course, _guest, _login, cloud_api


class _MainModel:
    model_name = "cloud-main-test"

    def __init__(self):
        self.budgets = []

    def run_agent(self, _prompt, *, tools, user_message, max_model_calls, handle_text, **_):
        self.budgets.append(max_model_calls)
        available = {tool.name: tool for tool in tools}
        reports = json.loads(available["query_video"].invoke({"query": user_message}))["reports"]
        if any("error" in report for report in reports):
            raise AgentModelInvocationError("Query could not finish within its budget")
        handle_text("没有足够证据。")

    def confirm_turn(self, *_):
        pass

    def discard_turn(self, *_):
        pass

    def delete_thread(self, *_):
        pass


def _install_runtime(api, *, searches_per_round=1):
    query = Query(selected=(), searches_per_round=searches_per_round)
    retrieval = Retrieval(hits=[])
    main = _MainModel()
    runtime = SupervisorRuntime(
        AgentRuntime(query, retrieval.search, retrieval_channel="lexical", limits_enabled=True),
        main, SimpleNamespace(), max_web_results=5, web_timeout_seconds=12,
        execution_timeout_seconds=60, max_tool_result_bytes=16_384,
        max_prompt_chars=200_000, limits_enabled=True,
    )
    api.services.agent._runtime = runtime
    api.services.agent._runtimes.append(runtime)
    return query, main, retrieval


def _send(client, api, *, kind, series_id, loop_turns, tool_budget, expected_status=200):
    identity = _guest(client) if kind == "guest" else _login(api, client, "runtime-limits@example.org")
    tenant_id = identity["publicTenantId"] if kind == "guest" else identity["tenantId"]
    conversation = _conversation(client, identity, tenant_id=tenant_id)
    model_budget = api.services.auth.config.guest_max_model_calls if kind == "guest" else 2
    response = client.post(
        "/api/agent/messages/send", params={"conversationId": conversation},
        headers={"Idempotency-Key": "real-runtime", "X-CSRF-Token": identity["csrfToken"]},
        json={
            "tenantId": tenant_id, "sourceTenantId": identity["publicTenantId"],
            "content": "找公开课程片段", "seriesIds": [series_id],
            "maxLoopTurns": loop_turns,
            "maxModelCallsPerTurn": 999 if kind == "guest" else model_budget,
            "maxToolCallsPerTurn": tool_budget,
        },
    )
    assert response.status_code == expected_status, response.text
    if expected_status != 200:
        assert response.json()["code"] == "AGENT_MODEL_ERROR"
        response = client.get(
            "/api/agent/messages/get",
            params={"conversationId": conversation, "tenantId": tenant_id, "idempotencyKey": "real-runtime"},
        )
        assert response.status_code == 200, response.text
    return response.json()["assistantMessage"], model_budget


@pytest.mark.parametrize("loop_turns", [0, 100])
@pytest.mark.parametrize("kind", ["guest", "user"])
def test_cloud_loop_options_preserve_actual_model_budgets(cloud_api, loop_turns, kind):
    series_id, _ = _course(cloud_api)
    query, main, retrieval = _install_runtime(cloud_api)
    with TestClient(cloud_api.app) as client:
        message, budget = _send(
            client, cloud_api, kind=kind, series_id=series_id,
            loop_turns=loop_turns, tool_budget=8,
        )
    assert message["status"] == "COMPLETED", message
    assert query.budgets == main.budgets == [budget]
    assert len(retrieval.calls) == 1
    assert retrieval.calls[0][1] == cloud_api.services.auth.config.public_tenant_id


@pytest.mark.parametrize("loop_turns", [0, 100])
@pytest.mark.parametrize("kind", ["guest", "user"])
def test_cloud_loop_options_cannot_disable_actual_tool_budget(cloud_api, loop_turns, kind):
    series_id, _ = _course(cloud_api)
    query, main, retrieval = _install_runtime(cloud_api, searches_per_round=2)
    with TestClient(cloud_api.app) as client:
        message, budget = _send(
            client, cloud_api, kind=kind, series_id=series_id,
            loop_turns=loop_turns, tool_budget=2, expected_status=502,
        )
    assert message["status"] == "FAILED", message
    assert query.budgets == [budget]
    assert len(retrieval.calls) == 1
    assert main.budgets == [budget]
