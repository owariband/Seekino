from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from dataclasses import replace
from math import isfinite
from threading import Event
from time import monotonic

from engine.rag.contracts import VideoChunkSearchResult
from engine.rag.retrieval.contracts import EventSearchResult
from gateway.agent_model.contracts import StructuredToolAgentModel
from gateway.agent_model.errors import ModelFailureError, ModelFailureReason
from langchain.agents.middleware.model_call_limit import ModelCallLimitExceededError
from langgraph.graph import END, START, StateGraph

from agent.utils.tool import AgentTool
from ..common.errors import AgentContextBudgetError, AgentModelInvocationError
from ..common.tool import as_langchain_tools, attach_agent_tool_logging
from .errors import VideoRetrievalError, VideoSearchFailure, VideoSearchFailureCode, VideoSearchFailurePhase
from .frame_evidence import FrameInspectionSession, FrameReader
from .output import merge_retrieval_calls
from .query import run_query
from .state import (
    AnswerPhase, ExecutionControl, QueryReport, RetrievalCall,
    RetrievalChannelContract, RunInputs, RunLimits, RunState,
)
from .skills import LEXICAL_RETRIEVAL_SKILL_PROMPT, RETRIEVAL_SKILL_PROMPT
from .tools.registry import build_retrieval_tools
from .tools.shared import _ToolCallBudget


