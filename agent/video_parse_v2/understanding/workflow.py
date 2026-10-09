"""Prepare observations, build a Canvas draft, and commit validated facts once."""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from agent.common.chunk_understanding import understand_chunk
from engine.parsev2.schemas import VideoParseResult

from ..timelineEvent_canvas import ParseContextCanvas
from ..prompts import build_canvas_understanding_prompt
from .state import ChunkState


class ChunkUnderstandingWorkflow:
    """Prepare ASR and visual inputs concurrently, validate, and commit once."""

    def __init__(
        self,
        *,
        runtime,
    ) -> None:
        self._runtime = runtime

        builder = StateGraph(ChunkState)
        builder.add_node("understanding", self._understand)
        builder.add_node("commit", self._commit)
        builder.add_node("notify", self._notify)
        builder.add_edge(START, "understanding")
        builder.add_edge("understanding", "commit")
        builder.add_edge("commit", "notify")
        builder.add_edge("notify", END)
        self._graph = builder.compile()

    async def run(self, chunk_parse_task_id: str) -> None:
        """Run an already claimed task and always release its temporary media."""

        execution = await self._runtime.load_chunk_execution(chunk_parse_task_id)
        caught: BaseException | None = None
        try:
            await self._graph.ainvoke({
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
        observations = {}

        def preserve_observations(asr_result, ocr_result):
            observations.update(asr_result=asr_result, ocr_result=ocr_result)
            canvas.register_evidence(execution.chunk_parse_task_id, {
                "asr": asr_result,
                "ocr": ocr_result.model_dump(mode="json") if ocr_result is not None else None,
            })

        result = await understand_chunk(
            start_ms=execution.allowed_start_seconds,
            end_ms=execution.allowed_end_seconds,
            ocr_enabled=config.ocr_enabled,
            understanding_enabled=config.understanding_enabled,
            prepare_visual=lambda: self._runtime.prepare_chunk_visual(execution),
            transcribe=lambda: self._runtime.transcribe_chunk(execution),
            read_text=lambda: self._runtime.read_chunk_text(execution),
            understand=lambda **kwargs: self._runtime.understand_chunk(
                execution, canvas=canvas, **kwargs,
            ),
            prompt_builder=lambda **kwargs: build_canvas_understanding_prompt(execution, canvas=canvas),
            observation_callback=preserve_observations,
        )
        if not config.understanding_enabled:
            operations = [
                {"op": "add_event", "member_ref": execution.chunk_parse_task_id,
                 "key": f"observation-{index}", "event": event.model_dump(mode="json")}
                for index, event in enumerate(result.timeline)
            ]
            if operations:
                canvas.write(role="understanding", batch_id="normalized-observations",
                             expected_revision=canvas.revision, operations=operations)
        return {"result": canvas.result(execution.chunk_parse_task_id), "canvas": canvas, **observations}

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
