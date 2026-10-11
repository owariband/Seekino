from __future__ import annotations

import sys
from collections.abc import Callable
from copy import deepcopy
from dataclasses import replace
import logging
from math import isfinite
from threading import Event
from time import monotonic

from engine.rag.contracts import VideoChunkSearchResult
from gateway.agent_model.contracts import AgentLoopModel, StructuredToolAgentModel
from gateway.agent_model.errors import ModelFailureError, ModelFailureReason
from langchain.agents.middleware.model_call_limit import ModelCallLimitExceededError
from langgraph.graph import END, START, StateGraph

from ..common.errors import AgentContextBudgetError, AgentModelInvocationError
from ..common.tool import as_langchain_tools, attach_agent_tool_logging
from .chat import render_chat
from .errors import (
    VideoRetrievalError, VideoSearchFailure, VideoSearchFailureCode,
    VideoSearchFailurePhase,
)
from .evaluator import evaluate
from .output import (
    bind_draft, build_retrieval_trace, finalize_output,
    validate_approved_video_ranges,
)
from .query import run_query
from .state import (
    AgentFinalOutput, AnswerPhase, EvalDecision, ExecutionControl, LoopTurn,
    ModelContextBudget, QueryDraft, RunInputs, RunLimits, RunState,
    RetrievalChannelContract, SelectedVideoRange, context_json,
)
from .skills import LEXICAL_RETRIEVAL_SKILL_PROMPT, RETRIEVAL_SKILL_PROMPT
from .tools import (
    DraftReadAudit, build_chat_tools, build_evaluator_tools, build_query_tools,
)
from .video_search_canvas import VideoSearchContextCanvas

_logger = logging.getLogger(__name__)


