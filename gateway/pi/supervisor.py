from __future__ import annotations

import base64
import json
from collections.abc import Callable, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from threading import Event, RLock
from types import SimpleNamespace
from typing import Any

from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool
from pydantic import BaseModel

from gateway.agent_model.contracts import AgentModelProfile, SupervisorSessionContext
from gateway.agent_model.errors import AgentModelTimeoutError, ModelFailureError, ModelFailureReason, ModelInputTooLargeError
from gateway.media import ToolImageArtifact, prepare_input_messages, require_vision_input
from gateway.schema_normalization import inline_schema_refs
from gateway.usage import track_model_call

from .client import PiClient, PiHostError


@dataclass
class _PendingTurn:
    message_id: str
    context: SupervisorSessionContext | None
    started: bool = False
    cancelled: Event = field(default_factory=Event)


class PiSupervisorModel:
    """Adapt the existing business commit contract to a borrowed Pi Host."""

    session_key = "pi"

    def __init__(
        self, *, client: PiClient, profile: AgentModelProfile,
        request_timeout_seconds: float, max_input_chars: int,
        total_timeout_enabled: bool = True,
    ) -> None:
        self._client, self._profile = client, profile
        self.model_name, self.provider_name = profile.model, profile.provider
        self.supports_vision_input = profile.supports_vision_input
        self._timeout, self._max_input_chars = request_timeout_seconds, max_input_chars
        self._total_timeout_enabled = total_timeout_enabled
        self._pending: dict[str, _PendingTurn] = {}
        self._lock = RLock()
        self._closed = False

    def run_agent(
        self, prompt: str, *, thread_id: str, message_id: str, user_message: str,
        history: Sequence[dict[str, Any]], tools: Sequence[BaseTool],
        max_model_calls: int | None, handle_text: Callable[[str], None],
        request_timeout_seconds: float | None = None,
        check_control: Callable[[], None] | None = None,
        max_input_chars: int | None = None, image_urls: Sequence[str] = (),
        session_context: SupervisorSessionContext | None = None,
    ) -> None:
        if not prompt.strip() or not thread_id or not message_id or not (user_message.strip() or image_urls):
            raise ValueError("prompt, thread_id, message_id and user_message are required")
        if max_model_calls is not None and max_model_calls <= 0:
            raise ValueError("max_model_calls must be positive")
        with self._lock:
            if self._closed:
                raise RuntimeError("Pi Supervisor is closed")
            if thread_id in self._pending:
                raise RuntimeError("Supervisor previous turn has not been confirmed")
            pending = _PendingTurn(message_id, session_context)
            self._pending[thread_id] = pending

        profile = (session_context.model_profile if session_context is not None else None) or self._profile
        timeout = min(self._timeout, request_timeout_seconds or self._timeout)
        character_limit = min(self._max_input_chars, max_input_chars or self._max_input_chars)
        latest_usage: dict[str, Any] | None = None
        try:
            if check_control is not None:
                check_control()
            source_history = session_context.load_history(-1) if session_context is not None else history
            messages: list[dict[str, Any]] = []
            seen: set[str] = set()
            for index, item in enumerate(source_history):
                identity = str(item.get("messageId") or item.get("id") or index)
                role, status = str(item.get("role", "")).lower(), str(item.get("status", "COMPLETED")).upper()
                if identity in seen or role not in {"user", "assistant"} or status != "COMPLETED":
                    continue
                content = item.get("content")
                if not isinstance(content, (str, list)):
                    continue
                seen.add(identity)
                messages.append({"role": role, "content": content, **(
                    {"imageUrls": item["imageUrls"]} if role == "user" and item.get("imageUrls") else {}
                )})
            messages = prepare_input_messages("", initial_messages=messages) if messages else []
            current = prepare_input_messages(user_message, image_urls=image_urls)
            require_vision_input(
                [*messages, *current], supports_vision_input=profile.supports_vision_input,
                model_name=profile.model,
            )
            by_name = {tool.name: tool for tool in tools}
            if len(by_name) != len(tools) or any(not name.strip() for name in by_name):
                raise ValueError("tool names must be nonempty and unique")
            specs = [{
                "name": tool.name, "description": tool.description or "",
                "inputSchema": inline_schema_refs(tool.get_input_schema().model_json_schema()),
            } for tool in tools]

            with track_model_call(
                provider=profile.provider, model=profile.model, operation="agent_loop",
                granularity="sdk_turn_run", usage_source="pi_run_total",
            ) as usage_call:
                def check() -> None:
                    if pending.cancelled.is_set():
                        raise RuntimeError("Pi Supervisor turn was closed")
                    usage_call.raise_if_cancelled()
                    if check_control is not None:
                        check_control()

                def event(value: dict[str, Any]) -> None:
                    nonlocal latest_usage
                    if value.get("type") == "usage":
                        latest_usage = value.get("usage")
                        return
                    check()
                    if value.get("type") == "text_delta":
                        handle_text(str(value.get("delta", "")))
                    elif value.get("type") == "state" and session_context is not None:
                        session_context.save_state(self.session_key, deepcopy(value["state"]))

                def call_tool(name: str, arguments: dict, tool_call_id: str) -> dict:
                    check()
                    if name not in by_name:
                        return {"content": [{"type": "text", "text": "Unknown business tool."}], "isError": True}
                    try:
                        output = by_name[name].invoke({
                            "type": "tool_call", "id": tool_call_id, "name": name, "args": arguments,
                        })
                        if not isinstance(output, ToolMessage) or output.tool_call_id != tool_call_id:
                            raise TypeError("Business tool must return a matching ToolMessage")
                        content = output.content
                        if isinstance(content, BaseModel):
                            content = content.model_dump(mode="json")
                        text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False, default=str)
                        blocks: list[dict[str, Any]] = [{"type": "text", "text": text}]
                        if output.status != "error" and isinstance(output.artifact, ToolImageArtifact):
                            if not profile.supports_vision_input:
                                raise ValueError("Selected model does not support tool image input")
                            for image in output.artifact.images:
                                header, encoded = image.data_url.split(",", 1)
                                if not header.startswith("data:image/") or not header.endswith(";base64"):
                                    raise ValueError("Tool images must be base64 image data URLs")
                                base64.b64decode(encoded, validate=True)
                                if image.description:
                                    blocks.append({"type": "text", "text": image.description})
                                blocks.append({"type": "image", "data": encoded, "mimeType": header[5:-7]})
                        check()
                        return {"content": blocks, "isError": output.status == "error"}
                    except Exception:
                        check()
                        raise

                unregister = usage_call.add_cancel_callback(lambda: self._client.request(
                    "abort", session_id=thread_id, run_id=message_id,
                ))
                try:
                    with self._lock:
                        if self._closed or self._pending.get(thread_id) is not pending:
                            raise RuntimeError("Pi Supervisor is closed")
                        pending.started = True
                    result = self._client.prompt(
                        session_id=thread_id, run_id=message_id,
                        profile={
                            "id": profile.id, "revision": profile.revision, "provider": profile.provider,
                            "model": profile.model, "apiKey": profile.api_key, "baseUrl": profile.base_url,
                            "supportsVision": profile.supports_vision_input,
                            "maxOutputTokens": min(profile.max_output_tokens, self._profile.max_output_tokens),
                            "contextWindowTokens": profile.context_window_tokens,
                            "reasoningEffort": profile.reasoning_effort,
                        },
                        message=user_message, history=messages, system_prompt=prompt, tools=specs,
                        state=deepcopy(session_context.states.get(self.session_key, {})) if session_context else None,
                        limits={"maxModelCalls": max_model_calls, "maxInputChars": character_limit,
                                "timeoutSeconds": timeout if self._total_timeout_enabled else None},
                        images=[{"url": url} for url in image_urls], on_event=event, on_tool=call_tool,
                        check_control=check, timeout=timeout if self._total_timeout_enabled else None,
                    )
                    check()
                    latest_usage = result.get("usage") or latest_usage
                    if session_context is not None and isinstance(result.get("state"), dict):
                        session_context.save_state(self.session_key, deepcopy(result["state"]))
                finally:
                    unregister()
                    if latest_usage is not None and "input_tokens" not in latest_usage:
                        reads, writes = latest_usage.get("cacheRead", 0), latest_usage.get("cacheWrite", 0)
                        input_tokens = latest_usage.get("input", 0) + reads + writes
                        output_tokens = latest_usage.get("output", 0)
                        latest_usage = {
                            "input_tokens": input_tokens, "output_tokens": output_tokens,
                            "total_tokens": input_tokens + output_tokens,
                            "input_tokens_details": {"cached_tokens": reads, "cache_write_tokens": writes},
                        }
                    usage_call.capture_response(SimpleNamespace(model=profile.model, usage=latest_usage))
        except BaseException as error:
            self.discard_turn(thread_id, message_id)
            if isinstance(error, PiHostError):
                if error.code in {"input_too_large", "session_limit"}:
                    raise ModelInputTooLargeError("Pi context exceeds the configured input budget") from error
                if error.code in {"timeout", "TIMEOUT"}:
                    raise AgentModelTimeoutError("Pi execution deadline expired") from error
                reason = {
                    "model_call_limit": ModelFailureReason.LOOP_LIMIT,
                    "LOOP_LIMIT": ModelFailureReason.LOOP_LIMIT,
                    "output_token_limit": ModelFailureReason.OUTPUT_TOKEN_LIMIT,
                }.get(error.code)
                raise ModelFailureError(f"Pi execution failed ({error.code})", reason=reason) from error
            raise

    def confirm_turn(self, thread_id: str, message_id: str, content: str) -> None:
        with self._lock:
            pending = self._pending.get(thread_id)
            if pending is None or pending.message_id != message_id:
                return
            result = self._client.request("confirm_turn", session_id=thread_id, run_id=message_id, content=content)
            if pending.context is not None and isinstance(result.get("state"), dict):
                pending.context.save_state(self.session_key, deepcopy(result["state"]))
            self._pending.pop(thread_id, None)

    def discard_turn(self, thread_id: str, message_id: str) -> None:
        with self._lock:
            pending = self._pending.get(thread_id)
            if pending is None or pending.message_id != message_id:
                return
            self._pending.pop(thread_id, None)
        if not pending.started:
            return
        try:
            self._client.request("discard_turn", session_id=thread_id, run_id=message_id)
        except PiHostError:
            # SQL remains authoritative; the next load reconciles its saved pending state.
            pass

    def delete_thread(self, thread_id: str) -> None:
        with self._lock:
            self._pending.pop(thread_id, None)
        self._client.request("delete_thread", session_id=thread_id)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            active, self._pending = self._pending, {}
            for pending in active.values():
                pending.cancelled.set()
        for thread_id, pending in active.items():
            if not pending.started:
                continue
            try:
                self._client.request("abort", session_id=thread_id, run_id=pending.message_id)
            except PiHostError:
                pass
