from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import replace
from threading import BoundedSemaphore, Event, Lock
from time import monotonic
from typing import Any, Literal, TypeVar

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ClaudeSDKError,
    ResultMessage,
    TextBlock,
)
from langchain_core.tools import BaseTool
from pydantic import BaseModel, ValidationError

from gateway.agent_model.errors import (
    AgentModelTimeoutError,
    MissingRequiredFunctionCallError,
    MissingStructuredQueryDraftError,
    ModelFailureError,
    ModelFailureReason,
    ModelInputTooLargeError,
)
from gateway.constants import CLAUDE_CODE_PROVIDER
from gateway.agent_model.context import (
    ContextCheckpoint,
    ContextWindowPolicy,
)
from gateway.schema_normalization import inline_schema_refs
from gateway.media import prepare_input_messages, require_vision_input
from gateway.usage import ModelCallTracker, SDKModelUsage, TokenUsage, track_model_call

from .tools import ToolBridge, build_tool_bridge

_ResultModel = TypeVar("_ResultModel", bound=BaseModel)
_Awaited = TypeVar("_Awaited")
_AuthMode = Literal["local_subscription", "api_key"]
_ReasoningEffort = Literal["none", "low", "medium", "high", "xhigh", "max"]
_logger = logging.getLogger(__name__)

_CLAUDE_CREDENTIAL_ENV_KEYS = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
)
_CLAUDE_API_MODE_ONLY_CLEAR_ENV_KEYS = ("CLAUDE_CODE_OAUTH_TOKEN",)

_SYSTEM_PROMPT = """\
You are an AniVAG agent stage. Follow the user prompt exactly.
You may call only the MCP tools supplied by AniVAG. Do not access files, run shell
commands, load skills, start subagents, or use any unlisted tool. Treat tool errors
as feedback that you can correct in a later call.
"""


class ClaudeAgentModelError(ModelFailureError):
    """Claude Agent SDK failed or returned an invalid agent response."""


