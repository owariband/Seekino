from __future__ import annotations

from fastapi.testclient import TestClient
import pytest

from tests.agent.chat.test_runtime import Chat, Evaluator, Query, Retrieval, _runtime
from tests.server.test_cloud_access import _conversation, _course, _guest, _login, cloud_api


def _install_runtime(api, *, searches_per_round=1):
    runtime, query, evaluator, chat, retrieval = _runtime(
        query=Query(selected=(), searches_per_round=searches_per_round),
        evaluator=Evaluator(), chat=Chat(), retrieval=Retrieval(hits=[]),
        limits_enabled=True,
    )
    api.services.agent._runtime = runtime
    api.services.agent._runtimes.append(runtime)
    return query, evaluator, chat, retrieval


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
    query, evaluator, chat, retrieval = _install_runtime(cloud_api)
    with TestClient(cloud_api.app) as client:
        message, budget = _send(
            client, cloud_api, kind=kind, series_id=series_id,
            loop_turns=loop_turns, tool_budget=8,
        )
    assert message["status"] == "COMPLETED", message
    assert query.budgets == evaluator.budgets == chat.model_call_budgets == [budget]
    assert len(retrieval.calls) == 1
    assert retrieval.calls[0][1] == cloud_api.services.auth.config.public_tenant_id


@pytest.mark.parametrize("loop_turns", [0, 100])
@pytest.mark.parametrize("kind", ["guest", "user"])
def test_cloud_loop_options_cannot_disable_actual_tool_budget(cloud_api, loop_turns, kind):
    series_id, _ = _course(cloud_api)
    query, evaluator, chat, retrieval = _install_runtime(cloud_api, searches_per_round=2)
    with TestClient(cloud_api.app) as client:
        message, budget = _send(
            client, cloud_api, kind=kind, series_id=series_id,
            loop_turns=loop_turns, tool_budget=1, expected_status=502,
        )
    assert message["status"] == "FAILED", message
    assert query.budgets == [budget]
    assert len(retrieval.calls) == 1
    assert evaluator.budgets == chat.model_call_budgets == []
