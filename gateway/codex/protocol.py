from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from threading import Lock
from typing import Any, Literal, TypedDict

from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool
from pydantic import BaseModel

from gateway.schema_normalization import inline_schema_refs
from gateway.media import ToolImageArtifact


class DynamicToolSpec(TypedDict):
    type: Literal["function"]
    name: str
    description: str
    inputSchema: dict[str, Any]


class DynamicToolContentItem(TypedDict):
    type: Literal["inputText"]
    text: str


class DynamicToolImageItem(TypedDict):
    type: Literal["inputImage"]
    imageUrl: str


class DynamicToolResponse(TypedDict):
    contentItems: list[DynamicToolContentItem | DynamicToolImageItem]
    success: bool


def strict_output_schema(schema: Mapping[str, Any]) -> dict[str, Any]:
    """Build the strict JSON Schema required by Codex structured output."""

    def normalize(value: Any) -> Any:
        if isinstance(value, list):
            return [normalize(item) for item in value]
        if not isinstance(value, dict):
            return value
        normalized = {key: normalize(item) for key, item in value.items()}

        all_of = normalized.get("allOf")
        if (
            isinstance(all_of, list)
            and len(all_of) == 1
            and isinstance(all_of[0], dict)
        ):
            normalized.pop("allOf")
            normalized.update(all_of[0])

        if normalized.get("default", ...) is None:
            normalized.pop("default")

        properties = normalized.get("properties")
        schema_type = normalized.get("type")
        is_object = (
            schema_type == "object"
            or (
                isinstance(schema_type, list)
                and "object" in schema_type
            )
            or isinstance(properties, dict)
        )
        if is_object:
            normalized["additionalProperties"] = False
        if isinstance(properties, dict):
            normalized["required"] = list(properties)
        return normalized

    return normalize(inline_schema_refs(schema))


def dynamic_tool_specs(tools: Sequence[BaseTool]) -> list[DynamicToolSpec]:
    """Translate LangChain tools to Codex app-server dynamic tool specs."""
    seen_names: set[str] = set()
    specs: list[DynamicToolSpec] = []
    for tool in tools:
        if not tool.name.strip():
            raise ValueError("tool name must not be empty")
        if tool.name in seen_names:
            raise ValueError(f"duplicate tool name: {tool.name!r}")
        seen_names.add(tool.name)
        specs.append({
            "type": "function",
            "name": tool.name,
            "description": tool.description or "",
            "inputSchema": inline_schema_refs(
                tool.get_input_schema().model_json_schema()
            ),
        })
    return specs


def _output_text(output: object) -> str:
    if isinstance(output, str):
        return output
    if isinstance(output, BaseModel):
        output = output.model_dump(mode="json")
    return json.dumps(
        output,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )


def _tool_response(
    content: object, *, success: bool, artifact: object = None,
) -> DynamicToolResponse:
    response: DynamicToolResponse = {
        "contentItems": [{"type": "inputText", "text": _output_text(content)}],
        "success": success,
    }
    if success and isinstance(artifact, ToolImageArtifact):
        for image in artifact.images:
            if image.description:
                response["contentItems"].append({
                    "type": "inputText", "text": image.description,
                })
            response["contentItems"].append({
                "type": "inputImage", "imageUrl": image.data_url,
            })
    return response


class CodexToolBridge:
    """Serve app-server requests while denying every non-AniVAG capability."""

    def __init__(
        self,
        tools: Sequence[BaseTool],
        *,
        check_control: Callable[[], None] | None = None,
        max_tool_calls: int | None = None,
        before_tool_call: Callable[[], None] | None = None,
    ) -> None:
        self._tools = {tool.name: tool for tool in tools}
        if len(self._tools) != len(tools):
            raise ValueError("tool names must be unique")
        if max_tool_calls is not None and max_tool_calls <= 0:
            raise ValueError("max_tool_calls must be positive when provided")
        self._check_control = check_control
        self._max_tool_calls = max_tool_calls
        self._before_tool_call = before_tool_call
        self._call_count = 0
        self._successful_call_count = 0
        self._responses: dict[str, DynamicToolResponse] = {}
        self._lock = Lock()

    @property
    def tool_call_count(self) -> int:
        with self._lock:
            return self._call_count

    @property
    def successful_tool_call_count(self) -> int:
        with self._lock:
            return self._successful_call_count

    def handle(
        self,
        method: str,
        params: dict[str, Any] | None,
    ) -> dict[str, Any]:
        if method == "item/tool/call":
            return self._handle_tool_call(params)
        if method in {
            "item/commandExecution/requestApproval",
            "item/fileChange/requestApproval",
        }:
            return {"decision": "cancel"}
        if method in {"execCommandApproval", "applyPatchApproval"}:
            return {"decision": "abort"}
        if method == "item/permissions/requestApproval":
            return {"permissions": {}}
        if method == "item/tool/requestUserInput":
            return {"answers": {}}
        if method == "mcpServer/elicitation/request":
            return {"action": "cancel"}
        return {}

    def _handle_tool_call(
        self,
        params: dict[str, Any] | None,
    ) -> DynamicToolResponse:
        if not isinstance(params, Mapping):
            return _tool_response("Invalid dynamic tool request.", success=False)
        call_id = params.get("callId")
        tool_name = params.get("tool")
        arguments = params.get("arguments")
        if not isinstance(call_id, str) or not call_id:
            return _tool_response("Dynamic tool call is missing callId.", success=False)

        with self._lock:
            cached = self._responses.get(call_id)
            if cached is not None:
                return cached
            limit_reached = (
                self._max_tool_calls is not None
                and self._call_count >= self._max_tool_calls
            )

        # Returning any dynamic tool result lets app-server start another model
        # sampling round. `turnId` spans every sampling round in one outer turn,
        # so it cannot identify parallel batches safely. Reserve before running
        # a potentially side-effecting tool or responding to app-server.
        if self._before_tool_call is not None:
            self._before_tool_call()

        if limit_reached:
            response = _tool_response(
                "The tool-call limit for this invocation has been reached.",
                success=False,
            )
            with self._lock:
                self._responses[call_id] = response
            return response
        if not isinstance(tool_name, str) or tool_name not in self._tools:
            response = _tool_response(
                f"Unknown dynamic tool: {tool_name!r}.",
                success=False,
            )
            with self._lock:
                self._responses[call_id] = response
            return response
        if not isinstance(arguments, Mapping):
            response = _tool_response(
                "Dynamic tool arguments must be a JSON object.",
                success=False,
            )
            with self._lock:
                self._responses[call_id] = response
            return response

        if self._check_control is not None:
            try:
                self._check_control()
            except Exception as exc:
                return _tool_response(
                    f"Tool execution was cancelled: {type(exc).__name__}.",
                    success=False,
                )

        tool = self._tools[tool_name]
        try:
            output = tool.invoke({
                "type": "tool_call",
                "id": call_id,
                "name": tool.name,
                "args": dict(arguments),
            })
            if not isinstance(output, ToolMessage):
                raise TypeError(
                    f"LangChain tool {tool.name!r} did not return a ToolMessage"
                )
            if output.tool_call_id != call_id:
                raise RuntimeError(
                    f"LangChain tool {tool.name!r} changed its tool call ID"
                )
            response = _tool_response(
                output.content,
                success=output.status != "error",
                artifact=output.artifact,
            )
        except Exception as exc:
            response = _tool_response(
                f"Tool execution failed: {type(exc).__name__}: {exc}",
                success=False,
            )

        with self._lock:
            self._call_count += 1
            if response["success"]:
                self._successful_call_count += 1
            self._responses[call_id] = response
        return response