class ClaudeAgentModel:
    """Adapt Claude Agent SDK to AniVAG's structured and tool-loop protocols."""

    provider_name = CLAUDE_CODE_PROVIDER
    supports_vision_input = True
    supports_context_checkpoints = False
    supports_context_messages = False
    supports_stop_feedback = True

    def __init__(
        self,
        *,
        model: str,
        reasoning_effort: _ReasoningEffort = "none",
        request_timeout_seconds: float,
        max_output_tokens: int,
        max_input_chars: int | None,
        context_window_policy: ContextWindowPolicy | None = None,
        auth_mode: _AuthMode = "local_subscription",
        api_key: str | None = None,
        cli_path: str | None = None,
        max_concurrency: int = 4,
        concurrency_gate: BoundedSemaphore | None = None,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        client_factory: Callable[[ClaudeAgentOptions], Any] | None = None,
    ) -> None:
        if not model.strip():
            raise ValueError("model must not be empty")
        if reasoning_effort not in {"none", "low", "medium", "high", "xhigh", "max"}:
            raise ValueError("unsupported reasoning_effort")
        if request_timeout_seconds <= 0:
            raise ValueError("request_timeout_seconds must be greater than zero")
        if max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be greater than zero")
        if max_input_chars is not None and (
            isinstance(max_input_chars, bool)
            or not isinstance(max_input_chars, int)
            or max_input_chars <= 0
        ):
            raise ValueError("max_input_chars must be positive when provided")
        if auth_mode not in {"local_subscription", "api_key"}:
            raise ValueError("unsupported auth_mode")
        if auth_mode == "api_key" and (api_key is None or not api_key.strip()):
            raise ValueError("api_key is required when auth_mode is api_key")
        if cli_path is not None and not cli_path.strip():
            raise ValueError("cli_path must not be empty")
        if cwd is not None and not cwd.strip():
            raise ValueError("cwd must not be empty")
        if (
            isinstance(max_concurrency, bool)
            or not isinstance(max_concurrency, int)
            or max_concurrency <= 0
        ):
            raise ValueError("max_concurrency must be a positive integer")
        environment = dict(env or {})
        if any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in environment.items()
        ):
            raise ValueError("env keys and values must be strings")
        # The SDK subprocess otherwise inherits these values from os.environ.
        # Clear all competing auth routes so local_subscription cannot silently
        # consume API credit and api_key cannot be shadowed by an auth token.
        environment.update(dict.fromkeys(_CLAUDE_CREDENTIAL_ENV_KEYS, ""))
        if auth_mode == "api_key":
            assert api_key is not None
            environment.update(
                dict.fromkeys(_CLAUDE_API_MODE_ONLY_CLEAR_ENV_KEYS, "")
            )
            environment["ANTHROPIC_API_KEY"] = api_key

        self.model_name = model
        self.reasoning_effort = reasoning_effort
        self.request_timeout_seconds = request_timeout_seconds
        self.max_output_tokens = max_output_tokens
        self.max_input_chars = max_input_chars
        self.context_window_policy = context_window_policy
        self.auth_mode = auth_mode
        self.cli_path = cli_path
        self.cwd = cwd
        self._environment = environment
        self._client_factory = client_factory or ClaudeSDKClient
        self._capacity = concurrency_gate or BoundedSemaphore(max_concurrency)
        self._closed = Event()
        self._close_lock = Lock()

    def run_structured(
        self,
        prompt: str,
        *,
        tools: Sequence[BaseTool],
        result_type: type[_ResultModel],
        max_model_calls: int | None,
        request_timeout_seconds: float | None = None,
        check_control: Callable[[], None] | None = None,
        max_input_chars: int | None = None,
        context_checkpoint: ContextCheckpoint | None = None,
        initial_messages: Sequence[dict[str, Any]] | None = None,
        image_urls: Sequence[str] = (),
        stop_feedback: Callable[[], str | None] | None = None,
    ) -> _ResultModel:
        prompt, image_content = self._prepare_input(prompt, initial_messages=initial_messages, image_urls=image_urls)
        timeout = self._validate_call(
            prompt,
            tools=tools,
            max_model_calls=max_model_calls,
            request_timeout_seconds=request_timeout_seconds,
            check_control=check_control,
            max_input_chars=max_input_chars,
            context_checkpoint=context_checkpoint,
        )
        schema = inline_schema_refs(result_type.model_json_schema())
        bridge = build_tool_bridge(tools)
        options = self._options(
            bridge,
            max_model_calls=max_model_calls,
            output_schema=schema,
        )

        try:
            response = self._run_with_capacity(
                lambda remaining: self._run_async(
                    options=options,
                    initial_prompt=prompt,
                    image_content=image_content,
                    timeout_seconds=min(timeout, remaining),
                    check_control=check_control,
                    bridge=bridge if stop_feedback is not None else None,
                    stop_feedback=stop_feedback,
                    max_model_calls=max_model_calls,
                    result_type=result_type if stop_feedback is not None else None,
                ),
                timeout_seconds=timeout,
                check_control=check_control,
            )[-1]
        except ClaudeSDKError as exc:
            raise ClaudeAgentModelError(f"Claude Agent SDK request failed: {exc}") from exc

        self._raise_for_result_error(response.result)
        return _structured_result(response, result_type)

    def run_agent_loop(
        self,
        prompt: str,
        *,
        tools: Sequence[BaseTool],
        max_model_calls: int | None,
        handle_text: Callable[[str], None],
        require_tool_call: bool = False,
        stop_feedback: Callable[[], str | None] | None = None,
        request_timeout_seconds: float | None = None,
        check_control: Callable[[], None] | None = None,
        max_input_chars: int | None = None,
        context_checkpoint: ContextCheckpoint | None = None,
        initial_messages: Sequence[dict[str, Any]] | None = None,
        image_urls: Sequence[str] = (),
    ) -> None:
        prompt, image_content = self._prepare_input(prompt, initial_messages=initial_messages, image_urls=image_urls)
        timeout = self._validate_call(
            prompt,
            tools=tools,
            max_model_calls=max_model_calls,
            request_timeout_seconds=request_timeout_seconds,
            check_control=check_control,
            max_input_chars=max_input_chars,
            context_checkpoint=context_checkpoint,
        )
        bridge = build_tool_bridge(tools)
        options = self._options(bridge, max_model_calls=max_model_calls)
        try:
            responses = self._run_with_capacity(
                lambda remaining: self._run_async(
                    options=options,
                    initial_prompt=prompt,
                    image_content=image_content,
                    timeout_seconds=min(timeout, remaining),
                    check_control=check_control,
                    bridge=bridge,
                    require_tool_call=require_tool_call,
                    stop_feedback=stop_feedback,
                    handle_text=handle_text,
                    max_model_calls=max_model_calls,
                ),
                timeout_seconds=timeout,
                check_control=check_control,
            )
        except ClaudeSDKError as exc:
            raise ClaudeAgentModelError(f"Claude Agent SDK request failed: {exc}") from exc
        if not responses:
            raise ClaudeAgentModelError("Claude Agent SDK returned no response")

    def close(self) -> None:
        """Prevent new calls; active calls observe this flag and interrupt."""

        with self._close_lock:
            self._closed.set()

    def _prepare_input(
        self, prompt: str, *, initial_messages: Sequence[dict[str, Any]] | None,
        image_urls: Sequence[str],
    ) -> tuple[str, list[dict[str, Any]]]:
        messages = prepare_input_messages(prompt, initial_messages=initial_messages, image_urls=image_urls)
        require_vision_input(messages, supports_vision_input=self.supports_vision_input, model_name=self.model_name)
        rendered, image_content = [], []
        for index, message in enumerate(messages, start=1):
            content = message.get("content", "")
            if isinstance(content, list):
                texts, images = [], []
                for block in content:
                    if block["type"] in {"text", "input_text", "output_text"}:
                        texts.append(block["text"])
                    elif block["type"] in {"image_url", "input_image"}:
                        images.append(block)
                    else:
                        raise ValueError(f"Unsupported Claude input block: {block['type']}")
                rendered.append({**message, "content": "\n".join(texts), "imageCount": len(images)})
                if images:
                    image_content.extend([{"type": "text", "text": f"Attached images for conversation message {index}:"}, *images])
            else:
                rendered.append(message)
        if initial_messages is not None:
            prompt = "Existing conversation messages (JSON):\n" + json.dumps(rendered, ensure_ascii=False) + "\n\nCurrent instruction:\n" + prompt
        return prompt, image_content

    def _validate_call(
        self,
        prompt: str,
        *,
        tools: Sequence[BaseTool],
        max_model_calls: int | None,
        request_timeout_seconds: float | None,
        check_control: Callable[[], None] | None,
        max_input_chars: int | None,
        context_checkpoint: ContextCheckpoint | None,
    ) -> float:
        if not prompt.strip():
            raise ValueError("prompt must not be empty")
        if max_model_calls is not None and (
            isinstance(max_model_calls, bool)
            or not isinstance(max_model_calls, int)
            or max_model_calls <= 0
        ):
            raise ValueError("max_model_calls must be a positive integer")
        timeout = (
            self.request_timeout_seconds
            if request_timeout_seconds is None
            else request_timeout_seconds
        )
        if timeout <= 0:
            raise ValueError("request_timeout_seconds must be greater than zero")
        if max_input_chars is not None and (
            isinstance(max_input_chars, bool)
            or not isinstance(max_input_chars, int)
            or max_input_chars <= 0
        ):
            raise ValueError("max_input_chars must be positive when provided")
        if context_checkpoint is not None:
            raise ValueError("Claude Agent provider does not accept external context messages")
        # build_tool_bridge performs detailed tool validation after cheap call guards.
        if not tools:
            raise ValueError("tools must not be empty")
        self._check_control(check_control)
        return timeout

    def _options(
        self,
        bridge: ToolBridge,
        *,
        max_model_calls: int | None,
        output_schema: dict[str, Any] | None = None,
    ) -> ClaudeAgentOptions:
        return ClaudeAgentOptions(
            tools=[],
            allowed_tools=list(bridge.allowed_names),
            mcp_servers={"anivag": bridge.server},
            strict_mcp_config=True,
            permission_mode="dontAsk",
            setting_sources=[],
            skills=[],
            plugins=[],
            system_prompt=_SYSTEM_PROMPT,
            model=self.model_name,
            max_turns=max_model_calls,
            cwd=self.cwd,
            cli_path=self.cli_path,
            env=dict(self._environment),
            extra_args={"no-session-persistence": None},
            thinking=(
                {"type": "disabled"}
                if self.reasoning_effort == "none"
                else None
            ),
            effort=(None if self.reasoning_effort == "none" else self.reasoning_effort),
            output_format=(
                {"type": "json_schema", "schema": output_schema}
                if output_schema is not None
                else None
            ),
        )

    def _run_with_capacity(
        self,
        operation: Callable[[float], Any],
        *,
        timeout_seconds: float,
        check_control: Callable[[], None] | None,
    ) -> Any:
        deadline = monotonic() + timeout_seconds
        while True:
            self._check_control(check_control)
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise AgentModelTimeoutError("Claude Agent request deadline expired while queued")
            if self._capacity.acquire(timeout=min(0.1, remaining)):
                break
        try:
            self._check_control(check_control)
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise AgentModelTimeoutError("Claude Agent request deadline expired before start")
            return asyncio.run(
                _run_with_timeout(
                    operation(remaining),
                    timeout_seconds=remaining,
                )
            )
        finally:
            self._capacity.release()

    async def _run_async(
        self,
        *,
        options: ClaudeAgentOptions,
        initial_prompt: str,
        image_content: Sequence[dict[str, Any]],
        timeout_seconds: float,
        check_control: Callable[[], None] | None,
        bridge: ToolBridge | None = None,
        require_tool_call: bool = False,
        stop_feedback: Callable[[], str | None] | None = None,
        handle_text: Callable[[str], None] | None = None,
        max_model_calls: int | None = None,
        result_type: type[_ResultModel] | None = None,
    ) -> list[_Response]:
        deadline = monotonic() + timeout_seconds
        responses: list[_Response] = []
        prompt = initial_prompt
        correction_sent = False
        submitted_tool = False
        used_turns = 0
        while True:
            self._check_control(check_control)
            remaining_calls = (
                None if max_model_calls is None else max_model_calls - used_turns
            )
            if remaining_calls is not None and remaining_calls <= 0:
                raise ClaudeAgentModelError(
                    "Claude Agent loop exhausted max_model_calls before stop",
                    reason=ModelFailureReason.LOOP_LIMIT,
                )

            # Each feedback round starts a fresh, non-persistent CLI process.
            # The visible prior answer and evaluator feedback are carried in the
            # next prompt; AniVAG's tools remain the canonical workspace.
            round_options = replace(
                options,
                max_turns=remaining_calls,
                resume=None,
            )
            client = self._client_factory(round_options)
            entered = False
            feedback = None
            try:
                await self._await_with_control(
                    client.__aenter__(),
                    client=client,
                    deadline=deadline,
                    check_control=check_control,
                    interrupt_on_cancel=False,
                )
                entered = True
                before_tools = bridge.state.count if bridge is not None else 0
                await self._await_with_control(
                    client.query(_image_prompt(prompt, image_content) if image_content else prompt),
                    client=client,
                    deadline=deadline,
                    check_control=check_control,
                )
                with track_model_call(
                    provider=self.provider_name,
                    model=self.model_name,
                    operation="agent",
                    granularity="sdk_turn_run",
                    usage_source="claude_result_usage",
                ) as tracker:
                    messages = await self._receive_with_control(
                        client,
                        deadline=deadline,
                        check_control=check_control,
                    )
                    response = _response_from_messages(messages)
                    _capture_usage(tracker, response.result, self.model_name)
                    self._raise_for_result_error(response.result)
                used_turns += max(response.result.num_turns, 1)
                if max_model_calls is not None and used_turns > max_model_calls:
                    raise ClaudeAgentModelError(
                        "Claude Agent loop exceeded max_model_calls",
                        reason=ModelFailureReason.LOOP_LIMIT,
                    )
                if result_type is not None:
                    value = _structured_result(response, result_type)
                    self._check_control(check_control)
                    if monotonic() >= deadline:
                        raise AgentModelTimeoutError("Claude Agent request deadline expired")
                    feedback = stop_feedback() if stop_feedback is not None else None
                    self._check_control(check_control)
                    if monotonic() >= deadline:
                        raise AgentModelTimeoutError("Claude Agent request deadline expired")
            finally:
                if entered:
                    await client.__aexit__(None, None, None)

            responses.append(response)
            self._check_control(check_control)

            if result_type is not None:
                if not feedback:
                    return responses
                if max_model_calls is not None and used_turns >= max_model_calls:
                    raise ClaudeAgentModelError(
                        "Claude Agent loop exhausted max_model_calls before stop",
                        reason=ModelFailureReason.LOOP_LIMIT,
                    )
                prompt = _follow_up_prompt(
                    original_prompt=initial_prompt,
                    previous_response=value.model_dump_json(),
                    feedback=feedback,
                )
                continue

            if bridge is None:
                return responses

            tool_was_called = bridge.state.count > before_tools
            submitted_tool = submitted_tool or tool_was_called
            if require_tool_call and not submitted_tool:
                if correction_sent:
                    raise MissingRequiredFunctionCallError(
                        "Claude Agent stopped twice without the required tool call"
                    )
                correction_sent = True
                if max_model_calls is not None and used_turns >= max_model_calls:
                    raise MissingRequiredFunctionCallError(
                        "Claude Agent exhausted max_model_calls without the required tool call",
                        reason=ModelFailureReason.LOOP_LIMIT,
                    )
                prompt = _follow_up_prompt(
                    original_prompt=initial_prompt,
                    previous_response=response.text,
                    feedback="请调用一个可用工具，不要只返回文字。",
                )
                continue
            correction_sent = False

            text = response.text
            if not text.strip():
                raise ClaudeAgentModelError(
                    "Claude Agent completed without visible text"
                )
            assert handle_text is not None
            handle_text(text)
            feedback = stop_feedback() if stop_feedback is not None else None
            if not feedback:
                return responses
            if max_model_calls is not None and used_turns >= max_model_calls:
                raise ClaudeAgentModelError(
                    "Claude Agent loop exhausted max_model_calls before stop",
                    reason=ModelFailureReason.LOOP_LIMIT,
                )
            prompt = _follow_up_prompt(
                original_prompt=initial_prompt,
                previous_response=text,
                feedback=feedback[:500],
            )

    async def _receive_with_control(
        self,
        client: Any,
        *,
        deadline: float,
        check_control: Callable[[], None] | None,
    ) -> list[Any]:
        async def collect() -> list[Any]:
            return [message async for message in client.receive_response()]

        return await self._await_with_control(
            collect(),
            client=client,
            deadline=deadline,
            check_control=check_control,
        )

    async def _await_with_control(
        self,
        operation: Awaitable[_Awaited],
        *,
        client: Any,
        deadline: float,
        check_control: Callable[[], None] | None,
        interrupt_on_cancel: bool = True,
    ) -> _Awaited:
        task = asyncio.ensure_future(operation)
        try:
            while not task.done():
                try:
                    self._check_control(check_control)
                except BaseException:
                    if interrupt_on_cancel:
                        await _interrupt(client)
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    raise
                remaining = deadline - monotonic()
                if remaining <= 0:
                    if interrupt_on_cancel:
                        await _interrupt(client)
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    raise AgentModelTimeoutError("Claude Agent request deadline expired")
                await asyncio.wait({task}, timeout=min(0.1, remaining))
            return task.result()
        except asyncio.CancelledError:
            if interrupt_on_cancel:
                await _interrupt(client)
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            raise
        except BaseException:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            raise

    def _check_control(self, check_control: Callable[[], None] | None) -> None:
        if self._closed.is_set():
            raise RuntimeError("Claude Agent model is closed")
        if check_control is not None:
            check_control()

    @staticmethod
    def _raise_for_result_error(result: ResultMessage) -> None:
        if (
            result.stop_reason == "model_context_window_exceeded"
            or result.terminal_reason == "model_context_window_exceeded"
        ):
            raise ModelInputTooLargeError(
                "Claude Agent exceeded the model context window"
            )
        output_limit_reasons = {"max_tokens"}
        loop_limit_reasons = {"max_turns"}
        if (
            not result.is_error
            and result.stop_reason not in output_limit_reasons
            and result.stop_reason not in loop_limit_reasons
            and result.terminal_reason not in loop_limit_reasons
            and result.subtype != "error_max_turns"
        ):
            return
        details = "; ".join(result.errors or ()) or result.result or result.subtype
        if (
            result.stop_reason in loop_limit_reasons
            or result.terminal_reason in loop_limit_reasons
            or result.subtype == "error_max_turns"
        ):
            reason = ModelFailureReason.LOOP_LIMIT
        elif result.stop_reason in output_limit_reasons:
            reason = ModelFailureReason.OUTPUT_TOKEN_LIMIT
        else:
            reason = ModelFailureReason.INVALID_MODEL_RESPONSE
        raise ClaudeAgentModelError(
            f"Claude Agent request failed: {details}",
            reason=reason,
        )

