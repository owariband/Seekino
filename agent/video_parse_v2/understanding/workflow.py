"""Prepare observations, build a Canvas draft, and commit validated facts once."""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph
from langgraph.types import RetryPolicy

from gateway.agent_model.errors import ModelFailureError, ModelFailureReason
from httpx import TransportError

from engine.parsev2.observations import build_chunk_without_understanding
from engine.parsev2.validation import validate_result_time_range
from engine.parsev2.schemas import VideoParseResult

from ..timelineEvent_canvas import ParseContextCanvas
from ..prompts import build_understanding_prompt
from .state import ChunkState


class ChunkUnderstandingWorkflow:
    """Prepare ASR and visual inputs concurrently, validate, and commit once."""

    def __init__(
        self,
        *,
        runtime,
    ) -> None:
        self._runtime = runtime

    def _graph_for(self, execution):
        config = execution.inputs.parse_config.execution
        policy = RetryPolicy(
            max_attempts=config.max_retry_count + 1,
            initial_interval=config.retry_initial_delay_seconds,
            max_interval=config.retry_max_delay_seconds,
            jitter=False,
            retry_on=_retryable_node_error,
        )
        builder = StateGraph(ChunkState)
        for name, callback in (("asr", self._asr), ("visual", self._visual),
                               ("ocr", self._ocr), ("understanding", self._understand)):
            builder.add_node(name, _annotate_stage(name, callback), retry_policy=policy)
        # Commit/notify are not model operations; never replay them automatically.
        builder.add_node("commit", _annotate_stage("commit", self._commit))
        builder.add_node("notify", self._notify)
        builder.add_edge(START, "asr")
        builder.add_edge(START, "visual")
        builder.add_edge("visual", "ocr")
        builder.add_edge(["asr", "ocr"], "understanding")
        builder.add_edge("understanding", "commit")
        builder.add_edge("commit", "notify")
        builder.add_edge("notify", END)
        return builder.compile()

    async def _asr(self, state):
        return {"asr_result": await self._runtime.transcribe_chunk(state["execution"])}

    async def _visual(self, state):
        await self._runtime.prepare_chunk_visual(state["execution"])
        return {"visual_ready": True}

    async def _ocr(self, state):
        execution = state["execution"]
        return {"ocr_result": await self._runtime.read_chunk_text(execution)
                if execution.inputs.parse_config.ocr_enabled else None}

    async def InvokeUnderstanding(self, chunk_parse_task_id: str) -> None:
        """Run an already claimed task and always release its temporary media."""

        execution = await self._runtime.load_chunk_execution(chunk_parse_task_id)
        caught: BaseException | None = None
        try:
            await self._graph_for(execution).ainvoke({
                "chunk_parse_task_id": chunk_parse_task_id,
                "execution": execution,
                "inputs": execution.inputs,
            })
        except BaseException as error:
            caught = error
            raise
        finally:
            try:
                await self._runtime.cleanup_chunk_execution(execution)
            except BaseException:
                if caught is None:
                    raise

    async def _understand(self, state: ChunkState) -> dict[str, object]:
        execution = state["execution"]
        config = execution.inputs.parse_config
        canvas = ParseContextCanvas(
            scope="chunk:" + execution.chunk_parse_task_id, stage="understanding",
            members={execution.chunk_parse_task_id: {
                "sequence_no": execution.sequence_no, "version": 0,
                "bounds": (execution.allowed_start_seconds, execution.allowed_end_seconds),
                "content": VideoParseResult(persons=[], timeline=[]),
            }},
            background=execution.background,
            evidence={"media": {
                "source_media_ref": execution.source_media_ref,
                "video_id": execution.inputs.video_id,
                "start_ms": execution.allowed_start_seconds,
                "end_ms": execution.allowed_end_seconds,
            }},
        )
        asr_result, ocr_result = state["asr_result"], state["ocr_result"]
        # A fresh draft per attempt prevents retaining a failed model result.
        canvas.register_evidence(execution.chunk_parse_task_id, {
            "asr": asr_result,
            "ocr": ocr_result.model_dump(mode="json") if ocr_result is not None else None,
        })
        if config.understanding_enabled:
            result = await self._runtime.understand_chunk(
                execution, asr_result=asr_result, ocr_result=ocr_result,
                prompt=build_understanding_prompt(
                    execution, asr_result=asr_result, ocr_result=ocr_result,
                ),
            )
        else:
            result = build_chunk_without_understanding(asr_result=asr_result, ocr_result=ocr_result)
        result = validate_result_time_range(
            result, start_seconds=execution.allowed_start_seconds,
            end_seconds=execution.allowed_end_seconds,
        )
        # Canvas remains the host's validated state, not a model tool workflow.
        # Import names and events together so no partial timeline is published.
        canvas.write(
            role="understanding", batch_id="initial-result",
            expected_revision=canvas.revision,
            operations=[
                {"op": "persons", "member_ref": execution.chunk_parse_task_id,
                 "action": "set", "values": result.persons},
                *({"op": "add_event", "member_ref": execution.chunk_parse_task_id,
                   "key": f"initial-{index}", "event": event.model_dump(mode="json")}
                  for index, event in enumerate(result.timeline)),
            ],
        )
        return {"result": canvas.result(execution.chunk_parse_task_id), "canvas": canvas}

    async def _commit(self, state: ChunkState) -> dict[str, object]:
        await self._runtime.commit_chunk_result(
            state["execution"],
            state["result"],
            evidence={
                **state["canvas"].get("evidence", state["execution"].chunk_parse_task_id),
                "media": state["canvas"].get("evidence", "media"),
            },
        )
        return {}

    async def _notify(self, state: ChunkState) -> dict[str, object]:
        await self._runtime.notify_chunk_committed(state["execution"])
        return {}


def _annotate_stage(stage, callback):
    async def invoke(state):
        try:
            return await callback(state)
        except Exception as error:
            error.parse_node_stage = stage
            raise
    return invoke


def _retryable_node_error(error: Exception) -> bool:
    # Cancellation, stale leases, invalid input and exhausted budgets are not
    # transient model failures. The surrounding execution deadline never resets.
    if isinstance(error, ModelFailureError):
        return error.reason == ModelFailureReason.MODEL_TIMEOUT
    return isinstance(error, (TimeoutError, ConnectionError, TransportError))
