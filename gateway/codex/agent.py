from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FutureTimeout
from contextvars import copy_context
from threading import Lock
from time import monotonic
from types import SimpleNamespace
from typing import Any, Literal, Protocol, TypeVar

from langchain_core.tools import BaseTool
from openai_codex import ImageInput as CodexImageInput, RunInput, TextInput as CodexTextInput
from pydantic import BaseModel, ValidationError

from gateway.agent_model.errors import (
    AgentModelTimeoutError,
    MissingRequiredFunctionCallError,
    MissingStructuredQueryDraftError,
    ModelFailureError,
    ModelFailureReason,
    ModelInputTooLargeError,
)
from gateway.constants import CODEX_PROVIDER
from gateway.agent_model.context import (
    ContextCheckpoint,
    ContextWindowOverflow,
    ContextWindowPolicy,
    preflight_context,
)
from gateway.usage import track_model_call
from gateway.media import encode_media_file
from gateway.multimodal import (
    ImageInput, MultimodalInput, TextInput, VideoInput, validate_generate_request,
)

from .client import CodexModelCallLimitError, CodexSession
from .protocol import dynamic_tool_specs, strict_output_schema


_ResultModel = TypeVar("_ResultModel", bound=BaseModel)
ReasoningEffort = Literal["none", "low", "high", "max"]

# A minimal live turn with pinned openai-codex 0.156.1 reports roughly 12,900
# input tokens before AniVAG's variable prompt and tool contract are considered.
# Round upward and recalibrate this adapter reserve when upgrading openai-codex.
_CODEX_BASE_INPUT_RESERVE_TOKENS = 13_000


class CodexAgentModelError(ModelFailureError):
    """Codex failed or returned an invalid agent result."""


class SessionLike(Protocol):
    @property
    def tool_call_count(self) -> int: ...

    @property
    def successful_tool_call_count(self) -> int: ...

    def run_turn(
        self,
        prompt: RunInput,
        *,
        output_schema: dict[str, Any] | None = None,
    ) -> object: ...

    def interrupt(self) -> None: ...

    def close(self) -> None: ...


SessionFactory = Callable[..., SessionLike]


def _turn_field(turn: object, name: str, default: Any = None) -> Any:
    if isinstance(turn, dict):
        return turn.get(name, default)
    return getattr(turn, name, default)


def _failure_reason(error: BaseException) -> ModelFailureReason:
    reason = getattr(error, "reason", None)
    if isinstance(reason, ModelFailureReason):
        return reason
    if isinstance(error, TimeoutError) or "timeout" in type(error).__name__.lower():
        return ModelFailureReason.MODEL_TIMEOUT
    return ModelFailureReason.INVALID_MODEL_RESPONSE