class _Response:
    __slots__ = ("messages", "result", "text")

    def __init__(self, messages: list[Any], result: ResultMessage, text: str) -> None:
        self.messages = messages
        self.result = result
        self.text = text


def _structured_result(response: _Response, result_type: type[_ResultModel]) -> _ResultModel:
    value = response.result.structured_output
    if value is None and response.result.result:
        try:
            value = json.loads(response.result.result)
        except json.JSONDecodeError:
            value = None
    try:
        return result_type.model_validate(value)
    except ValidationError as exc:
        raise MissingStructuredQueryDraftError(
            "Claude Agent ended without its required structured result",
            reason=ModelFailureReason.INVALID_MODEL_RESPONSE,
        ) from exc


def _response_from_messages(messages: list[Any]) -> _Response:
    results = [message for message in messages if isinstance(message, ResultMessage)]
    if not results:
        raise ClaudeAgentModelError("Claude Agent SDK response had no ResultMessage")
    text_parts: list[str] = []
    for message in messages:
        if not isinstance(message, AssistantMessage) or message.parent_tool_use_id is not None:
            continue
        text_parts.extend(
            block.text.strip()
            for block in message.content
            if isinstance(block, TextBlock) and block.text.strip()
        )
    result = results[-1]
    text = "\n\n".join(text_parts)
    if not text and isinstance(result.result, str):
        text = result.result.strip()
    return _Response(messages, result, text)