class AgentRuntime:
    """Run isolated Query, Evaluator and Chat contexts over one per-Kick Canvas."""

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
        max_read_chars_per_call: int = 16_384,
        max_catalog_items_per_page: int = 20,
        stage_timeout_seconds: float = 480,
        evaluator_stage_timeout_seconds: float = 480,
        chat_timeout_seconds: float = 480,
        execution_timeout_seconds: float = 480,
        retrieval_channel: RetrievalChannelContract = "transcript",
    ) -> None:
        for name, value, maximum in [
            ("max_loop_turns", max_loop_turns, 3),
            ("max_model_calls_per_turn", max_model_calls_per_turn, None),
            ("max_tool_calls_per_turn", max_tool_calls_per_turn, None),
            ("maximum_top_k", maximum_top_k, 100),
            ("max_prompt_chars", max_prompt_chars, None),
            ("max_tool_result_bytes", max_tool_result_bytes, None),
            ("max_read_chars_per_call", max_read_chars_per_call, None),
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
        if retrieval_channel not in ("transcript", "lexical"):
            raise ValueError("retrieval_channel must be transcript or lexical")
        for name, value in (
            ("stage_timeout_seconds", stage_timeout_seconds),
            ("evaluator_stage_timeout_seconds", evaluator_stage_timeout_seconds),
            ("chat_timeout_seconds", chat_timeout_seconds),
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
        self._max_read_chars_per_call = max_read_chars_per_call
        self._max_catalog_items = max_catalog_items_per_page
        self._stage_timeout = stage_timeout_seconds
        self._evaluator_stage_timeout = evaluator_stage_timeout_seconds
        self._chat_timeout = chat_timeout_seconds
        self._execution_timeout = execution_timeout_seconds
        self._retrieval_channel = retrieval_channel
        # TODO(video-parse-v2/chat-owner): Defer Chat retrieval/prompt and Message
        # contract refactors until Engine publishes its query-policy contract.
        # Then adapt to that API; do not implement Engine policy in Chat or
        # change Message fields as part of Parse V2 pipeline work.
        self._retrieval_skill_prompt = (
            LEXICAL_RETRIEVAL_SKILL_PROMPT
            if retrieval_channel == "lexical"
            else RETRIEVAL_SKILL_PROMPT
        )
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
        # Keep the per-call Canvas and controls out of Supervisor checkpoints.
        self._graph = builder.compile(checkpointer=False)

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
        deadline: float | None = None,
    ) -> AgentFinalOutput:
        if (
            not inputs["tenant_id"].strip()
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
        own_deadline = monotonic() + self._execution_timeout
        if deadline is not None and (not isfinite(deadline) or deadline <= 0):
            raise ValueError("deadline must be positive and finite")
        control = ExecutionControl(
            min(own_deadline, deadline) if deadline is not None else own_deadline,
            cancellation, emit,
        )
        canvas = VideoSearchContextCanvas(
            tenant_id=inputs["tenant_id"],
            series_ids=tuple(inputs["series_ids"]),
        )
        control.check()
        result = self._graph.invoke(
            {
                "inputs": deepcopy(inputs),
                "canvas": canvas,
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
        canvas = state["canvas"]
        previous = state["turns"]
        limits = state["limits"]
        control.check()
        if limits.max_loop_turns is not None and len(previous) >= limits.max_loop_turns:
            raise VideoSearchFailure(
                VideoSearchFailureCode.LOOP_LIMIT_EXCEEDED,
                VideoSearchFailurePhase.QUERY,
                retryable=True,
            ) from AgentModelInvocationError(
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
            canvas=canvas,
            inputs=inputs,
            control=query_control,
            calls=calls,
            maximum_top_k=self._maximum_top_k,
            maximum_tool_calls=limits.max_tool_calls_per_turn,
            maximum_result_bytes=self._max_tool_result_bytes,
            maximum_read_chars=self._max_read_chars_per_call,
            maximum_catalog_items=self._max_catalog_items,
            retrieval_channel=self._retrieval_channel,
        )
        attach_agent_tool_logging(query_tools, role="Query", inputs=inputs)
        try:
            completion = run_query(
                self._query_model,
                inputs,
                previous,
                tools=as_langchain_tools(query_tools),
                max_model_calls=limits.max_model_calls_per_turn,
                control=query_control,
                context_budget=context_budget,
                canvas=canvas,
                retrieval_skill_prompt=self._retrieval_skill_prompt,
            )
            query_control.check()
            control.check()
        except Exception as exc:
            _raise_classified_failure(
                exc,
                phase=VideoSearchFailurePhase.QUERY,
                stage_control=query_control,
                run_control=control,
            )
        draft_items = canvas.draft_items()
        draft = QueryDraft(
            plan_context=completion.plan_context,
            message=completion.message,
            selected_video_ranges=[
                SelectedVideoRange(
                    video_chunk_id=item.video_chunk_id,
                    start_seconds=item.start_seconds,
                    end_seconds=item.end_seconds,
                )
                for item in draft_items
            ],
        )
        available, selected, sources = bind_draft(
            inputs,
            previous,
            calls,
            draft,
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
                monotonic() + self._evaluator_stage_timeout,
            ),
        )
        evaluator_tools = build_evaluator_tools(
            self._retrieve,
            canvas=canvas,
            inputs=inputs,
            control=evaluator_control,
            calls=calls,
            maximum_top_k=self._maximum_top_k,
            maximum_tool_calls=limits.max_tool_calls_per_turn,
            maximum_result_bytes=self._max_tool_result_bytes,
            maximum_read_chars=self._max_read_chars_per_call,
            maximum_catalog_items=self._max_catalog_items,
            audit=audit,
            retrieval_channel=self._retrieval_channel,
        )
        attach_agent_tool_logging(evaluator_tools, role="Evaluator", inputs=inputs)
        try:
            review = evaluate(
                self._evaluator_model,
                inputs,
                previous,
                draft,
                calls,
                sources,
                tools=as_langchain_tools(evaluator_tools),
                max_model_calls=limits.max_model_calls_per_turn,
                control=evaluator_control,
                max_prompt_chars=self._max_prompt_chars,
                canvas=canvas,
                retrieval_skill_prompt=self._retrieval_skill_prompt,
            )
            evaluator_control.check()
            control.check()
        except Exception as exc:
            _raise_classified_failure(
                exc,
                phase=VideoSearchFailurePhase.EVALUATOR,
                stage_control=evaluator_control,
                run_control=control,
            )
        evaluator_review = review
        review_source = "evaluator"
        if review.action == "complete":
            with canvas._lock:
                approval_revision = canvas.revision
                if canvas.draft_items() != draft_items or not audit.covers(
                    [item.video_chunk_id for item in draft_items], revision=approval_revision,
                ):
                    review = EvalDecision(
                        action="continue",
                        advice=("Evaluator must read every page of the current Draft catalog "
                                "and every section of each current Draft item before approval. "
                                "Repeat the audit using get_context_canvas."),
                    )
                    review_source = "draft_read_guard"
                else:
                    # Output uses the same immutable evidence that was actually audited.
                    selected = selected.model_copy(update={"hits": [
                        canvas.retrieval_snapshot(item.video_chunk_id) for item in draft_items
                    ]}, deep=True)
                    try:
                        validate_approved_video_ranges(selected, draft)
                    except AgentModelInvocationError as exc:
                        review = EvalDecision(
                            action="continue",
                            advice=("Approved range validation rejected the draft: "
                                    f"{exc}. Revise the Draft Item ranges before resubmitting."),
                        )
                        review_source = "range_validation"
                    if review.action == "complete":
                        canvas.approve_draft(expected_revision=approval_revision)

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
            "review_action=%s review_source=%s evaluator_action=%s "
            "evaluator_advice=%s review_advice=%s",
            inputs["conversation_id"],
            inputs["assistant_message_id"],
            current.number,
            len(available.hits),
            len(selected.hits),
            review.action,
            review_source,
            evaluator_review.action,
            context_json(evaluator_review.advice),
            context_json(review.advice),
        )
        if (
            limits.max_loop_turns is not None
            and review.action == "continue"
            and current.number >= limits.max_loop_turns
        ):
            raise VideoSearchFailure(
                VideoSearchFailureCode.LOOP_LIMIT_EXCEEDED,
                VideoSearchFailurePhase.EVALUATOR,
                retryable=True,
            ) from AgentModelInvocationError(
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
                monotonic() + self._chat_timeout,
            ),
        )
        chat_tools = build_chat_tools(
            canvas=state["canvas"],
            control=chat_control,
            maximum_tool_calls=limits.max_tool_calls_per_turn,
            maximum_result_bytes=self._max_tool_result_bytes,
            maximum_read_chars=self._max_read_chars_per_call,
            maximum_catalog_items=self._max_catalog_items,
        )
        attach_agent_tool_logging(chat_tools, role="Chat", inputs=state["inputs"])
        try:
            rendered = render_chat(
                self._chat_model,
                state["inputs"],
                current,
                state["canvas"],
                tools=as_langchain_tools(chat_tools),
                control=chat_control,
                max_prompt_chars=self._max_prompt_chars,
                max_model_calls=limits.max_model_calls_per_turn,
                retrieval_trace=build_retrieval_trace(state["turns"]),
            )
            chat_control.check()
            control.check()
        except Exception as exc:
            _raise_classified_failure(
                exc,
                phase=VideoSearchFailurePhase.CHAT,
                stage_control=chat_control,
                run_control=control,
            )
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


def _raise_classified_failure(
    error: Exception,
    *,
    phase: VideoSearchFailurePhase,
    stage_control: ExecutionControl,
    run_control: ExecutionControl,
) -> None:
    """Preserve execution aborts and classify only known recoverable failures."""

    stage_control.check()
    run_control.check()
    if isinstance(error, VideoSearchFailure):
        raise error

    chain = tuple(_error_chain(error))
    if any(isinstance(item, AgentContextBudgetError) for item in chain):
        raise VideoSearchFailure(
            VideoSearchFailureCode.CONTEXT_BUDGET_EXCEEDED,
            phase,
            retryable=True,
        ) from error

    if any(_is_loop_limit_error(item) for item in chain):
        raise VideoSearchFailure(
            VideoSearchFailureCode.LOOP_LIMIT_EXCEEDED,
            phase,
            retryable=True,
        ) from error

    reason = next(
        (
            item.reason
            for item in chain
            if isinstance(getattr(item, "reason", None), ModelFailureReason)
        ),
        None,
    )
    if reason is not None:
        code = {
            ModelFailureReason.OUTPUT_TOKEN_LIMIT:
                VideoSearchFailureCode.OUTPUT_TOKEN_LIMIT,
            ModelFailureReason.MODEL_TIMEOUT:
                VideoSearchFailureCode.MODEL_TIMEOUT,
            ModelFailureReason.REQUIRED_TOOL_MISSING:
                VideoSearchFailureCode.REQUIRED_TOOL_MISSING,
            ModelFailureReason.LOOP_LIMIT:
                VideoSearchFailureCode.LOOP_LIMIT_EXCEEDED,
            ModelFailureReason.INVALID_MODEL_RESPONSE:
                VideoSearchFailureCode.INVALID_MODEL_RESPONSE,
        }[reason]
        raise VideoSearchFailure(code, phase, retryable=True) from error

    retrieval_error = next(
        (item for item in chain if isinstance(item, VideoRetrievalError)),
        None,
    )
    if retrieval_error is not None:
        raise VideoSearchFailure(
            VideoSearchFailureCode.RETRIEVAL_FAILED,
            VideoSearchFailurePhase.RETRIEVAL,
            retryable=True,
        ) from (retrieval_error.__cause__ or retrieval_error)

    raise error


def _error_chain(error: BaseException):
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def _is_loop_limit_error(error: BaseException) -> bool:
    message = str(error).lower()
    if isinstance(error, ModelCallLimitExceededError):
        return True
    if isinstance(error, ModelFailureError):
        return "agent loop exhausted max_model_calls" in message
    if isinstance(error, AgentModelInvocationError):
        return (
            "tool-call budget exceeded" in message
            or "loop-turn budget exceeded" in message
        )
    return False
