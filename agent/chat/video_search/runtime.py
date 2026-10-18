from __future__ import annotations

import sys
from collections.abc import Callable
from copy import deepcopy
from dataclasses import replace
import logging
from math import isfinite
from threading import Event
from time import monotonic
from typing import Literal

from engine.rag.contracts import VideoChunkSearchResult
from engine.rag.retrieval.contracts import EventSearchResult
from gateway.agent_model.contracts import AgentLoopModel, StructuredToolAgentModel
from gateway.agent_model.errors import ModelFailureError, ModelFailureReason
from langchain.agents.middleware.model_call_limit import ModelCallLimitExceededError
from langgraph.graph import END, START, StateGraph

from agent.utils.tool import AgentTool

from ..common.errors import AgentContextBudgetError, AgentModelInvocationError
from ..common.tool import as_langchain_tools, attach_agent_tool_logging
from .chat import render_chat
from .errors import (
    VideoRetrievalError, VideoSearchFailure, VideoSearchFailureCode,
    VideoSearchFailurePhase,
)
from .evaluator import evaluate
from .loop_control import best_attempt, decide, evaluator_risks, replan_advice, uncertainty_note
from .frame_evidence import FrameInspectionSession, FrameReader
from .output import (
    bind_draft, build_chat_output, build_retrieval_trace, finalize_output,
    validate_approved_video_ranges, validate_result,
)
from .query import run_query
from .result import run_result, validate_result_items
from .state import (
    AgentFinalOutput, AnswerPhase, ChatRenderDraft, ChatVideoBlock, EvalDecision, ExecutionControl,
    LoopTurn, MarkdownRenderBlock, QueryDraft, ResultAttempt, RunInputs, RunLimits, RunState,
    RetrievalChannelContract, SelectedVideoRange, context_json,
)
from .skills import LEXICAL_RETRIEVAL_SKILL_PROMPT, RETRIEVAL_SKILL_PROMPT
from .tools import (
    build_chat_tools, build_evaluator_tools, build_query_tools,
)
from .tools.canvas import build_video_search_canvas_tools
from .video_search_canvas import VideoSearchContextCanvas

_logger = logging.getLogger(__name__)