async def _image_prompt(prompt: str, image_content: Sequence[dict[str, Any]]) -> AsyncIterator[dict[str, Any]]:
    content = [{"type": "text", "text": prompt}]
    for block in image_content:
        if block["type"] == "text":
            content.append(block)
            continue
        image = block["image_url"]
        url = image["url"] if isinstance(image, dict) else image
        if url.startswith("data:"):
            header, data = url.split(",", 1)
            if not header.endswith(";base64"):
                raise ValueError("Claude image data URLs must contain base64 data")
            source = {"type": "base64", "media_type": header[5:-7], "data": data}
        elif url.startswith(("https://", "http://")):
            source = {"type": "url", "url": url}
        else:
            raise ValueError("Claude images require data or HTTP URLs")
        content.append({"type": "image", "source": source})
    yield {"type": "user", "message": {"role": "user", "content": content}, "parent_tool_use_id": None}


def _follow_up_prompt(
    *,
    original_prompt: str,
    previous_response: str,
    feedback: str,
) -> str:
    context = {
        "original_instruction": original_prompt,
        "previous_response": previous_response,
        "feedback": feedback,
    }
    return (
        "Continue the AniVAG task from this explicit context. "
        "The canonical workspace remains available through the registered tools. "
        "Do not assume any other conversation history.\n"
        + json.dumps(context, ensure_ascii=False)
    )


