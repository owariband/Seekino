from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from langchain_core.tools import StructuredTool, ToolException
from pydantic import BaseModel

from .errors import AgentToolFeedbackError, AgentToolOutputError
from .tool_result import JsonToolResult, ToolError, ToolResult


class AgentTool:
    """Application Tool contract with LangChain kept behind an adapter."""

    def __init__(
        self,
        *,
        func: Callable[..., ToolResult],
        name: str,
        description: str,
        args_schema: type[BaseModel],
        maximum_result_bytes: int,
    ) -> None:
        self._func = func
        self._name = name
        self._description = description
        self._args_schema = args_schema
        self._maximum_result_bytes = maximum_result_bytes
        self._langchain_tool = StructuredTool.from_function(
            func=self._invoke_for_langchain,
            name=name,
            description=description,
            args_schema=args_schema,
            handle_tool_error=True,
            handle_validation_error=self._validation_feedback,
            response_format="content_and_artifact",
        )

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._description

    @property
    def args_schema(self) -> type[BaseModel]:
        return self._args_schema

    def execute(self, *args: Any, **kwargs: Any) -> ToolResult:
        try:
            result = self._func(*args, **kwargs)
        except AgentToolFeedbackError as exc:
            error_payload = ToolError(
                code=exc.code,
                message=str(exc),
                retryable=True,
                retry_hint=exc.retry_hint,
            ).as_dict()
            if exc.suggested_end is not None:
                error_payload["suggested_end"] = exc.suggested_end
            return self._failure_result(error_payload)
        if not isinstance(result, ToolResult):
            raise TypeError(
                f"Agent Tool {self.name!r} must return ToolResult, "
                f"got {type(result).__name__}"
            )
        return result

    def as_langchain_tool(self) -> StructuredTool:
        return self._langchain_tool

    def invoke(
        self,
        input: str | dict[str, Any],
        config: Any | None = None,
        **kwargs: Any,
    ) -> Any:
        return self._langchain_tool.invoke(input, config=config, **kwargs)

    def _invoke_for_langchain(self, *args: Any, **kwargs: Any) -> tuple[str, object]:
        result = self.execute(*args, **kwargs)
        content = self._model_content(result)
        if not result.success:
            raise ToolException(content)
        return content, result.artifact

    def _model_content(self, result: ToolResult) -> str:
        content = result.model_content()
        if len(content.encode("utf-8")) > self._maximum_result_bytes:
            raise AgentToolOutputError(
                f"Tool result exceeds {self._maximum_result_bytes} UTF-8 bytes"
            )
        return content

    def _failure_result(self, error: dict[str, Any]) -> JsonToolResult:
        return JsonToolResult(success=False, payload={"error": error})

    def _validation_feedback(self, error: Any) -> str:
        details = [
            f"{'.'.join(map(str, item.get('loc', ()))) or 'arguments'} "
            f"({item['type']}): {item['msg']}"
            for item in error.errors(
                include_input=False, include_context=False, include_url=False,
            )[:4]
        ]
        return self._model_content(self._failure_result(ToolError(
            code="INVALID_ARGUMENTS",
            message=f"Invalid arguments for {self.name}: {'; '.join(details)}.",
            retryable=True,
            retry_hint="Check the Tool schema and call it again with valid arguments.",
        ).as_dict()))


def as_langchain_tools(tools: Iterable[AgentTool]) -> list[StructuredTool]:
    return [tool.as_langchain_tool() for tool in tools]


def build_agent_tool(
    *,
    func: Callable[..., ToolResult],
    name: str,
    description: str,
    args_schema: type[BaseModel],
    maximum_result_bytes: int,
) -> AgentTool:
    return AgentTool(
        func=func,
        name=name,
        description=description,
        args_schema=args_schema,
        maximum_result_bytes=maximum_result_bytes,
    )
