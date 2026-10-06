"""Executable Chunk subgraph with all external effects delegated to the host."""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from engine.parsev2.validation import validate_result_time_range

from ..listener import ChunkIntervalListener
from ..prompts import build_understanding_prompt
from ..host import VideoParseRuntime
from .state import ChunkState


class ChunkUnderstandingWorkflow:
    """Prepare ASR and visual inputs concurrently, validate, and commit once."""

    def __init__(
        self,
        *,
        runtime: VideoParseRuntime,
        listener: ChunkIntervalListener,
    ) -> None:
        self._runtime = runtime
        self._listener = listener

        builder = StateGraph(ChunkState)
        builder.add_node("visual_input", self._visual_input)
        builder.add_node("asr", self._transcribe)
        builder.add_node("understanding", self._understand)
        builder.add_node("validate", self._validate)
        builder.add_node("commit", self._commit)
        builder.add_node("notify", self._notify)
        builder.add_edge(START, "visual_input")
        builder.add_edge(START, "asr")
        builder.add_edge(["asr", "visual_input"], "understanding")
        builder.add_edge("understanding", "validate")
        builder.add_edge("validate", "commit")
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

    async def _visual_input(self, state: ChunkState) -> dict[str, object]:
        execution = state["execution"]
        await self._runtime.prepare_chunk_visual(execution)
        updates: dict[str, object] = {"visual_ready": True}
        if execution.inputs.parse_config.ocr_enabled:
            updates["ocr_result"] = await self._runtime.read_chunk_text(execution)
        return updates

    async def _transcribe(self, state: ChunkState) -> dict[str, object]:
        return {
            "asr_result": await self._runtime.transcribe_chunk(state["execution"])
        }

    async def _understand(self, state: ChunkState) -> dict[str, object]:
        execution = state["execution"]
        asr_result = state["asr_result"]
        ocr_result = state.get("ocr_result")
        if execution.inputs.parse_config.understanding_enabled:
            prompt = build_understanding_prompt(
                execution,
                asr_result=asr_result,
                ocr_result=ocr_result,
            )
            result = await self._runtime.understand_chunk(
                execution,
                asr_result=asr_result,
                ocr_result=ocr_result,
                prompt=prompt,
            )
        else:
            result = self._runtime.build_chunk_without_understanding(
                execution,
                asr_result=asr_result,
                ocr_result=ocr_result,
            )
        return {"result": result}

    @staticmethod
    def _validate(state: ChunkState) -> dict[str, object]:
        execution = state["execution"]
        result = validate_result_time_range(
            state["result"],
            start_seconds=execution.allowed_start_seconds,
            end_seconds=execution.allowed_end_seconds,
        )
        return {"result": result}

    async def _commit(self, state: ChunkState) -> dict[str, object]:
        await self._runtime.commit_chunk_result(
            state["execution"],
            state["result"],
            self._listener.update_interval_mask,
        )
        return {}

    async def _notify(self, state: ChunkState) -> dict[str, object]:
        await self._runtime.notify_chunk_committed(state["execution"])
        return {}
