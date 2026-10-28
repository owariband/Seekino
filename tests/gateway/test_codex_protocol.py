from __future__ import annotations

from typing import Any

import pytest
from langchain_core.messages import ToolMessage
from langchain_core.tools import StructuredTool
from pydantic import BaseModel

from gateway.codex.client import (
    CodexModelCallBudget,
    CodexModelCallLimitError,
)
from gateway.codex.protocol import (
    CodexToolBridge,
    dynamic_tool_specs,
    strict_output_schema,
)


class NestedInput(BaseModel):
    label: str


class ToolInput(BaseModel):
    nested: NestedInput


class NestedOutput(BaseModel):
    value: str


class StrictOutput(BaseModel):
    nested: NestedOutput
    alternatives: list[NestedOutput]
    note: str | None = None


def test_dynamic_tool_specs_inline_schema_references() -> None:
    tool = StructuredTool.from_function(
        lambda nested: nested,
        name="inspect_item",
        description="Inspect an item.",
        args_schema=ToolInput,
    )

    specs = dynamic_tool_specs([tool])

    assert specs == [{
        "type": "function",
        "name": "inspect_item",
        "description": "Inspect an item.",
        "inputSchema": {
            "properties": {
                "nested": {
                    "properties": {"label": {"title": "Label", "type": "string"}},
                    "required": ["label"],
                    "title": "NestedInput",
                    "type": "object",
                }
            },
            "required": ["nested"],
            "title": "ToolInput",
            "type": "object",
        },
    }]


def test_dynamic_tool_specs_reject_duplicate_names() -> None:
    first = StructuredTool.from_function(
        lambda value: value,
        name="same_name",
        description="first",
    )
    second = StructuredTool.from_function(
        lambda value: value,
        name="same_name",
        description="second",
    )

    with pytest.raises(ValueError, match="duplicate tool name"):
        dynamic_tool_specs([first, second])


def test_strict_output_schema_closes_every_nested_object() -> None:
    schema = strict_output_schema(StrictOutput.model_json_schema())

    assert "$defs" not in schema
    assert schema["additionalProperties"] is False
    nested = schema["properties"]["nested"]
    assert nested["additionalProperties"] is False
    list_item = schema["properties"]["alternatives"]["items"]
    assert list_item["additionalProperties"] is False
    assert schema["required"] == ["nested", "alternatives", "note"]
    optional_note = schema["properties"]["note"]
    assert "default" not in optional_note
    assert optional_note["anyOf"] == [
        {"type": "string"},
        {"type": "null"},
    ]


def test_strict_output_schema_normalizes_single_all_of_object() -> None:
    schema = strict_output_schema({
        "allOf": [{
            "type": "object",
            "properties": {"value": {"type": "string"}},
        }]
    })

    assert "allOf" not in schema
    assert schema == {
        "type": "object",
        "properties": {"value": {"type": "string"}},
        "required": ["value"],
        "additionalProperties": False,
    }


class _RecordingTool:
    name = "record_item"
    description = "Record an item."

    def __init__(self, *, status: str = "success") -> None:
        self.calls: list[dict[str, Any]] = []
        self.status = status

    def invoke(self, call: dict[str, Any]) -> ToolMessage:
        self.calls.append(call)
        return ToolMessage(
            content={"received": call["args"]},
            tool_call_id=call["id"],
            name=call["name"],
            status=self.status,
        )


def test_tool_bridge_preserves_tool_call_identity_and_caches_retries() -> None:
    tool = _RecordingTool()
    bridge = CodexToolBridge([tool])  # type: ignore[list-item]
    params = {
        "callId": "call-7",
        "threadId": "thread-1",
        "turnId": "turn-1",
        "tool": "record_item",
        "arguments": {"video_id": "video-1"},
    }

    first = bridge.handle("item/tool/call", params)
    second = bridge.handle("item/tool/call", params)

    assert first == second == {
        "contentItems": [{
            "type": "inputText",
            "text": "{'received': {'video_id': 'video-1'}}",
        }],
        "success": True,
    }
    assert tool.calls == [{
        "type": "tool_call",
        "id": "call-7",
        "name": "record_item",
        "args": {"video_id": "video-1"},
    }]
    assert bridge.tool_call_count == 1
    assert bridge.successful_tool_call_count == 1