class CodexAgentModel:
    """Adapt the local Codex app-server to AniVAG's two Agent model protocols."""

    provider_name = CODEX_PROVIDER
    supports_context_checkpoints = False
    supports_context_messages = False
    supports_stop_feedback = True

    def __init__(
        self,
        *,
        model: str,
        reasoning_effort: ReasoningEffort = "none",
        request_timeout_seconds: float,
        max_output_tokens: int,
        max_input_chars: int | None,
        context_window_policy: ContextWindowPolicy | None = None,
        codex_bin: str | None = None,
        max_concurrency: int = 4,
        cwd: str | None = None,
        executor: ThreadPoolExecutor | None = None,
        session_factory: SessionFactory | None = None,
        total_timeout_enabled: bool = True,
    ) -> None:
        if not model.strip():
            raise ValueError("model must not be empty")
        if reasoning_effort not in {"none", "low", "high", "max"}:
            raise ValueError("unsupported reasoning_effort")
        if request_timeout_seconds <= 0:
            raise ValueError("request_timeout_seconds must be greater than zero")
        if not isinstance(total_timeout_enabled, bool):
            raise ValueError("total_timeout_enabled must be a boolean")
        if max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be greater than zero")
        if max_input_chars is not None and (
            isinstance(max_input_chars, bool)
            or not isinstance(max_input_chars, int)
            or max_input_chars <= 0
        ):
            raise ValueError("max_input_chars must be positive when provided")
        if isinstance(max_concurrency, bool) or max_concurrency <= 0:
            raise ValueError("max_concurrency must be a positive integer")
        if codex_bin is not None and not codex_bin.strip():
            raise ValueError("codex_bin must not be empty when provided")
        if cwd is not None and not cwd.strip():
            raise ValueError("cwd must not be empty when provided")

        self.model_name = model
        self.reasoning_effort = reasoning_effort
        self.request_timeout_seconds = request_timeout_seconds
        self.total_timeout_enabled = total_timeout_enabled
        self.max_output_tokens = max_output_tokens
        self.max_input_chars = max_input_chars
        self.context_window_policy = context_window_policy
        self.codex_bin = codex_bin
        self.max_concurrency = max_concurrency
        self.cwd = cwd
        self._session_factory = session_factory or CodexSession
        self._owns_executor = executor is None
        self._executor = executor or ThreadPoolExecutor(
            max_workers=max_concurrency,
            thread_name_prefix="anivag-codex",
        )
        self._sessions: set[SessionLike] = set()
        self._closed = False
        self._lock = Lock()

    def generate(
        self,
        inputs: Sequence[MultimodalInput],
        *,
        schema_name: str,
        result_type: type[_ResultModel],
        tools: Sequence[BaseTool] = (),
        max_model_calls: int | None = None,
        request_timeout_seconds: float | None = None,
        check_control: Callable[[], None] | None = None,
        context_checkpoint: ContextCheckpoint | None = None,
        context_window_policy: ContextWindowPolicy | None = None,
        max_input_chars: int | None = None,
    ) -> _ResultModel:
        """Run native Codex with real images and host-owned business tools."""
        del context_checkpoint
        validate_generate_request(inputs, schema_name)
        self._validate_call_limit(max_model_calls)
        if check_control is not None:
            check_control()
        sdk_inputs = []
        content = []
        for item in inputs:
            if isinstance(item, VideoInput):
                raise ValueError("Codex requires ordered ImageInput frames, not VideoInput")
            if isinstance(item, TextInput):
                sdk_inputs.append(CodexTextInput(item.text))
                content.append({"type": "input_text", "text": item.text})
            elif isinstance(item, ImageInput):
                url, _ = encode_media_file(item.path, expected_kind="image")
                sdk_inputs.append(CodexImageInput(url))
                content.append({"type": "input_image", "image_url": url})
            else:
                raise TypeError(f"unsupported Codex input: {type(item).__name__}")
        if max_input_chars is not None and (
            isinstance(max_input_chars, bool)
            or not isinstance(max_input_chars, int)
            or max_input_chars <= 0
        ):
            raise ValueError("max_input_chars must be positive when provided")
        input_limit = (
            min(self.max_input_chars, max_input_chars)
            if self.max_input_chars is not None and max_input_chars is not None
            else self.max_input_chars or max_input_chars
        )
        output_schema = strict_output_schema(result_type.model_json_schema())
        self._preflight_context(
            "", tools=tools, output_schema=output_schema, content=content,
            policy=context_window_policy, max_input_chars=input_limit,
        )
        return self._run_structured_invocation(
            sdk_inputs, tools=tools, result_type=result_type,
            output_schema=output_schema, max_model_calls=max_model_calls,
            timeout=self._effective_timeout(request_timeout_seconds),
            check_control=check_control, require_successful_tool=bool(tools),
            operation=self._operation(schema_name, tools),
        )

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
    ) -> _ResultModel:
        del context_checkpoint
        rendered_prompt = self._validated_prompt(
            prompt,
            initial_messages=initial_messages,
            max_input_chars=max_input_chars,
        )
        output_schema = strict_output_schema(result_type.model_json_schema())
        self._preflight_context(
            rendered_prompt,
            tools=tools,
            output_schema=output_schema,
        )
        self._validate_call_limit(max_model_calls)
        timeout = self._effective_timeout(request_timeout_seconds)
        return self._run_structured_invocation(
            rendered_prompt, tools=tools, result_type=result_type,
            output_schema=output_schema, max_model_calls=max_model_calls,
            timeout=timeout, check_control=check_control,
        )

    def _run_structured_invocation(
        self,
        prompt: RunInput,
        *,
        tools: Sequence[BaseTool],
        result_type: type[_ResultModel],
        output_schema: dict[str, Any],
        max_model_calls: int | None,
        timeout: float,
        check_control: Callable[[], None] | None,
        require_successful_tool: bool = False,
        operation: str | None = None,
    ) -> _ResultModel:
        if check_control is not None:
            check_control()
        session = self._new_session(
            tools=tools,
            max_model_calls=max_model_calls,
            check_control=check_control,
        )
        latest_turn: object | None = None

        def execute() -> tuple[object, _ResultModel]:
            nonlocal latest_turn
            try:
                turn = session.run_turn(
                    prompt,
                    output_schema=output_schema,
                )
                latest_turn = turn
                if require_successful_tool and session.successful_tool_call_count == 0:
                    if max_model_calls is not None and max_model_calls <= 1:
                        raise MissingRequiredFunctionCallError(
                            "Codex completed before a business tool succeeded",
                            reason=ModelFailureReason.LOOP_LIMIT,
                        )
                    turn = session.run_turn(
                        "Successfully use the supplied business tools before completing. "
                        "Inspect failed receipts and correct recoverable errors; do not "
                        "repeat successful writes. Then return the structured result.",
                        output_schema=output_schema,
                    )
                    latest_turn = turn
                    if session.successful_tool_call_count == 0:
                        raise MissingRequiredFunctionCallError(
                            "Codex completed twice without a successful business tool"
                        )
                response = _turn_field(turn, "final_response")
                if not isinstance(response, str) or not response.strip():
                    raise MissingStructuredQueryDraftError(
                        "Codex ended without its required structured result",
                        reason=ModelFailureReason.INVALID_MODEL_RESPONSE,
                    )
                try:
                    value = result_type.model_validate_json(response)
                except (ValidationError, ValueError, json.JSONDecodeError) as exc:
                    raise MissingStructuredQueryDraftError(
                        "Codex returned an invalid structured result",
                        reason=ModelFailureReason.INVALID_MODEL_RESPONSE,
                    ) from exc
                return turn, value
            finally:
                session.close()

        turn, value = self._run_invocation(
            session,
            execute,
            timeout=timeout,
            check_control=check_control,
            operation=operation or self._operation("structured", tools),
            latest_turn=lambda: latest_turn,
        )
        del turn
        return value

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
    ) -> None:
        del context_checkpoint
        rendered_prompt = self._validated_prompt(
            prompt,
            initial_messages=initial_messages,
            max_input_chars=max_input_chars,
        )
        self._preflight_context(rendered_prompt, tools=tools)
        self._validate_call_limit(max_model_calls)
        timeout = self._effective_timeout(request_timeout_seconds)
        if check_control is not None:
            check_control()
        session = self._new_session(
            tools=tools,
            max_model_calls=max_model_calls,
            check_control=check_control,
        )
        latest_turn: object | None = None

        def execute() -> object:
            nonlocal latest_turn
            try:
                turn_count = 1
                turn = session.run_turn(rendered_prompt)
                latest_turn = turn
                if require_tool_call and session.tool_call_count == 0:
                    if max_model_calls is not None and turn_count >= max_model_calls:
                        raise MissingRequiredFunctionCallError(
                            "Codex stopped without the required tool call",
                            reason=ModelFailureReason.LOOP_LIMIT,
                        )
                    turn_count += 1
                    turn = session.run_turn(
                        "请先调用一个可用工具，再根据工具结果完成回答。"
                    )
                    latest_turn = turn
                    if session.tool_call_count == 0:
                        raise MissingRequiredFunctionCallError(
                            "Codex stopped twice without the required tool call"
                        )

                while True:
                    response = _turn_field(turn, "final_response")
                    if not isinstance(response, str) or not response.strip():
                        raise CodexAgentModelError(
                            "Codex agent loop completed without visible text"
                        )
                    handle_text(response)
                    feedback = (
                        stop_feedback() if stop_feedback is not None else None
                    )
                    if not feedback:
                        return turn
                    if max_model_calls is not None and turn_count >= max_model_calls:
                        raise CodexAgentModelError(
                            "Codex agent loop exhausted max_model_calls before stop",
                            reason=ModelFailureReason.LOOP_LIMIT,
                        )
                    turn_count += 1
                    turn = session.run_turn(feedback[:500])
                    latest_turn = turn
            finally:
                session.close()

        self._run_invocation(
            session,
            execute,
            timeout=timeout,
            check_control=check_control,
            operation=self._operation("agent_loop", tools),
            latest_turn=lambda: latest_turn,
        )

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            sessions = tuple(self._sessions)
            self._sessions.clear()
        for session in sessions:
            session.close()
        if self._owns_executor:
            self._executor.shutdown(wait=False, cancel_futures=True)

    def _new_session(
        self,
        *,
        tools: Sequence[BaseTool],
        max_model_calls: int | None,
        check_control: Callable[[], None] | None,
    ) -> SessionLike:
        session = self._session_factory(
            model=self.model_name,
            tools=tools,
            reasoning_effort=self.reasoning_effort,
            codex_bin=self.codex_bin,
            cwd=self.cwd,
            # max_model_calls also reserves continuations after dynamic tools.
            # Server-owned tool limits remain a separate concern.
            max_tool_calls=None,
            max_model_calls=max_model_calls,
            check_control=check_control,
        )
        with self._lock:
            if self._closed:
                session.close()
                raise RuntimeError("Codex agent model is closed")
            self._sessions.add(session)
        return session

    def _run_invocation(
        self,
        session: SessionLike,
        execute: Callable[[], Any],
        *,
        timeout: float,
        check_control: Callable[[], None] | None,
        operation: str,
        latest_turn: Callable[[], object | None],
    ) -> Any:
        deadline = monotonic() + timeout if self.total_timeout_enabled else None
        future: Future[Any] | None = None
        try:
            with track_model_call(
                provider=self.provider_name,
                model=self.model_name,
                operation=operation,
                granularity="sdk_turn_run",
                usage_source="codex_thread_total",
            ) as usage_call:
                future = self._executor.submit(copy_context().run, execute)
                unregister_cancel = usage_call.add_cancel_callback(
                    lambda: self._cancel_session(session, future),
                )
                try:
                    while True:
                        usage_call.raise_if_cancelled()
                        if check_control is not None:
                            try:
                                check_control()
                            except Exception:
                                self._cancel_session(session, future)
                                raise
                        remaining = deadline - monotonic() if deadline is not None else None
                        if remaining is not None and remaining <= 0:
                            self._cancel_session(session, future)
                            raise AgentModelTimeoutError(
                                "Codex Agent execution deadline expired"
                            )
                        try:
                            result = future.result(
                                timeout=min(remaining, 0.05) if remaining is not None else 0.05,
                            )
                            break
                        except FutureTimeout:
                            continue
                        except ModelFailureError:
                            raise
                        except CodexModelCallLimitError as exc:
                            raise CodexAgentModelError(
                                "Codex agent exhausted max_model_calls",
                                reason=ModelFailureReason.LOOP_LIMIT,
                            ) from exc
                        except Exception as exc:
                            raise CodexAgentModelError(
                                f"Codex request failed for model {self.model_name}",
                                reason=_failure_reason(exc),
                            ) from exc
                    usage_call.raise_if_cancelled()
                    return result
                finally:
                    unregister_cancel()
                    turn = latest_turn()
                    if turn is not None:
                        usage_call.capture_response(SimpleNamespace(
                            model=self.model_name,
                            usage=self._turn_usage(turn),
                        ))
        finally:
            if future is None:
                session.close()
            with self._lock:
                self._sessions.discard(session)

    @staticmethod
    def _turn_usage(turn: object) -> object | None:
        usage = _turn_field(turn, "usage")
        if usage is None:
            return None
        total = _turn_field(usage, "total", usage)
        input_tokens = _turn_field(total, "input_tokens")
        output_tokens = _turn_field(total, "output_tokens")
        total_tokens = _turn_field(total, "total_tokens")
        if any(value is None for value in (input_tokens, output_tokens, total_tokens)):
            return total
        return {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
            "input_tokens_details": {
                "cached_tokens": _turn_field(total, "cached_input_tokens"),
                "cache_write_tokens": _turn_field(total, "cache_write_input_tokens"),
            },
            "output_tokens_details": {
                "reasoning_tokens": _turn_field(
                    total,
                    "reasoning_output_tokens",
                ),
            },
        }

    @staticmethod
    def _cancel_session(session: SessionLike, future: Future[Any]) -> None:
        # Dynamic tools run synchronously on CodexClient's sole reader thread.
        # Waiting for turn/interrupt here can deadlock because that same reader
        # must receive the interrupt response. Closing the client transport
        # first terminates app-server and makes the waiting turn fail promptly.
        session.close()
        future.cancel()

    def _validated_prompt(
        self,
        prompt: str,
        *,
        initial_messages: Sequence[dict[str, Any]] | None,
        max_input_chars: int | None,
    ) -> str:
        if not prompt.strip():
            raise ValueError("Agent prompt must not be empty")
        if initial_messages is not None and not initial_messages:
            raise ValueError("initial_messages must not be empty when provided")
        if max_input_chars is not None and (
            isinstance(max_input_chars, bool)
            or not isinstance(max_input_chars, int)
            or max_input_chars <= 0
        ):
            raise ValueError("max_input_chars must be positive when provided")

        rendered = prompt
        if initial_messages is not None:
            rendered = (
                "Existing conversation messages (JSON):\n"
                + json.dumps(initial_messages, ensure_ascii=False, default=str)
                + "\n\nCurrent instruction:\n"
                + prompt
            )
        effective_limit = (
            min(self.max_input_chars, max_input_chars)
            if self.max_input_chars is not None and max_input_chars is not None
            else self.max_input_chars or max_input_chars
        )
        if effective_limit is not None and len(rendered) > effective_limit:
            raise ModelInputTooLargeError(
                "Codex Agent prompt exceeded max_input_chars"
            )
        return rendered

    def _effective_timeout(self, value: float | None) -> float:
        timeout = self.request_timeout_seconds if value is None else value
        if timeout <= 0:
            raise ValueError("request_timeout_seconds must be greater than zero")
        return min(timeout, self.request_timeout_seconds)

    def _preflight_context(
        self,
        prompt: str,
        *,
        tools: Sequence[BaseTool],
        output_schema: dict[str, Any] | None = None,
        content: list[dict[str, Any]] | None = None,
        policy: ContextWindowPolicy | None = None,
        max_input_chars: int | None = None,
    ) -> None:
        contract: dict[str, Any] = {
            "dynamicTools": dynamic_tool_specs(tools),
        }
        if output_schema is not None:
            contract["outputSchema"] = output_schema
        try:
            preflight_context(
                [{"role": "user", "content": content if content is not None else prompt}],
                contract,
                policy=policy if policy is not None else self.context_window_policy,
                maximum_input_characters=max_input_chars,
                fixed_input_tokens=_CODEX_BASE_INPUT_RESERVE_TOKENS,
                label="Codex Agent",
            )
        except ContextWindowOverflow as exc:
            raise ModelInputTooLargeError(str(exc)) from exc

    @staticmethod
    def _validate_call_limit(max_model_calls: int | None) -> None:
        if max_model_calls is not None and (
            isinstance(max_model_calls, bool)
            or not isinstance(max_model_calls, int)
            or max_model_calls <= 0
        ):
            raise ValueError("max_model_calls must be a positive integer")

    @staticmethod
    def _operation(kind: str, tools: Sequence[BaseTool]) -> str:
        names = ",".join(tool.name for tool in tools)
        return f"{kind}:{names}" if names else kind
