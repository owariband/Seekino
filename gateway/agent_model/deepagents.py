from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from contextlib import ExitStack
from dataclasses import dataclass
from threading import Lock, RLock
from time import monotonic
from typing import Any

from deepagents import (
    GeneralPurposeSubagentProfile,
    HarnessProfile,
    create_deep_agent,
    register_harness_profile,
)
from deepagents.backends import StateBackend
from deepagents.middleware.filesystem import FilesystemMiddleware
from deepagents.middleware.summarization import DEEPAGENTS_DEFAULT_SUMMARY_PROMPT, SummarizationMiddleware
from langchain.agents.middleware import AgentMiddleware
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.exceptions import ContextOverflowError
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    ToolMessage,
    convert_to_openai_messages,
)
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import BaseTool
from langgraph.checkpoint.memory import InMemorySaver
from openai import OpenAI
from pydantic import PrivateAttr

from gateway.ark.agent import (
    ArkFunctionCallingModel,
    new_ark_client,
    request_ark_function_turn,
)
from gateway.media import ToolImageArtifact, text_only_messages
from gateway.openai.agent import OpenAIAgentModel
from gateway.usage import capture_model_usage

from .context import ContextWindowPolicy, measure_context
from .contracts import AgentLoopModel
from .errors import (
    AgentModelTimeoutError,
    ArkFunctionCallingError,
    ModelFailureError,
    ModelFailureReason,
    ModelInputTooLargeError,
    OpenAIFunctionCallingError,
)
from .fallback import log_model_fallback
from .tool_loop import _copy_assistant_reasoning, _function_tool_spec

_logger = logging.getLogger(__name__)
_HARNESS_PROVIDER = "anivag_supervisor"

# The built-in Supervisor owns a virtual workspace. Business subagents retain
# their existing review/commit contracts and are exposed as scoped tools.
register_harness_profile(
    _HARNESS_PROVIDER,
    HarnessProfile(
        general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False),
    ),
)


def _count_context_tokens(messages: Sequence[BaseMessage], *, tools: Sequence[Any] | None = None) -> int:
    rendered = convert_to_openai_messages(list(messages))
    payload = [rendered] if isinstance(rendered, dict) else rendered
    specs = [_function_tool_spec(tool) if isinstance(tool, BaseTool) else tool for tool in tools or []]
    return measure_context(payload, specs).estimated_tokens


@dataclass
class _PendingTurn:
    message_id: str
    graph: Any


class DeepAgentSessionStore:
    """Process-local SDK state shared by models and configuration reloads."""

    def __init__(self) -> None:
        self.checkpointer = InMemorySaver()
        self.pending: dict[str, _PendingTurn] = {}
        self._locks: dict[str, RLock] = {}
        self._lock = Lock()

    def thread_lock(self, thread_id: str) -> RLock:
        with self._lock:
            return self._locks.setdefault(thread_id, RLock())

    def delete_thread(self, thread_id: str) -> None:
        with self.thread_lock(thread_id):
            self.checkpointer.delete_thread(thread_id)
            self.pending.pop(thread_id, None)

    def close(self) -> None:
        for thread_id in list(self.checkpointer.storage):
            self.delete_thread(thread_id)
        with self._lock:
            self.pending.clear()
            self._locks.clear()