def _capture_usage(
    tracker: ModelCallTracker,
    result: ResultMessage,
    model_name: str,
) -> None:
    usage = dict(result.usage or {})
    tracker.usage_scope = "main_loop"
    input_tokens = usage.get("input_tokens")
    output_tokens = usage.get("output_tokens")
    cache_creation_tokens = usage.get("cache_creation_input_tokens", 0)
    cache_read_tokens = usage.get("cache_read_input_tokens", 0)
    if (
        isinstance(input_tokens, int)
        and not isinstance(input_tokens, bool)
        and isinstance(output_tokens, int)
        and not isinstance(output_tokens, bool)
        and isinstance(cache_creation_tokens, int)
        and not isinstance(cache_creation_tokens, bool)
        and isinstance(cache_read_tokens, int)
        and not isinstance(cache_read_tokens, bool)
    ):
        total_input_tokens = (
            input_tokens + cache_creation_tokens + cache_read_tokens
        )
        usage["input_tokens"] = total_input_tokens
        usage["total_tokens"] = total_input_tokens + output_tokens
        usage["input_tokens_details"] = {
            "cached_tokens": usage.get("cache_read_input_tokens"),
            "cache_write_tokens": usage.get("cache_creation_input_tokens"),
        }
        cache_creation = usage.get("cache_creation")
        if isinstance(cache_creation, Mapping):
            usage["input_tokens_details"].update({
                "cache_write_5m_tokens": cache_creation.get("ephemeral_5m_input_tokens"),
                "cache_write_1h_tokens": cache_creation.get("ephemeral_1h_input_tokens"),
            })
    tracker.capture_response({"model": model_name, "usage": usage})
    breakdown = result.model_usage
    if not isinstance(breakdown, Mapping):
        return
    model_records = []
    for actual_model, reported in breakdown.items():
        if not isinstance(actual_model, str) or not actual_model.strip() or not isinstance(reported, Mapping):
            _logger.warning("Claude SDK returned invalid per-model usage metadata")
            continue
        tokens = None
        try:
            plain_input = reported.get("inputTokens")
            cached_input = reported.get("cacheReadInputTokens")
            cache_write = reported.get("cacheCreationInputTokens")
            output = reported.get("outputTokens")
            values = (plain_input, cached_input, cache_write, output)
            if all(isinstance(value, int) and not isinstance(value, bool) for value in values):
                total_input = plain_input + cached_input + cache_write
                tokens = TokenUsage(
                    total_input, output, total_input + output, cached_input, None,
                    cache_write_input_tokens=cache_write,
                )
        except ValueError:
            _logger.warning("Claude SDK returned invalid per-model token counts")
        canonical_model = reported.get("canonicalModel")
        actual_model = canonical_model if isinstance(canonical_model, str) and canonical_model.strip() else actual_model
        try:
            record = SDKModelUsage(actual_model, tokens, reported.get("costUSD"))
        except ValueError:
            _logger.warning("Claude SDK returned invalid per-model cost metadata")
            record = SDKModelUsage(actual_model, tokens)
        model_records.append(record)
    tracker.sdk_model_usage = tuple(model_records)


async def _interrupt(client: Any) -> None:
    try:
        await asyncio.wait_for(client.interrupt(), timeout=1.0)
    except Exception:
        _logger.debug("Claude Agent interrupt failed", exc_info=True)


async def _run_with_timeout(
    operation: Awaitable[_Awaited],
    *,
    timeout_seconds: float,
) -> _Awaited:
    try:
        return await asyncio.wait_for(operation, timeout=timeout_seconds)
    except AgentModelTimeoutError:
        raise
    except asyncio.TimeoutError as exc:
        raise AgentModelTimeoutError(
            "Claude Agent request deadline expired"
        ) from exc