class AgentRuntime:
    """Investigate one delegated question and return evidence to the Supervisor.

    Query owns its tool loop. LangGraph binds and validates the completed
    investigation; it never starts a second reviewer or renders a user answer.
    """

    def __init__(
        self,
        query_model: StructuredToolAgentModel,
        retrieve: Callable[..., VideoChunkSearchResult],
        *,
        max_model_calls_per_turn: int = 6,
        max_tool_calls_per_turn: int = 24,
        limits_enabled: bool = False,
        total_timeout_enabled: bool = True,
        max_prompt_chars: int = 200_000,
        max_tool_result_bytes: int = 16_384,
        max_read_chars_per_call: int = 16_384,
        max_catalog_items_per_page: int = 20,
        stage_timeout_seconds: float = 480,
        execution_timeout_seconds: float = 480,
        retrieval_channel: RetrievalChannelContract = "transcript",
        frame_reader: FrameReader | None = None,
        frame_max_height: int | None = None,
        maximum_frame_inspections: int | None = None,
        event_retrieve: Callable[..., EventSearchResult] | None = None,
    ) -> None:
        for name, value, maximum in (
            ("max_model_calls_per_turn", max_model_calls_per_turn, None),
            ("max_tool_calls_per_turn", max_tool_calls_per_turn, None),
            ("max_prompt_chars", max_prompt_chars, None),
            ("max_tool_result_bytes", max_tool_result_bytes, None),
            ("max_read_chars_per_call", max_read_chars_per_call, None),
            ("max_catalog_items_per_page", max_catalog_items_per_page, 100),
        ):
            _positive_run_limit(name, value)
            if maximum is not None and value > maximum:
                raise ValueError(f"{name} must be between 1 and {maximum}")
        if max_tool_result_bytes < 512:
            raise ValueError("max_tool_result_bytes must be at least 512")
        if not isinstance(limits_enabled, bool) or not isinstance(total_timeout_enabled, bool):
            raise ValueError("limits_enabled and total_timeout_enabled must be booleans")
        if retrieval_channel not in ("transcript", "lexical"):
            raise ValueError("retrieval_channel must be transcript or lexical")
        for name, value in (("stage_timeout_seconds", stage_timeout_seconds),
                            ("execution_timeout_seconds", execution_timeout_seconds)):
            if isinstance(value, bool) or not isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be positive and finite")
        if frame_reader is not None:
            for name, value in (("frame_max_height", frame_max_height),
                                ("maximum_frame_inspections", maximum_frame_inspections)):
                _positive_run_limit(name, value)
        self._query_model = query_model
        self._retrieve = retrieve
        self._event_retrieve = event_retrieve
        self._max_model_calls = max_model_calls_per_turn
        self._max_tool_calls = max_tool_calls_per_turn
        self._limits_enabled = limits_enabled
        self._total_timeout_enabled = total_timeout_enabled
        self._max_prompt_chars = max_prompt_chars
        self._max_tool_result_bytes = max_tool_result_bytes
        self._max_read_chars_per_call = max_read_chars_per_call
        self._max_catalog_items = max_catalog_items_per_page
        self._stage_timeout = stage_timeout_seconds
        self._execution_timeout = execution_timeout_seconds
        self._retrieval_channel = retrieval_channel
        self._frame_reader = frame_reader
        self._frame_max_height = frame_max_height
        self._maximum_frame_inspections = maximum_frame_inspections
        self._retrieval_skill_prompt = (
            LEXICAL_RETRIEVAL_SKILL_PROMPT if retrieval_channel == "lexical"
            else RETRIEVAL_SKILL_PROMPT
        )
        builder = StateGraph(RunState)
        builder.add_node("query", self._query)
        builder.add_node("validate_evidence", self._validate_evidence)
        builder.add_node("report", self._report)
        builder.add_edge(START, "query")
        builder.add_edge("query", "validate_evidence")
        builder.add_edge("validate_evidence", "report")
        builder.add_edge("report", END)
        # A worker owns its evidence, controls and ephemeral model session.
        self._graph = builder.compile(checkpointer=False)

    @property
    def model_name(self) -> str:
        return self._query_model.model_name

    def close(self) -> None:
        close_model = getattr(self._query_model, "close", None)
        if callable(close_model):
            close_model()

    def new_frame_session(self) -> FrameInspectionSession | None:
        if self._frame_reader is None:
            return None
        return FrameInspectionSession(
            self._maximum_frame_inspections if self._limits_enabled else None,
        )

    def build_supervisor_tools(
        self, inputs: RunInputs,
        control: ExecutionControl, calls: list[RetrievalCall], *,
        maximum_tool_calls: int | None = None, supports_vision_input: bool = False,
        frame_session: FrameInspectionSession | None = None, budget: _ToolCallBudget | None = None,
        maximum_result_bytes: int | None = None,
    ) -> list[AgentTool]:
        return build_retrieval_tools(
            self._retrieve, role="supervisor", event_retrieve=self._event_retrieve,
            inputs=inputs, control=control, calls=calls,
            maximum_tool_calls=maximum_tool_calls,
            maximum_result_bytes=min(self._max_tool_result_bytes, maximum_result_bytes)
            if maximum_result_bytes is not None else self._max_tool_result_bytes,
            maximum_catalog_items=self._max_catalog_items,
            maximum_read_chars=self._max_read_chars_per_call,
            retrieval_channel=self._retrieval_channel, frame_reader=self._frame_reader,
            frame_session=frame_session, frame_max_height=self._frame_max_height,
            supports_vision_input=supports_vision_input, budget=budget,
        )

    def kick(
        self, inputs: RunInputs, *, emit: Callable[[dict], None] | None = None,
        cancellation: Event | None = None, max_model_calls_per_turn: int | None = None,
        max_tool_calls_per_turn: int | None = None, deadline: float | None = None,
        tool_budget: _ToolCallBudget | None = None,
    ) -> QueryReport:
        if (not inputs["tenant_id"].strip() or not inputs["user_message"].strip()
            or not inputs["series_ids"] or any(not value.strip() for value in inputs["series_ids"])):
            raise ValueError("Query requires user input and fixed workspace / Series scope")
        top_k = inputs["requested_top_k"]
        if type(top_k) is not int or not 1 <= top_k <= 100:
            raise ValueError("requested_top_k must be between 1 and 100")
        limits = RunLimits(
            max_model_calls_per_turn=self._run_limit(
                "max_model_calls_per_turn", max_model_calls_per_turn, self._max_model_calls,
            ),
            max_tool_calls_per_turn=self._run_limit(
                "max_tool_calls_per_turn", max_tool_calls_per_turn, self._max_tool_calls,
            ),
        )
        run_deadline = monotonic() + self._execution_timeout if self._total_timeout_enabled else None
        if deadline is not None:
            if isinstance(deadline, bool) or not isfinite(deadline) or deadline <= 0:
                raise ValueError("deadline must be positive and finite")
            run_deadline = deadline if run_deadline is None else min(run_deadline, deadline)
        control = ExecutionControl(run_deadline, cancellation, emit)
        control.check()
        result = self._graph.invoke({
            "inputs": deepcopy(inputs),
            "frame_inspections": self.new_frame_session(), "calls": [],
            "control": control, "limits": limits, "tool_budget": tool_budget,
        })
        control.check()
        return result["report"]

    def _run_limit(self, name: str, override: int | None, configured: int) -> int | None:
        if override is not None:
            return _positive_run_limit(name, override)
        return configured if self._limits_enabled else None

    def _query(self, state: RunState) -> dict:
        control, inputs = state["control"], state["inputs"]
        control.publish(inputs, {"type": "answer.progress", "phase": AnswerPhase.RETRIEVING})
        stage_deadline = monotonic() + self._stage_timeout
        query_control = replace(control, deadline=(
            stage_deadline if control.deadline is None else min(control.deadline, stage_deadline)
        ))
        tools = build_retrieval_tools(
            self._retrieve, role="query", event_retrieve=self._event_retrieve,
            inputs=inputs, control=query_control, calls=state["calls"],
            maximum_tool_calls=state["limits"].max_tool_calls_per_turn,
            maximum_result_bytes=self._max_tool_result_bytes,
            maximum_read_chars=self._max_read_chars_per_call,
            maximum_catalog_items=self._max_catalog_items, retrieval_channel=self._retrieval_channel,
            frame_reader=self._frame_reader, frame_session=state["frame_inspections"],
            frame_max_height=self._frame_max_height,
            supports_vision_input=getattr(self._query_model, "supports_vision_input", False),
            budget=state["tool_budget"],
        )
        attach_agent_tool_logging(tools, role="Query", inputs=inputs)
        try:
            completion = run_query(
                self._query_model, inputs, tools=as_langchain_tools(tools),
                max_model_calls=state["limits"].max_model_calls_per_turn,
                control=query_control, max_prompt_chars=self._max_prompt_chars,
                calls=state["calls"], frame_session=state["frame_inspections"],
                retrieval_skill_prompt=self._retrieval_skill_prompt,
            )
            query_control.check()
            control.check()
        except Exception as exc:
            _raise_classified_failure(
                exc, phase=VideoSearchFailurePhase.QUERY,
                stage_control=query_control, run_control=control,
            )
        return {"completion": completion}

    @staticmethod
    def _validate_evidence(state: RunState) -> dict:
        state["control"].check()
        if not state["calls"]:
            raise VideoSearchFailure(
                VideoSearchFailureCode.REQUIRED_TOOL_MISSING,
                VideoSearchFailurePhase.QUERY, retryable=True,
            )
        if all(call.status == "error" for call in state["calls"]):
            raise VideoSearchFailure(
                VideoSearchFailureCode.RETRIEVAL_FAILED,
                VideoSearchFailurePhase.RETRIEVAL, retryable=True,
            )
        return {"retrieval_result": merge_retrieval_calls(state["inputs"], state["calls"])}

    @staticmethod
    def _report(state: RunState) -> dict:
        state["control"].check()
        return {"report": QueryReport(
            completion=state["completion"],
            retrieval_result=state["retrieval_result"],
            retrieval_calls=tuple(state["calls"]),
            frame_receipts=tuple(state["frame_inspections"].summary()) if state["frame_inspections"] else (),
        )}


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
