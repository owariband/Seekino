from __future__ import annotations

import json
from typing import Literal
from unittest.mock import Mock

import pytest
from pydantic import BaseModel, Field, model_validator

from agent.utils.tool import build_agent_tool
from agent.utils.tool_result import JsonToolResult


class SearchOptions(BaseModel):
    channel: Literal["lexical", "semantic"]
    top_k: int = Field(ge=1, le=20, strict=True)


class SearchArguments(BaseModel):
    query: str
    options: list[SearchOptions]


@pytest.mark.parametrize("arguments,details", [
    ({"query": "private query"}, ["options (missing): Field required"]),
    ({"query": "private query", "options": [{"channel": "private channel", "top_k": "private number"}]}, [
        "options.0.channel (literal_error): Input should be 'lexical' or 'semantic'",
        "options.0.top_k (int_type): Input should be a valid integer",
    ]),
    ({"query": "private query", "options": [{"channel": "lexical", "top_k": 0}]}, [
        "options.0.top_k (greater_than_equal): Input should be greater than or equal to 1",
    ]),
])
def test_validation_feedback_explains_corrections_without_echoing_arguments(arguments, details):
    execute = Mock(return_value=JsonToolResult(success=True, payload={"items": []}))
    tool = build_agent_tool(
        func=execute, name="search", description="Search with explicit options.",
        args_schema=SearchArguments, maximum_result_bytes=4096,
    )

    result = tool.invoke({"type": "tool_call", "id": "invalid", "name": tool.name, "args": arguments})

    assert result.status == "error"
    assert result.tool_call_id == "invalid"
    error = json.loads(result.content)["error"]
    assert error["code"] == "INVALID_ARGUMENTS"
    assert error["retryable"] is True
    for detail in details:
        assert detail in error["message"]
    assert "private" not in result.content
    assert set(error) == {"code", "message", "retryable", "retry_hint"}
    execute.assert_not_called()

    corrected = tool.invoke({"query": "corrected", "options": [{"channel": "lexical", "top_k": 1}]})
    assert json.loads(corrected) == {"items": []}
    execute.assert_called_once()


def test_validation_feedback_keeps_model_validator_reason_at_arguments():
    class Arguments(BaseModel):
        query: str
        channel_weights: dict[str, float] | None = None

        @model_validator(mode="after")
        def require_weights(self):
            if self.channel_weights is None:
                raise ValueError("channel_weights is required for a query")
            return self

    execute = Mock()
    tool = build_agent_tool(
        func=execute, name="search", description="Search with channel weights.",
        args_schema=Arguments, maximum_result_bytes=4096,
    )

    result = tool.invoke({"query": "private query"})

    assert "arguments (value_error): Value error, channel_weights is required for a query" in result
    assert "private query" not in result
    execute.assert_not_called()