class _GatewayChatModel(BaseChatModel):
    """Expose existing provider requests to the SDK without owning its loop."""

    model_name: str
    _adapter: Any = PrivateAttr(default=None)
    _delegate: BaseChatModel | None = PrivateAttr(default=None)
    _stack: ExitStack = PrivateAttr()
    _check: Callable[[], None] = PrivateAttr()
    _remaining: Callable[[], float] = PrivateAttr()
    _max_input_chars: int | None = PrivateAttr(default=None)
    _maximum_input_tokens: int | None = PrivateAttr(default=None)
    _client: Any = PrivateAttr(default=None)
    _fallback_client: Any = PrivateAttr(default=None)
    _fallback_active: bool = PrivateAttr(default=False)
    _seen_tool_ids: set[str] = PrivateAttr(default_factory=set)
    _max_model_calls: int | None = PrivateAttr(default=None)
    _model_calls: int = PrivateAttr(default=0)
    _invalid_tool_messages: dict[str, ToolMessage] = PrivateAttr(default_factory=dict)

    @property
    def _llm_type(self) -> str:
        return _HARNESS_PROVIDER

    def _get_ls_params(self, **kwargs: Any) -> dict[str, Any]:
        provider = getattr(self._adapter, "provider_name", _HARNESS_PROVIDER)
        return {"ls_provider": provider, "ls_model_name": self.model_name}

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> Any:
        return self.bind(tools=list(tools), **kwargs)

    def get_num_tokens_from_messages(self, messages: list[BaseMessage], **kwargs: Any) -> int:
        return _count_context_tokens(messages, tools=kwargs.get("tools"))

    def _generate(self, messages: list[BaseMessage], stop: Any = None, run_manager: Any = None, **kwargs: Any) -> ChatResult:
        self._check()
        timeout = min(float(kwargs.get("timeout") or self._remaining()), self._remaining())
        tools = list(kwargs.get("tools") or [])
        specs = [
            _function_tool_spec(tool) if isinstance(tool, BaseTool) else tool
            for tool in tools
        ]
        rendered = convert_to_openai_messages(messages)
        payload = [rendered] if isinstance(rendered, dict) else rendered
        _copy_assistant_reasoning(messages, payload)
        measurement = measure_context(payload, specs)
        if self._max_input_chars is not None and measurement.characters > self._max_input_chars:
            raise ContextOverflowError(
                f"Supervisor input has {measurement.characters} characters; limit is {self._max_input_chars}"
            )
        if self._maximum_input_tokens is not None and measurement.estimated_tokens >= self._maximum_input_tokens:
            raise ContextOverflowError(
                f"Supervisor input has {measurement.estimated_tokens} estimated tokens; limit is {self._maximum_input_tokens}"
            )
        if self._max_model_calls is not None and self._model_calls >= self._max_model_calls:
            raise ModelFailureError("Supervisor exhausted max_model_calls", reason=ModelFailureReason.LOOP_LIMIT)
        self._model_calls += 1
        if self._delegate is not None:
            result = self._delegate._generate(
                messages, stop=stop, run_manager=run_manager, **{**kwargs, "timeout": timeout},
            )
            self._check()
            return result
        adapter = self._adapter

        def request(*, fallback: bool, request_timeout: float) -> Any:
            self._check()
            if isinstance(adapter, OpenAIAgentModel):
                if fallback:
                    provider = adapter._fallback_adapter
                    if self._fallback_client is None:
                        self._fallback_client = provider.acquire_client(self._stack)
                    return provider.request_turn(
                        self._fallback_client, tools=tools,
                        messages=(payload if provider.supports_vision_input else text_only_messages(payload)),
                        tool_specs=specs or None, tool_choice="auto" if specs else None, timeout=request_timeout,
                        seen_tool_call_ids=self._seen_tool_ids,
                        require_visible_text_on_stop=False,
                    )
                if self._client is None:
                    self._client = adapter._client
                    if self._client is None:
                        self._client = OpenAI(
                            api_key=adapter._api_key, base_url=adapter.base_url,
                            timeout=adapter._request_timeout_seconds, max_retries=0,
                        )
                        self._stack.callback(self._client.close)
                return adapter._request_turn(
                    self._client, model_name=adapter.model_name, tools=tools,
                    messages=(payload if adapter.supports_vision_input else text_only_messages(payload)),
                    tool_specs=specs or None, tool_choice="auto" if specs else None,
                    timeout=min(request_timeout, adapter._request_timeout_seconds),
                    seen_tool_call_ids=self._seen_tool_ids,
                    require_visible_text_on_stop=False,
                )
            if isinstance(adapter, ArkFunctionCallingModel):
                if self._client is None:
                    self._client = adapter._client
                    if self._client is None:
                        self._client = new_ark_client(
                            request_timeout_seconds=adapter._request_timeout_seconds,
                            api_key=adapter._api_key,
                        )
                        self._stack.callback(self._client.close)
                vision = adapter._fallback_supports_vision_input if fallback else adapter.supports_vision_input
                return request_ark_function_turn(
                    self._client,
                    model_name=adapter.fallback_model_name if fallback else adapter.model_name,
                    max_output_tokens=(adapter._fallback_max_output_tokens if fallback else adapter.max_output_tokens),
                    tools=tools, messages=payload if vision else text_only_messages(payload),
                    tool_specs=specs or None, tool_choice="auto" if specs else None,
                    timeout=min(request_timeout, adapter._fallback_request_timeout_seconds if fallback else adapter._request_timeout_seconds),
                    seen_tool_call_ids=self._seen_tool_ids,
                    require_visible_text_on_stop=False,
                )
            raise TypeError("Deep Agents Supervisor requires an OpenAI-compatible or Ark API source")

        usage = []
        with capture_model_usage(usage.append, combine=True):
            started_at = monotonic()
            try:
                turn = request(fallback=self._fallback_active, request_timeout=timeout)
            except (OpenAIFunctionCallingError, ArkFunctionCallingError) as error:
                fallback_model = getattr(adapter, "fallback_model_name", None)
                if self._fallback_active or not fallback_model:
                    raise
                remaining = min(timeout - (monotonic() - started_at), self._remaining())
                if remaining <= 0:
                    raise AgentModelTimeoutError("Supervisor deadline expired before fallback") from error
                log_model_fallback(
                    _logger, primary_provider=adapter.provider_name, primary_model=adapter.model_name,
                    fallback_provider=getattr(adapter, "fallback_provider_name", None) or adapter.provider_name,
                    fallback_model=fallback_model, error=error,
                )
                self._fallback_active = True
                turn = request(fallback=True, request_timeout=remaining)
        self._check()
        assistant = turn[2]
        rendered_calls = {call["id"]: call for call in assistant.get("tool_calls", [])}
        tool_calls = []
        for identity, tool, arguments in turn[3]:
            if isinstance(arguments, ToolMessage):
                self._invalid_tool_messages[identity] = arguments
                arguments = {}
            tool_calls.append({
                "id": identity,
                "name": rendered_calls[identity]["function"]["name"],
                "args": arguments,
            })
        message = AIMessage(
            content=assistant.get("content") or "", tool_calls=tool_calls,
            additional_kwargs={"reasoning_content": assistant["reasoning_content"]}
            if "reasoning_content" in assistant else {},
        )
        if usage and usage[-1].usage is not None:
            tokens = usage[-1].usage
            message.usage_metadata = {
                "input_tokens": tokens.input_tokens,
                "output_tokens": tokens.output_tokens,
                "total_tokens": tokens.total_tokens,
            }
            message.response_metadata.update(model_provider=usage[-1].provider, model_name=usage[-1].model)
        return ChatResult(generations=[ChatGeneration(message=message)])


