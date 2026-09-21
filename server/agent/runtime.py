from __future__ import annotations

import sys
from collections.abc import Callable
from copy import deepcopy
from dataclasses import replace
import logging
from math import isfinite
from threading import Event
from time import monotonic

from gateway.agent_model import AgentLoopModel, StructuredToolAgentModel
from langgraph.graph import END, START, StateGraph

from ..schema import VideoChunkSearchResult
from .chat import render_chat
from .errors import AgentModelInvocationError
from .evaluator import evaluate
from .output import (
    bind_draft, build_retrieval_trace, finalize_output,
    validate_approved_video_ranges,
)
from .query import run_query
from .state import (
    AgentFinalOutput, AnswerPhase, EvalDecision, ExecutionControl, LoopTurn,
    ModelContextBudget, QueryDraft, RunInputs, RunLimits, RunState,
    SelectedVideoRange,
)
from .tools import (
    DraftReadAudit, attach_agent_tool_logging, build_chat_tools,
    build_evaluator_tools, build_query_tools,
)
from .workspace import AgentWorkspace

_logger = logging.getLogger(__name__)


class AgentRuntime:
    """Run isolated Query, Evaluator and Chat contexts over one per-Kick Workspace."""

    def __init__(
        self,
        query_model: StructuredToolAgentModel,
        evaluator_model: StructuredToolAgentModel,
        chat_model: AgentLoopModel,
        retrieve: Callable[..., VideoChunkSearchResult],
        *,
        max_loop_turns: int = 3,
        max_model_calls_per_turn: int = 6,
        max_tool_calls_per_turn: int = 24,
        limits_enabled: bool = False,
        maximum_top_k: int = 20,
        max_prompt_chars: int = 200_000,
        max_tool_result_bytes: int = 16_384,
        max_catalog_items_per_page: int = 20,
        stage_timeout_seconds: float = 120,
        execution_timeout_seconds: float = 600,
    ) -> None:
        for name, value, maximum in [
            ("max_loop_turns", max_loop_turns, 3),
            ("max_model_calls_per_turn", max_model_calls_per_turn, None),
            ("max_tool_calls_per_turn", max_tool_calls_per_turn, None),
            ("maximum_top_k", maximum_top_k, 100),
            ("max_prompt_chars", max_prompt_chars, None),
            ("max_tool_result_bytes", max_tool_result_bytes, None),
            ("max_catalog_items_per_page", max_catalog_items_per_page, 100),
        ]:
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < 1
                or (maximum is not None and value > maximum)
            ):
                bounds = (
                    f"between 1 and {maximum}"
                    if maximum is not None
                    else "a positive integer"
                )
                raise ValueError(f"{name} must be {bounds}")
        if max_tool_result_bytes < 512:
            raise ValueError("max_tool_result_bytes must be at least 512")
        if not isinstance(limits_enabled, bool):
            raise ValueError("limits_enabled must be a boolean")
        for name, value in (
            ("stage_timeout_seconds", stage_timeout_seconds),
            ("execution_timeout_seconds", execution_timeout_seconds),
        ):
            if isinstance(value, bool) or not isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be positive and finite")
        self._query_model = query_model
        self._evaluator_model = evaluator_model
        self._chat_model = chat_model
        self._retrieve = retrieve
        self._max_loop_turns = max_loop_turns
        self._max_model_calls = max_model_calls_per_turn
        self._max_tool_calls = max_tool_calls_per_turn
        self._limits_enabled = limits_enabled
        self._maximum_top_k = maximum_top_k
        self._max_prompt_chars = max_prompt_chars
        self._max_tool_result_bytes = max_tool_result_bytes
        self._max_catalog_items = max_catalog_items_per_page
        self._stage_timeout = stage_timeout_seconds
        self._execution_timeout = execution_timeout_seconds
        builder = StateGraph(RunState)
        builder.add_node("turn", self.turn)
        builder.add_node("chat", self._chat)
        builder.add_node("output", self._output)
        builder.add_edge(START, "turn")
        builder.add_conditional_edges(
            "turn", self._route, {"continue": "turn", "complete": "chat"},
        )
        builder.add_edge("chat", "output")
        builder.add_edge("output", END)
        self._graph = builder.compile()

    @property
    def model_name(self) -> str:
        return self._chat_model.model_name

    def close(self) -> None:
        closed: set[int] = set()
        for model in (self._query_model, self._evaluator_model, self._chat_model):
            if id(model) in closed:
                continue
            closed.add(id(model))
            close_model = getattr(model, "close", None)
            if callable(close_model):
                close_model()

    def kick(
        self,
        inputs: RunInputs,
        *,
        emit: Callable[[dict], None] | None = None,
        cancellation: Event | None = None,
        max_model_calls_per_turn: int | None = None,
        max_tool_calls_per_turn: int | None = None,
        max_loop_turns: int | None = None,
    ) -> AgentFinalOutput:
        if (
            not inputs["workspace_id"].strip()
            or not inputs["user_message"].strip()
            or not inputs["series_ids"]
            or any(not value.strip() for value in inputs["series_ids"])
        ):
            raise ValueError("Kick requires user input and fixed workspace / Series scope")
        top_k = inputs["requested_top_k"]
        if (
            isinstance(top_k, bool)
            or not isinstance(top_k, int)
            or not 1 <= top_k <= 100
        ):
            raise ValueError("requested_top_k must be between 1 and 100")
        if max_loop_turns is not None and (
            isinstance(max_loop_turns, bool)
            or not isinstance(max_loop_turns, int)
            or not 0 <= max_loop_turns <= 100
        ):
            raise ValueError("max_loop_turns must be between 0 and 100")
        model_call_limit = _positive_run_limit(
            "max_model_calls_per_turn",
            self._max_model_calls
            if max_model_calls_per_turn is None else max_model_calls_per_turn,
        )
        tool_call_limit = _positive_run_limit(
            "max_tool_calls_per_turn",
            self._max_tool_calls
            if max_tool_calls_per_turn is None else max_tool_calls_per_turn,
        )
        legacy_limits_enabled = max_loop_turns is None and self._limits_enabled
        limits = RunLimits(
            max_loop_turns=(
                self._max_loop_turns if legacy_limits_enabled
                else max_loop_turns or None
            ),
            max_model_calls_per_turn=(
                model_call_limit if legacy_limits_enabled else None
            ),
            max_tool_calls_per_turn=(
                tool_call_limit if legacy_limits_enabled else None
            ),
        )
        control = ExecutionControl(
            monotonic() + self._execution_timeout, cancellation, emit,
        )
        workspace = AgentWorkspace(
            workspace_id=inputs["workspace_id"],
            series_ids=tuple(inputs["series_ids"]),
        )
        control.check()
        result = self._graph.invoke(
            {
                "inputs": deepcopy(inputs),
                "workspace": workspace,
                "turns": [],
                "control": control,
                "limits": limits,
            },
            config={
                "recursion_limit": limits.max_loop_turns + 5
                if limits.max_loop_turns is not None else sys.maxsize,
            },
        )
        control.check()
        return result["final_output"]

    def turn(self, state: RunState) -> dict:
        control = state["control"]
        inputs = state["inputs"]
        workspace = state["workspace"]
        previous = state["turns"]
        limits = state["limits"]
        control.check()
        if limits.max_loop_turns is not None and len(previous) >= limits.max_loop_turns:
            raise AgentModelInvocationError(
                "Query / Evaluator loop-turn budget exceeded"
            )
        control.publish(inputs, {
            "type": "answer.progress",
            "phase": AnswerPhase.RETRIEVING,
        })
        calls = []
        context_budget = ModelContextBudget(self._max_prompt_chars)
        query_control = replace(
            control,
            deadline=min(
                control.deadline,
                monotonic() + self._stage_timeout,
            ),
        )
        query_tools = build_query_tools(
            self._retrieve,
            workspace=workspace,
            inputs=inputs,
            control=query_control,
            calls=calls,
            maximum_top_k=self._maximum_top_k,
            maximum_tool_calls=limits.max_tool_calls_per_turn,
            maximum_result_bytes=self._max_tool_result_bytes,
            maximum_catalog_items=self._max_catalog_items,
        )
        attach_agent_tool_logging(query_tools, role="Query", inputs=inputs)
        selection_limit = min(inputs["requested_top_k"], self._maximum_top_k)
        completion = run_query(
            self._query_model,
            inputs,
            previous,
            tools=query_tools,
            max_model_calls=limits.max_model_calls_per_turn,
            control=query_control,
            selection_limit=selection_limit,
            context_budget=context_budget,
            workspace=workspace,
        )
        query_control.check()
        control.check()
        draft_items = workspace.draft_items()
        if len(draft_items) > selection_limit:
            raise AgentModelInvocationError(
                "Query draft exceeds the final selected-hit budget"
            )
        draft = QueryDraft(
            plan_context=completion.plan_context,
            message=completion.message,
            selected_video_ranges=[
                SelectedVideoRange(
                    video_chunk_id=item.video_chunk_id,
                    start_ms=item.start_ms,
                    end_ms=item.end_ms,
                )
                for item in draft_items
            ],
        )
        available, selected, sources = bind_draft(
            inputs,
            previous,
            calls,
            draft,
            max_selected_hits=selection_limit,
        )

        control.publish(inputs, {
            "type": "answer.progress",
            "phase": AnswerPhase.EVALUATING,
        })
        audit = DraftReadAudit()
        evaluator_control = replace(
            control,
            deadline=min(
                control.deadline,
                monotonic() + self._stage_timeout,
            ),
        )
        evaluator_tools = build_evaluator_tools(
            self._retrieve,
            workspace=workspace,
            inputs=inputs,
            control=evaluator_control,
            calls=calls,
            maximum_top_k=self._maximum_top_k,
            maximum_tool_calls=limits.max_tool_calls_per_turn,
            maximum_result_bytes=self._max_tool_result_bytes,
            maximum_catalog_items=self._max_catalog_items,
            audit=audit,
        )
        attach_agent_tool_logging(evaluator_tools, role="Evaluator", inputs=inputs)
        review = evaluate(
            self._evaluator_model,
            inputs,
            previous,
            draft,
            calls,
            sources,
            tools=evaluator_tools,
            max_model_calls=limits.max_model_calls_per_turn,
            control=evaluator_control,
            max_prompt_chars=self._max_prompt_chars,
            workspace=workspace,
        )
        evaluator_control.check()
        control.check()
        if review.action == "complete" and not audit.covers(
            item.video_chunk_id for item in draft_items
        ):
            review = EvalDecision(
                action="continue",
                advice=(
                    "Evaluator must read the complete Draft catalog and every Draft "
                    "item before approval. Repeat the audit using the read Tools."
                ),
            )
        if review.action == "complete":
            try:
                validate_approved_video_ranges(selected, draft)
            except AgentModelInvocationError as exc:
                review = EvalDecision(
                    action="continue",
                    advice=(
                        "Server range validation rejected the approved draft: "
                        f"{exc}. Revise the Draft Item ranges before resubmitting."
                    ),
                )
        if review.action == "complete":
            workspace.approve_draft()

        current = LoopTurn(
            number=len(previous) + 1,
            plan_context=draft.plan_context,
            retrieval_calls=tuple(calls),
            retrieval_result=available,
            evidence_source_turns=sources,
            query_draft=draft,
            draft_output=selected,
            review=review,
            status=review.action,
        )
        _logger.info(
            "event=agent_turn_evaluated conversation_id=%s message_id=%s "
            "turn_number=%s retrieved_hit_count=%s selected_hit_count=%s "
            "review_action=%s",
            inputs["conversation_id"],
            inputs["assistant_message_id"],
            current.number,
            len(available.hits),
            len(selected.hits),
            review.action,
        )
        if (
            limits.max_loop_turns is not None
            and review.action == "continue"
            and current.number >= limits.max_loop_turns
        ):
            raise AgentModelInvocationError(
                f"Evaluator rejected the Query draft after {current.number} turns"
            )
        return {"turns": [*previous, current]}

    @staticmethod
    def _route(state: RunState) -> str:
        return state["turns"][-1].review.action

    def _chat(self, state: RunState) -> dict:
        current = state["turns"][-1]
        control = state["control"]
        limits = state["limits"]
        control.publish(state["inputs"], {
            "type": "answer.progress",
            "phase": AnswerPhase.RESPONDING,
        })
        chat_control = replace(
            control,
            deadline=min(
                control.deadline,
                monotonic() + self._stage_timeout,
            ),
        )
        chat_tools = build_chat_tools(
            workspace=state["workspace"],
            control=chat_control,
            maximum_tool_calls=limits.max_tool_calls_per_turn,
            maximum_result_bytes=self._max_tool_result_bytes,
            maximum_catalog_items=self._max_catalog_items,
        )
        attach_agent_tool_logging(chat_tools, role="Chat", inputs=state["inputs"])
        rendered = render_chat(
            self._chat_model,
            state["inputs"],
            current,
            state["workspace"],
            tools=chat_tools,
            control=chat_control,
            max_prompt_chars=self._max_prompt_chars,
            max_model_calls=limits.max_model_calls_per_turn,
            retrieval_trace=build_retrieval_trace(state["turns"]),
        )
        chat_control.check()
        return {
            "turns": [
                *state["turns"][:-1],
                replace(current, rendered_answer=rendered),
            ]
        }

    @staticmethod
    def _output(state: RunState) -> dict:
        state["control"].check()
        return {"final_output": finalize_output(state["turns"], state["inputs"])}


def _positive_run_limit(name: str, value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value
