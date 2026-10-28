from __future__ import annotations

import asyncio
import contextvars
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from threading import Lock, Thread
from typing import Any
from uuid import uuid4

from claude_agent_sdk import SdkMcpTool, create_sdk_mcp_server, tool as sdk_tool
from claude_agent_sdk.types import McpSdkServerConfig
from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool

from gateway.schema_normalization import inline_schema_refs
from gateway.media import ToolImageArtifact

_SERVER_NAME = "seekino"


@dataclass(slots=True)
class ToolInvocationState:
    """Invocation facts shared by an SDK MCP server and its caller."""

    count: int = 0
    _lock: Lock = field(default_factory=Lock, repr=False)

    def begin(self) -> str:
        with self._lock:
            self.count += 1
        return f"claude-{uuid4()}"


@dataclass(frozen=True, slots=True)
class ToolBridge:
    server: McpSdkServerConfig
    allowed_names: tuple[str, ...]
    contract: tuple[dict[str, Any], ...]
    state: ToolInvocationState


def build_tool_bridge(tools: Sequence[BaseTool]) -> ToolBridge:
    """Expose LangChain tools through one in-process SDK MCP server."""

    _validate_tools(tools)
    state = ToolInvocationState()
    contract = tuple(_tool_contract(item) for item in tools)
    sdk_tools = [
        _build_sdk_tool(item, state, input_schema=spec["inputSchema"])
        for item, spec in zip(tools, contract, strict=True)
    ]
    return ToolBridge(
        server=create_sdk_mcp_server(_SERVER_NAME, tools=sdk_tools),
        allowed_names=tuple(spec["name"] for spec in contract),
        contract=contract,
        state=state,
    )


def _build_sdk_tool(
    base_tool: BaseTool,
    state: ToolInvocationState,
    *,
    input_schema: dict[str, Any] | None = None,
) -> SdkMcpTool[Any]:
    input_schema = input_schema or inline_schema_refs(
        base_tool.get_input_jsonschema()
    )

    @sdk_tool(base_tool.name, base_tool.description, input_schema)
    async def invoke(arguments: dict[str, Any]) -> dict[str, Any]:
        call_id = state.begin()
        tool_call = {
            "type": "tool_call",
            "id": call_id,
            "name": base_tool.name,
            "args": arguments,
        }
        try:
            result = await _run_sync_tool(base_tool, tool_call)
        except Exception as exc:  # The model needs actionable tool feedback.
            return _mcp_text_result(str(exc), is_error=True)
        if isinstance(result, ToolMessage):
            return _mcp_text_result(
                _tool_output_text(result.content),
                is_error=result.status == "error",
                artifact=result.artifact,
            )
        return _mcp_text_result(_tool_output_text(result), is_error=False)

    return invoke


def _tool_contract(base_tool: BaseTool) -> dict[str, Any]:
    return {
        "name": f"mcp__{_SERVER_NAME}__{base_tool.name}",
        "description": base_tool.description,
        "inputSchema": inline_schema_refs(base_tool.get_input_jsonschema()),
    }


async def _run_sync_tool(base_tool: BaseTool, tool_call: dict[str, Any]) -> Any:
    """Run a sync LangChain tool without tying cancellation to asyncio's executor.

    asyncio.run waits for its default executor during shutdown. A stuck sync tool
    launched with asyncio.to_thread would therefore make the provider deadline
    ineffective. The daemon thread may finish after cancellation, while the SDK
    request can stop immediately. copy_context preserves request and agent logging
    context inside the tool invocation.
    """

    loop = asyncio.get_running_loop()
    completed: asyncio.Future[Any] = loop.create_future()
    context = contextvars.copy_context()

    def finish_with_result(result: Any) -> None:
        if not completed.done():
            completed.set_result(result)

    def finish_with_error(error: BaseException) -> None:
        if not completed.done():
            completed.set_exception(error)

    def notify(callback: Any, value: Any) -> None:
        try:
            loop.call_soon_threadsafe(callback, value)
        except RuntimeError:
            # The awaiting request was cancelled and asyncio.run has already
            # closed its loop. The detached daemon worker can simply finish.
            return

    def run() -> None:
        try:
            result = context.run(base_tool.invoke, tool_call)
        except BaseException as exc:
            notify(finish_with_error, exc)
        else:
            notify(finish_with_result, result)

    Thread(
        target=run,
        name=f"seekino-claude-tool-{base_tool.name}",
        daemon=True,
    ).start()
    return await completed


def _validate_tools(tools: Sequence[BaseTool]) -> None:
    if not tools:
        raise ValueError("tools must not be empty")
    names = [item.name for item in tools]
    if any(not name.strip() for name in names):
        raise ValueError("tool names must not be empty")
    if len(names) != len(set(names)):
        raise ValueError("tool names must be unique")
    if any(not item.description.strip() for item in tools):
        raise ValueError("tool descriptions must not be empty")


def _mcp_text_result(
    text: str, *, is_error: bool, artifact: object = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "content": [{"type": "text", "text": text}],
    }
    if is_error:
        result["is_error"] = True
    elif isinstance(artifact, ToolImageArtifact):
        for image in artifact.images:
            if image.description:
                result["content"].append({"type": "text", "text": image.description})
            header, data = image.data_url.split(",", 1)
            result["content"].append({
                "type": "image", "mimeType": header[5:].split(";", 1)[0],
                "data": data,
            })
    return result


def _tool_output_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    try:
        return json.dumps(content, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(content)