class _HostControlMiddleware(AgentMiddleware):
    def __init__(self, check: Callable[[], None], handle_text: Callable[[str], None], invalid_tool_messages: dict[str, ToolMessage]) -> None:
        self._check = check
        self._handle_text = handle_text
        self._tool_lock = Lock()
        self._images: list[HumanMessage] = []
        self._invalid_tool_messages = invalid_tool_messages

    def wrap_model_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        self._check()
        result = handler(request)
        self._check()
        return result

    def wrap_tool_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        # Limit business tool concurrency without limiting the graph executor:
        # DeltaChannel checkpoint writers also use that executor.
        with self._tool_lock:
            self._check()
            invalid = self._invalid_tool_messages.pop(request.tool_call["id"], None)
            if invalid is not None:
                return invalid
            result = handler(request)
            self._check()
            if isinstance(result, ToolMessage) and isinstance(result.artifact, ToolImageArtifact):
                self._images.append(HumanMessage(content=result.artifact.openai_content()))
                return result.model_copy(update={"artifact": None})
        return result

    def before_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        self._check()
        if not self._images:
            return None
        # All tool_call_ids must receive their tool replies before an image
        # user message is appended, including batches containing several tools.
        images, self._images = self._images, []
        return {"messages": images}

    def after_model(self, state: Any, runtime: Any) -> None:
        self._check()
        message = state["messages"][-1]
        if isinstance(message, AIMessage) and not message.tool_calls:
            if message.text:
                self._handle_text(message.text)