class AgentRuntime:
    """Run isolated Query, Evaluator and Chat contexts over one per-Kick Canvas.

    Under a Supervisor with ``single_pass_result``, Query is the only ReAct loop,
    the Evaluator runs only for high-risk drafts, and one structured Result call
    writes the answer. Deterministic Loop Control then passes, asks the user,
    re-plans (at most ``result_max_loops`` outer loops) or degrades.
    """

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
        total_timeout_enabled: bool = True,
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
        frame_reader: FrameReader | None = None,
        frame_max_height: int | None = None,
        maximum_frame_inspections: int | None = None,
        event_retrieve: Callable[..., EventSearchResult] | None = None,
        result_model: StructuredToolAgentModel | None = None,
        result_max_loops: int = 2,
        evaluator_policy: Literal["risk", "always"] = "risk",
        evaluator_low_confidence_rank: int = 3,
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
            ("result_max_loops", result_max_loops, 3),
            ("evaluator_low_confidence_rank", evaluator_low_confidence_rank, 100),
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
        if not isinstance(total_timeout_enabled, bool):
            raise ValueError("total_timeout_enabled must be a boolean")
        if evaluator_policy not in ("risk", "always"):
            raise ValueError("evaluator_policy must be risk or always")
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
        self._result_model = result_model
        self._result_max_loops = result_max_loops
        self._evaluator_policy = evaluator_policy
        self._evaluator_low_confidence_rank = evaluator_low_confidence_rank
        self._retrieve = retrieve
        self._event_retrieve = event_retrieve
        self._max_loop_turns = max_loop_turns
        self._max_model_calls = max_model_calls_per_turn
        self._max_tool_calls = max_tool_calls_per_turn
        self._limits_enabled = limits_enabled
        self._total_timeout_enabled = total_timeout_enabled
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
        if frame_reader is not None:
            for name, value in (("frame_max_height", frame_max_height),
                                ("maximum_frame_inspections", maximum_frame_inspections)):
                if type(value) is not int or value <= 0:
                    raise ValueError(f"{name} must be a positive integer when frame_reader is configured")
        self._frame_reader = frame_reader
        self._frame_max_height = frame_max_height
        self._maximum_frame_inspections = maximum_frame_inspections
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
        builder.add_node("result", self._result)
        builder.add_node("output", self._output)
        builder.add_edge(START, "turn")
        builder.add_conditional_edges(
            "turn", self._route, {"continue": "turn", "complete": "chat", "result": "result"},
        )
        builder.add_conditional_edges(
            "result", self._route_result, {"replan": "turn", "done": "output"},
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
        for model in (self._query_model, self._evaluator_model, self._chat_model, self._result_model):
            if model is None:
                continue
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
        on_handoff: Callable[[VideoSearchContextCanvas, LoopTurn], None] | None = None,
        render_with_chat: bool = True,
        single_pass_result: bool = False,
    ) -> AgentFinalOutput:
        if (
            not inputs["tenant_id"].strip()
            or not inputs["user_message"].strip()
            or not inputs["series_ids"]
            or any(not value.strip() for value in inputs["series_ids"])
        ):
            raise ValueError("Kick requires user input and fixed workspace / Series scope")
        if single_pass_result and (render_with_chat or self._result_model is None):
            raise ValueError("single_pass_result requires a Supervisor handoff and a Result model")
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
        run_deadline = (
            monotonic() + self._execution_timeout
            if self._total_timeout_enabled else None
        )
        if deadline is not None and (not isfinite(deadline) or deadline <= 0):
            raise ValueError("deadline must be positive and finite")
        if deadline is not None:
            run_deadline = deadline if run_deadline is None else min(run_deadline, deadline)
        control = ExecutionControl(
            run_deadline, cancellation, emit,
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
                "frame_inspections": (
                    FrameInspectionSession(
                        self._maximum_frame_inspections if legacy_limits_enabled else None
                    )
                    if self._frame_reader is not None else None
                ),
                "turns": [],
                "control": control,
                "limits": limits,
                "render_with_chat": render_with_chat,
                "single_pass_result": single_pass_result,
                "attempts": [],
            },
            config={
                "recursion_limit": limits.max_loop_turns + 5
                if limits.max_loop_turns is not None else sys.maxsize,
            },
        )
        control.check()
        if on_handoff is not None:
            turn = result["turns"][-1]
            final_output = result["final_output"]
            partial = final_output.partial
            warnings = list(final_output.warnings)
            searched_video_ids = set(final_output.searched_video_ids or [])
            scope_recorded = final_output.searched_video_ids is not None
            for completed_turn in result["turns"]:
                for call in completed_turn.retrieval_calls:
                    if call.status == "error":
                        continue
                    partial = partial or call.result.partial
                    warnings.extend(call.result.warnings)
                    scope_recorded = scope_recorded and call.result.searched_video_ids is not None
                    searched_video_ids.update(call.result.searched_video_ids or [])
            coverage = {
                "partial": partial,
                "warnings": list(dict.fromkeys(warnings)),
                "searched_video_ids": sorted(searched_video_ids) if scope_recorded else None,
            }
            turn = replace(turn, retrieval_result=turn.retrieval_result.model_copy(
                update=coverage, deep=True,
            ))
            canvas.put("review", "latest", {
                "turn_number": turn.number,
                "action": turn.review.action,
                "advice": turn.review.advice,
                "selected_video_ranges": [
                    item.model_dump(mode="json")
                    for item in turn.query_draft.selected_video_ranges
                ],
                **coverage,
                **({"result": {
                    "outcome": result["outcome"],
                    "loop": result["final_attempt"].loop,
                    "loops": len(result["attempts"]),
                    "self_check": result["final_attempt"].output.self_check.model_dump(mode="json", by_alias=True),
                    "warnings": list(result["final_attempt"].warnings),
                }} if "final_attempt" in result else {}),
            }, readonly=True)
            control.check()
            on_handoff(canvas, turn)
            control.check()
        return result["final_output"]

    def build_supervisor_read_tools(
        self, canvas: VideoSearchContextCanvas, control: ExecutionControl,
        *, maximum_result_bytes: int | None = None,
    ) -> list[AgentTool]:
        return build_video_search_canvas_tools(
            canvas=canvas, role="supervisor", control=control,
            maximum_result_bytes=(
                self._max_tool_result_bytes if maximum_result_bytes is None
                else min(self._max_tool_result_bytes, maximum_result_bytes)
            ),
            maximum_catalog_items=self._max_catalog_items,
            maximum_read_chars=self._max_read_chars_per_call,
        )

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
        query_deadline = monotonic() + self._stage_timeout
        query_control = replace(control, deadline=(
            query_deadline if control.deadline is None
            else min(control.deadline, query_deadline)
        ))
        query_tools = build_query_tools(
            self._retrieve,
            event_retrieve=self._event_retrieve,
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
            frame_reader=self._frame_reader,
            frame_session=state.get("frame_inspections"),
            frame_max_height=self._frame_max_height,
            supports_vision_input=getattr(self._query_model, "supports_vision_input", False),
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
                max_prompt_chars=self._max_prompt_chars,
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

        single_pass = state["single_pass_result"]
        # Under single-pass Result the Evaluator is a high-risk bypass, not a stage of every request.
        risks = evaluator_risks(
            inputs, [item.video_chunk_id for item in draft_items],
            [*(call for item in previous for call in item.retrieval_calls), *calls],
            low_confidence_rank=self._evaluator_low_confidence_rank,
        ) if single_pass else ()
        if not single_pass or self._evaluator_policy == "always" or risks:
            control.publish(inputs, {
                "type": "answer.progress",
                "phase": AnswerPhase.EVALUATING,
            })
            evaluator_deadline = monotonic() + self._evaluator_stage_timeout
            evaluator_control = replace(control, deadline=(
                evaluator_deadline if control.deadline is None
                else min(control.deadline, evaluator_deadline)
            ))
            evaluator_tools = build_evaluator_tools(
                self._retrieve,
                event_retrieve=self._event_retrieve,
                canvas=canvas,
                inputs=inputs,
                control=evaluator_control,
                calls=calls,
                maximum_top_k=self._maximum_top_k,
                maximum_tool_calls=limits.max_tool_calls_per_turn,
                maximum_result_bytes=self._max_tool_result_bytes,
                maximum_read_chars=self._max_read_chars_per_call,
                maximum_catalog_items=self._max_catalog_items,
                retrieval_channel=self._retrieval_channel,
                frame_reader=self._frame_reader,
                frame_session=state.get("frame_inspections"),
                frame_max_height=self._frame_max_height,
                supports_vision_input=getattr(self._evaluator_model, "supports_vision_input", False),
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
                    supervisor_final_review=not state["render_with_chat"],
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
            review_source = "evaluator"
        else:
            review = EvalDecision(action="complete", advice="")
            review_source = "host"
        evaluator_review = review
        if review.action == "complete" and not single_pass:
            with canvas._lock:
                # Freeze canonical evidence after the Evaluator approves the draft.
                selected = selected.model_copy(update={"hits": [
                    canvas.retrieval_snapshot(item.video_chunk_id) for item in draft_items
                ]}, deep=True)
                validate_approved_video_ranges(selected, draft)
                canvas.approve_draft(expected_revision=canvas.revision)

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
            review_source=review_source,
            evaluator_risks=risks,
        )
        canvas.put("history", str(current.number), current.context(), readonly=True)
        _logger.info(
            "event=agent_turn_evaluated conversation_id=%s message_id=%s "
            "turn_number=%s retrieved_hit_count=%s selected_hit_count=%s "
            "review_action=%s review_source=%s evaluator_action=%s "
            "evaluator_advice=%s review_advice=%s evaluator_risks=%s",
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
            ",".join(risks) or "-",
        )
        if (
            not single_pass
            and limits.max_loop_turns is not None
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
        if state["single_pass_result"]:
            return "result"
        return state["turns"][-1].review.action

    @staticmethod
    def _route_result(state: RunState) -> str:
        return "replan" if state["outcome"] == "replan" else "done"

    def _result(self, state: RunState) -> dict:
        """Run one single-pass Result and let Loop Control choose the exit."""

        control = state["control"]
        inputs = state["inputs"]
        canvas = state["canvas"]
        turns = state["turns"]
        turn = turns[-1]
        attempts = state["attempts"]
        assert self._result_model is not None
        max_loops = (
            self._result_max_loops if state["limits"].max_loop_turns is None
            else min(self._result_max_loops, state["limits"].max_loop_turns)
        )
        draft_items = canvas.draft_items()
        draft_ids = frozenset(item.video_chunk_id for item in draft_items)
        retrieved_ids = frozenset(
            hit.candidate.video_chunk_id
            for call in turn.retrieval_calls if call.status != "error"
            for hit in call.result.hits
        )
        rejected = turn.review.action == "continue"
        if rejected:
            # A rejected draft re-plans on the Evaluator's advice without spending a Result call.
            attempt = ResultAttempt(
                loop=turn.number, output=None, items=(), draft_ids=draft_ids,
                retrieved_ids=retrieved_ids, evaluator_rejected=True,
                evaluator_advice=turn.review.advice,
            )
            if decide(attempt, attempts, max_loops=max_loops) == "replan":
                self._log_loop(inputs, attempt, "replan")
                return {"attempts": [*attempts, attempt], "outcome": "replan"}
        control.publish(inputs, {
            "type": "answer.progress",
            "phase": AnswerPhase.RESPONDING,
        })
        result_deadline = monotonic() + self._chat_timeout
        result_control = replace(control, deadline=(
            result_deadline if control.deadline is None
            else min(control.deadline, result_deadline)
        ))
        previous = next((item for item in reversed(attempts) if item.output is not None), None)
        try:
            output = run_result(
                self._result_model, inputs, turn, canvas, draft_items,
                [call for item in turns for call in item.retrieval_calls],
                previous=previous, control=result_control,
                max_prompt_chars=self._max_prompt_chars,
            )
            result_control.check()
            control.check()
        except Exception as exc:
            _raise_classified_failure(
                exc,
                phase=VideoSearchFailurePhase.RESULT,
                stage_control=result_control,
                run_control=control,
            )
        items, warnings = validate_result_items(output, canvas, draft_items)
        attempt = ResultAttempt(
            loop=turn.number, output=output, items=items, draft_ids=draft_ids,
            retrieved_ids=retrieved_ids, evaluator_rejected=rejected,
            evaluator_advice=turn.review.advice if rejected else "", warnings=warnings,
        )
        history = [*attempts, attempt]
        outcome = decide(attempt, attempts, max_loops=max_loops)
        if rejected and outcome == "replan":
            # Loop Control already declined to re-plan this rejected draft before the Result.
            outcome = "degrade"
        self._log_loop(inputs, attempt, outcome)
        if outcome == "replan":
            advice = replan_advice(attempt)
            # The next Plan turn reads this advice exactly like Evaluator advice.
            replanned = replace(
                turn, review=EvalDecision(action="continue", advice=advice),
                status="continue", review_source="result",
            )
            # Keep the Evaluator's record of this turn; add the Result's re-plan beside it.
            canvas.put("history", f"{turn.number}.result", replanned.context(), readonly=True)
            return {"turns": [*turns[:-1], replanned], "attempts": history, "outcome": outcome}
        final = attempt if outcome in ("pass", "ask_user") else best_attempt(history)
        return {"attempts": history, "final_attempt": final, "outcome": outcome}

    @staticmethod
    def _log_loop(inputs: RunInputs, attempt: ResultAttempt, outcome: str) -> None:
        check = attempt.output.self_check if attempt.output is not None else None
        _logger.info(
            "event=agent_result_loop conversation_id=%s message_id=%s loop=%s outcome=%s "
            "self_check_pass=%s needs_user_confirmation=%s missing_evidence=%s "
            "evaluator_rejected=%s video_item_count=%s rejected_item_count=%s",
            inputs["conversation_id"], inputs["assistant_message_id"], attempt.loop, outcome,
            check.passed if check else None, check.needs_user_confirmation if check else None,
            context_json(list(attempt.missing_evidence)), attempt.evaluator_rejected,
            len(attempt.items), len(attempt.warnings),
        )

    def _chat(self, state: RunState) -> dict:
        current = state["turns"][-1]
        control = state["control"]
        limits = state["limits"]
        control.publish(state["inputs"], {
            "type": "answer.progress",
            "phase": AnswerPhase.RESPONDING,
        })
        if not state["render_with_chat"]:
            # The Supervisor rewrites the answer from the Canvas, so an inner
            # Chat render would only be discarded. Project the approved draft.
            return {
                "turns": [
                    *state["turns"][:-1],
                    replace(current, rendered_answer=_render_approved_draft(
                        state["canvas"], current,
                    )),
                ]
            }
        chat_deadline = monotonic() + self._chat_timeout
        chat_control = replace(control, deadline=(
            chat_deadline if control.deadline is None
            else min(control.deadline, chat_deadline)
        ))
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
        if state["single_pass_result"]:
            return {"final_output": _result_output(state)}
        return {"final_output": finalize_output(state["turns"], state["inputs"])}


def _result_output(state: RunState) -> AgentFinalOutput:
    """Project the chosen Result attempt through the existing final-output contract."""

    canvas = state["canvas"]
    attempt = state["final_attempt"]
    assert attempt.output is not None
    turn = next(item for item in state["turns"] if item.number == attempt.loop)
    markdown = attempt.output.answer_markdown
    if state["outcome"] == "degrade":
        markdown = uncertainty_note(attempt, state["inputs"]["user_message"]) + "\n\n" + markdown
    sources = {item.video_chunk_id: list(item.source_video_chunk_ids) for item in attempt.items}
    source_ids = list(dict.fromkeys(identifier for values in sources.values() for identifier in values))
    with canvas._lock:
        evidence = turn.retrieval_result.model_copy(update={
            "hits": [canvas.retrieval_snapshot(identifier) for identifier in source_ids],
            "agent_reply": turn.retrieval_result.agent_reply.model_copy(update={"result_count": len(source_ids)}),
        }, deep=True)
    selection = turn.query_draft.model_copy(update={"selected_video_ranges": [
        SelectedVideoRange(
            video_chunk_id=item.video_chunk_id, start_seconds=item.start_seconds,
            end_seconds=item.end_seconds,
        )
        for item in attempt.items
    ]}, deep=True)
    validate_result(evidence, state["inputs"])
    rendered = ChatRenderDraft(blocks=[
        MarkdownRenderBlock(markdown=markdown),
        *(ChatVideoBlock(video_chunk_id=item.video_chunk_id, claim=item.claim) for item in attempt.items),
    ])
    return build_chat_output(
        evidence, selection, rendered, build_retrieval_trace(state["turns"]), video_sources=sources,
    )


def _render_approved_draft(
    canvas: VideoSearchContextCanvas, turn: LoopTurn,
) -> ChatRenderDraft:
    canvas.append_markdown(turn.query_draft.message)
    for item in canvas.draft_items(approved=True):
        canvas.upsert_video_item(ChatVideoBlock(
            video_chunk_id=item.video_chunk_id, claim=item.claim,
        ))
    return canvas.render_draft()


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
