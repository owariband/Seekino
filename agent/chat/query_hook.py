from __future__ import annotations

import logging
from time import monotonic
from typing import Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, ConfigDict, Field, field_validator

from gateway.config import QueryHookConfig
from gateway.query_hook import QueryHookModelError, QueryHookModels
from gateway.usage import model_call_context

from .common.state import ExecutionControl, RunInputs, context_json
from .query_hook_prompts import ROUTE_QUESTION, SLOT_PROMPT, TEMPLATES, TURN_SCOPE

_logger = logging.getLogger(__name__)
Action = Literal["respond", "retrieve", "mixed"]


class RetrievalSlot(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    retrieval_query: str | None = Field(min_length=1, max_length=160)

    @field_validator("retrieval_query")
    @classmethod
    def nonblank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("retrieval_query must not be blank")
        return value


class _HookState(TypedDict):
    query: str
    recent_messages: list[dict[str, str]]
    control: ExecutionControl
    deadline: float
    action: Action | None
    retrieval_query: str | None
    output: str
    conversation_id: str
    message_id: str


class QueryHook:
    """Route once per user turn, optionally fill a slot, then append a fixed template."""

    def __init__(self, config: QueryHookConfig, models: QueryHookModels) -> None:
        self._config = config
        self._models = models
        graph = StateGraph(_HookState)
        graph.add_node("route", self._route)
        graph.add_node("rewrite", self._rewrite)
        graph.add_node("append", self._append)
        graph.add_edge(START, "route")
        graph.add_conditional_edges("route", lambda state: (
            END if state["action"] is None
            else "append" if state["action"] == "respond" else "rewrite"
        ), [END, "append", "rewrite"])
        graph.add_edge("rewrite", "append")
        graph.add_edge("append", END)
        self._graph = graph.compile(checkpointer=False)

    def __call__(self, inputs: RunInputs, control: ExecutionControl) -> str:
        control.check()
        query = inputs["user_message"]
        # Text-only routing cannot decide what an image or frozen playback asks for.
        if not self._config.enabled:
            return query
        if (inputs.get("attachments") or inputs.get("image_urls")
                or (inputs.get("extra_context") or {}).get("playback")
                or len(query) > self._config.query_max_chars):
            _logger.info(
                "event=query_hook_skipped conversation_id=%s message_id=%s reason=unsupported_input",
                inputs["conversation_id"], inputs["assistant_message_id"],
            )
            return query
        history: list[dict[str, str]] = []
        remaining = self._config.history_max_chars
        for item in reversed(inputs["history"]):
            role = str(item.get("role", "")).lower()
            content = item.get("content")
            if (role not in {"user", "assistant"} or not isinstance(content, str)
                    or str(item.get("status", "COMPLETED")).upper() != "COMPLETED"):
                continue
            if not remaining or len(history) >= self._config.history_message_limit:
                break
            content = content[-remaining:]
            remaining -= len(content)
            history.append({"role": role, "content": content})
        with model_call_context({"stage": "query_hook"}):
            result = self._graph.invoke({
                "query": query, "recent_messages": list(reversed(history)),
                "control": control,
                "deadline": monotonic() + self._config.total_timeout_seconds,
                "action": None, "retrieval_query": None, "output": query,
                "conversation_id": inputs["conversation_id"],
                "message_id": inputs["assistant_message_id"],
            })
        control.check()
        return result["output"]

    @staticmethod
    def _remaining(state: _HookState) -> float:
        parent = state["control"].remaining_seconds()
        return max(0, min(
            state["deadline"] - monotonic(),
            parent if parent is not None else float("inf"),
        ))

    def _route(self, state: _HookState) -> dict:
        started = monotonic()
        action = None
        status = "fallback"
        error = "hook_deadline"
        remaining = self._remaining(state)
        try:
            if remaining > 0:
                action = self._models.route(
                    {"query": state["query"], "recent_messages": state["recent_messages"]},
                    ROUTE_QUESTION, timeout_seconds=remaining,
                )
                if self._remaining(state) > 0:
                    status = "completed"
                    error = None
                else:
                    action = None
        except QueryHookModelError as exc:
            state["control"].check()
            error = str(exc)
        _logger.log(
            logging.WARNING if status == "fallback" else logging.INFO,
            "event=query_hook_route conversation_id=%s message_id=%s status=%s action=%s duration_ms=%.1f error=%s",
            state["conversation_id"], state["message_id"], status, action,
            (monotonic() - started) * 1000, error,
        )
        return {"action": action}

    def _rewrite(self, state: _HookState) -> dict:
        started = monotonic()
        query = None
        status = "fallback"
        error = "hook_deadline"
        remaining = self._remaining(state)
        try:
            if remaining > 0:
                prompt = (
                    f"{SLOT_PROMPT}\n已确定 action：{state['action']}\n\n"
                    f"近期对话（仅供明确指代参考）：\n{context_json(state['recent_messages'])}\n\n"
                    f"当前用户消息（仅提取其中的视频查找目标）：\n{state['query']}"
                )
                slot = self._models.rewrite(prompt, RetrievalSlot, timeout_seconds=remaining)
                if self._remaining(state) > 0:
                    query = slot.retrieval_query
                    status = "completed"
                    error = None
        except QueryHookModelError as exc:
            state["control"].check()
            error = str(exc)
        _logger.log(
            logging.WARNING if status == "fallback" else logging.INFO,
            "event=query_hook_rewrite conversation_id=%s message_id=%s status=%s duration_ms=%.1f error=%s",
            state["conversation_id"], state["message_id"], status,
            (monotonic() - started) * 1000, error,
        )
        return {"retrieval_query": query}

    @staticmethod
    def _append(state: _HookState) -> dict:
        state["control"].check()
        candidate = state["retrieval_query"]
        hint = f"\n候选检索用语（JSON 字符串）：{context_json(candidate)}" if candidate else ""
        return {"output": (
            f"{state['query']}\n\n[本轮处理说明｜应用生成]\n"
            f"{TEMPLATES[state['action']]}{hint}\n{TURN_SCOPE}\n[/本轮处理说明]"
        )}