class DeepAgentsSupervisorModel:
    """Run the SDK harness, confirming its final text after business storage."""

    def __init__(
        self, *, session_store: DeepAgentSessionStore, model: str, provider_name: str,
        request_timeout_seconds: float, max_input_chars: int | None,
        context_window_policy: ContextWindowPolicy | None,
        supports_vision_input: bool = False, agent_model: AgentLoopModel | None = None,
        chat_model: BaseChatModel | None = None,
        total_timeout_enabled: bool = True,
    ) -> None:
        if not model.strip() or request_timeout_seconds <= 0:
            raise ValueError("model and positive request_timeout_seconds are required")
        if not isinstance(total_timeout_enabled, bool):
            raise ValueError("total_timeout_enabled must be a boolean")
        if (agent_model is None) == (chat_model is None):
            raise ValueError("provide exactly one of agent_model and chat_model")
        self.model_name = model
        self.provider_name = provider_name
        self.supports_vision_input = supports_vision_input
        self.session_store = session_store
        self._agent_model = agent_model
        self._chat_model = chat_model
        self._request_timeout_seconds = request_timeout_seconds
        self.total_timeout_enabled = total_timeout_enabled
        self._max_input_chars = max_input_chars
        self._context_window_policy = context_window_policy

    def run_agent(
        self, prompt: str, *, thread_id: str, message_id: str, user_message: str,
        history: Sequence[dict[str, Any]], tools: Sequence[BaseTool],
        max_model_calls: int | None, handle_text: Callable[[str], None],
        request_timeout_seconds: float | None = None,
        check_control: Callable[[], None] | None = None,
        max_input_chars: int | None = None,
    ) -> None:
        if not prompt.strip() or not thread_id or not message_id:
            raise ValueError("prompt, thread_id and message_id are required")
        if max_model_calls is not None and (isinstance(max_model_calls, bool) or not isinstance(max_model_calls, int) or max_model_calls <= 0):
            raise ValueError("max_model_calls must be positive when provided")
        timeout = self._request_timeout_seconds if request_timeout_seconds is None else request_timeout_seconds
        if timeout <= 0:
            raise ValueError("request_timeout_seconds must be positive")
        deadline = monotonic() + timeout if self.total_timeout_enabled else None

        def remaining() -> float:
            if deadline is None:
                return min(timeout, self._request_timeout_seconds)
            seconds = deadline - monotonic()
            if seconds <= 0:
                raise AgentModelTimeoutError("Supervisor execution deadline expired")
            return seconds

        def check() -> None:
            if check_control is not None:
                check_control()
            remaining()

        config = {"configurable": {"thread_id": thread_id}, "recursion_limit": 1_000_000}
        with self.session_store.thread_lock(thread_id), ExitStack() as stack:
            check()
            if thread_id in self.session_store.pending:
                raise RuntimeError("Supervisor previous turn has not been confirmed")
            model = _GatewayChatModel(model_name=f"{_HARNESS_PROVIDER}:{self.model_name}")
            model._adapter = self._agent_model
            model._delegate = self._chat_model
            model._stack = stack
            model._check = check
            model._remaining = remaining
            model._max_model_calls = max_model_calls
            model._max_input_chars = self._max_input_chars if max_input_chars is None else max_input_chars
            if self._context_window_policy is not None:
                model._maximum_input_tokens = self._context_window_policy.maximum_input_tokens
                model.profile = {
                    "max_input_tokens": self._context_window_policy.maximum_input_tokens,
                    "image_inputs": self.supports_vision_input,
                }
            backend = StateBackend()
            middleware: list[AgentMiddleware] = [
                FilesystemMiddleware(backend=backend, tools=["read_file"]),
                _HostControlMiddleware(check, handle_text, model._invalid_tool_messages),
            ]
            policy = self._context_window_policy
            if policy is not None:
                summary_prompt = HumanMessage(content=DEEPAGENTS_DEFAULT_SUMMARY_PROMPT)
                summary_budget = policy.maximum_input_tokens - _count_context_tokens([summary_prompt])
                if model._max_input_chars is not None:
                    summary_budget = min(summary_budget, (model._max_input_chars - len(summary_prompt.content)) // 3)
                middleware.append(SummarizationMiddleware(
                    model=model, backend=backend, trigger=("tokens", policy.auto_compact_tokens),
                    keep=("tokens", policy.compact_target_tokens), trim_tokens_to_summarize=max(1, summary_budget),
                    token_counter=_count_context_tokens,
                ))
            graph = create_deep_agent(
                model=model, tools=list(tools), system_prompt=prompt,
                middleware=middleware, backend=backend,
                checkpointer=self.session_store.checkpointer, name="anivag_supervisor",
            )
            state = graph.get_state(config)
            messages: list[BaseMessage] = []
            if not state.values:
                seen: set[str] = set()
                for index, item in enumerate(history):
                    role = str(item.get("role", "")).lower()
                    status = str(item.get("status", "COMPLETED")).upper()
                    content = item.get("content")
                    identity = str(item.get("messageId") or item.get("id") or f"history:{index}")
                    if role not in {"user", "assistant"} or status != "COMPLETED" or not isinstance(content, str) or identity in seen:
                        continue
                    seen.add(identity)
                    message_type = HumanMessage if role == "user" else AIMessage
                    messages.append(message_type(content=content, id=identity))
            messages.append(HumanMessage(content=user_message, id=f"{message_id}:user"))
            self.session_store.pending[thread_id] = _PendingTurn(message_id, graph)
            try:
                # Async durability preserves outer checkpoints without forcing sync
                # checkpoint waits on business graphs that disable their saver.
                graph.invoke({"messages": messages}, config, durability="async")
                check()
            except BaseException as error:
                self.session_store.delete_thread(thread_id)
                if isinstance(error, ContextOverflowError):
                    raise ModelInputTooLargeError(str(error)) from error
                raise

    def confirm_turn(self, thread_id: str, message_id: str, content: str) -> None:
        with self.session_store.thread_lock(thread_id):
            pending = self.session_store.pending.get(thread_id)
            if pending is None or pending.message_id != message_id:
                return
            config = {"configurable": {"thread_id": thread_id}}
            messages = pending.graph.get_state(config).values.get("messages", [])
            if not messages or not isinstance(messages[-1], AIMessage) or messages[-1].tool_calls:
                self.session_store.delete_thread(thread_id)
                raise RuntimeError("Supervisor turn has no completed assistant response")
            pending.graph.update_state(config, {"messages": [
                AIMessage(content=content, id=messages[-1].id),
            ]}, as_node="model")
            self.session_store.pending.pop(thread_id, None)

    def discard_turn(self, thread_id: str, message_id: str) -> None:
        with self.session_store.thread_lock(thread_id):
            pending = self.session_store.pending.get(thread_id)
            if pending is not None and pending.message_id == message_id:
                self.session_store.delete_thread(thread_id)

    def delete_thread(self, thread_id: str) -> None:
        self.session_store.delete_thread(thread_id)

    def close(self) -> None:
        close = getattr(self._agent_model, "close", None)
        if callable(close):
            close()


__all__ = ["DeepAgentSessionStore", "DeepAgentsSupervisorModel"]
