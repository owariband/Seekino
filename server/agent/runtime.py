from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from dataclasses import replace
from math import isfinite
from threading import Event
from time import monotonic

from gateway.agent_model import QueryAgentModel
from gateway.models import StreamingChatModel, StructuredAgentModel
from langgraph.graph import END, START, StateGraph

from ..schema import VideoChunkSearchResult
from .chat import render_chat
from .evaluator import evaluate
from .output import bind_draft, finalize_output
from .query import run_query
from .state import AgentModelInvocationError, ExecutionControl, LoopTurn, RunInputs, RunState
from .tools import build_retrieval_tool


class AgentRuntime:
    """Reusable Loop rules; every kick owns its state, scope and delivery buffer.

    Deadline/cancellation checks are cooperative at model/tool/stream boundaries.
    Blocking Provider calls also need a finite timeout on the injected model.
    """

    def __init__(
        self,
        query_model: QueryAgentModel,
        evaluator_model: StructuredAgentModel,
        chat_model: StreamingChatModel,
        retrieve: Callable[..., VideoChunkSearchResult],
        *,
        max_loop_turns: int = 3,
        max_model_calls_per_turn: int = 6,
        max_tool_calls_per_turn: int = 4,
        maximum_top_k: int = 20,
        execution_timeout_seconds: float = 180,
    ) -> None:
        for name, value, maximum in [
            ("max_loop_turns", max_loop_turns, 3),
            ("max_model_calls_per_turn", max_model_calls_per_turn, None),
            ("max_tool_calls_per_turn", max_tool_calls_per_turn, None),
            ("maximum_top_k", maximum_top_k, 100),
        ]:
            if (isinstance(value, bool) or not isinstance(value, int) or value < 1
                or (maximum is not None and value > maximum)):
                bounds = f"between 1 and {maximum}" if maximum is not None else "a positive integer"
                raise ValueError(f"{name} must be {bounds}")
        if (isinstance(execution_timeout_seconds, bool)
            or not isfinite(execution_timeout_seconds) or execution_timeout_seconds <= 0):
            raise ValueError("execution_timeout_seconds must be positive and finite")
        self._query_model = query_model
        self._evaluator_model = evaluator_model
        self._chat_model = chat_model
        self._retrieve = retrieve
        self._max_loop_turns = max_loop_turns
        self._max_model_calls = max_model_calls_per_turn
        self._max_tool_calls = max_tool_calls_per_turn
        self._maximum_top_k = maximum_top_k
        self._execution_timeout = execution_timeout_seconds
        builder = StateGraph(RunState)
        builder.add_node("turn", self.turn)
        builder.add_node("chat", self._chat)
        builder.add_node("output", self._output)
        builder.add_edge(START, "turn")
        builder.add_conditional_edges("turn", self._route, {"continue": "turn", "complete": "chat"})
        builder.add_edge("chat", "output")
        builder.add_edge("output", END)
        self._graph = builder.compile()

    @property
    def model_name(self) -> str:
        return self._chat_model.model_name

    def kick(
        self,
        inputs: RunInputs,
        *,
        emit: Callable[[dict], None] | None = None,
        cancellation: Event | None = None,
    ) -> VideoChunkSearchResult:
        if (not inputs["workspace_id"].strip() or not inputs["user_message"].strip()
            or not inputs["series_ids"] or any(not value.strip() for value in inputs["series_ids"])):
            raise ValueError("Kick requires user input and fixed workspace / Series scope")
        top_k = inputs["requested_top_k"]
        if isinstance(top_k, bool) or not isinstance(top_k, int) or not 1 <= top_k <= 100:
            raise ValueError("requested_top_k must be between 1 and 100")
        control = ExecutionControl(monotonic() + self._execution_timeout, cancellation, emit)
        control.check()
        result = self._graph.invoke(
            {"inputs": deepcopy(inputs), "turns": [], "control": control},
            config={"recursion_limit": self._max_loop_turns + 5},
        )
        control.check()
        return result["final_output"]

    def turn(self, state: RunState) -> dict:
        control, inputs, previous = state["control"], state["inputs"], state["turns"]
        control.check()
        if len(previous) >= self._max_loop_turns:
            raise AgentModelInvocationError("Query / Evaluator loop-turn budget exceeded")
        calls = []
        tool = build_retrieval_tool(
            self._retrieve, inputs=inputs, control=control, calls=calls,
            maximum_top_k=self._maximum_top_k, max_tool_calls=self._max_tool_calls,
        )
        selection_limit = min(inputs["requested_top_k"], self._maximum_top_k)
        draft = run_query(
            self._query_model, inputs, previous,
            tools=[tool], max_model_calls=self._max_model_calls, control=control,
            selection_limit=selection_limit,
        )
        control.check()
        available, selected, sources = bind_draft(
            inputs, previous, calls, draft, max_selected_hits=selection_limit,
        )
        review = evaluate(
            self._evaluator_model, inputs, previous, draft, available, selected, calls, sources,
        )
        control.check()
        current = LoopTurn(
            number=len(previous) + 1, plan_context=draft.plan_context,
            retrieval_calls=tuple(calls), retrieval_result=available,
            evidence_source_turns=sources, query_draft=draft, draft_output=selected,
            review=review, status=review.action,
        )
        if review.action == "continue" and current.number >= self._max_loop_turns:
            raise AgentModelInvocationError(
                f"Evaluator rejected the Query draft after {current.number} turns: {review.advice}"
            )
        return {"turns": [*previous, current]}

    @staticmethod
    def _route(state: RunState) -> str:
        return state["turns"][-1].review.action

    def _chat(self, state: RunState) -> dict:
        current = state["turns"][-1]
        text = render_chat(self._chat_model, state["inputs"], current, state["control"])
        return {"turns": [*state["turns"][:-1], replace(current, rendered_text=text)]}

    @staticmethod
    def _output(state: RunState) -> dict:
        state["control"].check()
        return {"final_output": finalize_output(state["turns"][-1], state["inputs"])}