def test_tool_bridge_returns_tool_message_errors_to_codex() -> None:
    bridge = CodexToolBridge([
        _RecordingTool(status="error")  # type: ignore[list-item]
    ])

    response = bridge.handle("item/tool/call", {
        "callId": "call-error",
        "tool": "record_item",
        "arguments": {"value": 1},
    })

    assert response["success"] is False
    assert "received" in response["contentItems"][0]["text"]
    assert bridge.tool_call_count == 1
    assert bridge.successful_tool_call_count == 0


def test_tool_bridge_does_not_count_tool_exceptions_as_success() -> None:
    class FailingTool(_RecordingTool):
        def invoke(self, call):
            raise RuntimeError("recoverable failure")

    bridge = CodexToolBridge([FailingTool()])  # type: ignore[list-item]
    response = bridge.handle("item/tool/call", {
        "callId": "call-failed", "tool": "record_item", "arguments": {},
    })
    assert response["success"] is False
    assert bridge.tool_call_count == 1
    assert bridge.successful_tool_call_count == 0


@pytest.mark.parametrize(
    ("method", "expected"),
    [
        ("item/commandExecution/requestApproval", {"decision": "cancel"}),
        ("item/fileChange/requestApproval", {"decision": "cancel"}),
        ("execCommandApproval", {"decision": "abort"}),
        ("applyPatchApproval", {"decision": "abort"}),
        ("item/permissions/requestApproval", {"permissions": {}}),
        ("item/tool/requestUserInput", {"answers": {}}),
        ("mcpServer/elicitation/request", {"action": "cancel"}),
    ],
)
def test_tool_bridge_denies_non_seekino_server_requests(
    method: str,
    expected: dict[str, Any],
) -> None:
    bridge = CodexToolBridge([])

    assert bridge.handle(method, {}) == expected


def test_tool_bridge_bounds_tool_calls() -> None:
    tool = _RecordingTool()
    bridge = CodexToolBridge([tool], max_tool_calls=1)  # type: ignore[list-item]

    assert bridge.handle("item/tool/call", {
        "callId": "call-1",
        "tool": "record_item",
        "arguments": {},
    })["success"] is True
    limited = bridge.handle("item/tool/call", {
        "callId": "call-2",
        "tool": "record_item",
        "arguments": {},
    })

    assert limited["success"] is False
    assert "limit" in limited["contentItems"][0]["text"]
    assert len(tool.calls) == 1


def test_tool_bridge_reserves_continuation_before_executing_tool() -> None:
    tool = _RecordingTool()

    def reject_continuation() -> None:
        raise RuntimeError("model budget exhausted")

    bridge = CodexToolBridge(
        [tool],  # type: ignore[list-item]
        before_tool_call=reject_continuation,
    )

    with pytest.raises(RuntimeError, match="model budget exhausted"):
        bridge.handle("item/tool/call", {
            "callId": "call-over-budget",
            "tool": "record_item",
            "arguments": {"value": 1},
        })

    assert tool.calls == []
    assert bridge.tool_call_count == 0


def test_initial_round_and_tool_continuations_share_one_model_budget() -> None:
    tool = _RecordingTool()
    budget = CodexModelCallBudget(2)
    bridge = CodexToolBridge(
        [tool],  # type: ignore[list-item]
        before_tool_call=budget.reserve,
    )

    budget.reserve()  # Initial sampling round.
    first = bridge.handle("item/tool/call", {
        "callId": "call-1",
        "tool": "record_item",
        "arguments": {"value": 1},
    })

    assert first["success"] is True
    with pytest.raises(CodexModelCallLimitError, match="max_model_calls"):
        bridge.handle("item/tool/call", {
            "callId": "call-2",
            "tool": "record_item",
            "arguments": {"value": 2},
        })

    assert budget.used == 2
    assert len(tool.calls) == 1


def test_same_outer_turn_id_does_not_hide_sequential_continuations() -> None:
    tool = _RecordingTool()
    budget = CodexModelCallBudget(3)
    bridge = CodexToolBridge(
        [tool],  # type: ignore[list-item]
        before_tool_call=budget.reserve,
    )

    budget.reserve()  # Initial sampling round.
    for call_id in ("call-1", "call-2"):
        response = bridge.handle("item/tool/call", {
            "callId": call_id,
            # Codex reuses the outer turn ID across sequential sampling rounds.
            "turnId": "outer-turn-1",
            "tool": "record_item",
            "arguments": {"value": call_id},
        })
        assert response["success"] is True

    assert budget.used == 3
    assert len(tool.calls) == 2
