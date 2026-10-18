from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from .scheduler import ChunkScheduler
from .understanding import ChunkUnderstandingWorkflow
from .errors import ParseError
from .prefix import PrefixContextAgent
from .refine import IntervalRefine
from .state import (
    VideoTaskStatus,
)
from .state import RunState
from .prepare import VideoParsePrepare
from .voiceprint import VoiceprintAgent


class ParseAgent:
    """Resume one Video task from its durable stage and coordinate its main path."""

    def __init__(
        self,
        *,
        runtime,
    ) -> None:
        self.runtime = runtime
        self.prefix_context_agent = PrefixContextAgent(
            runtime=self.runtime,
        )
        self.interval_refine = IntervalRefine(
            runtime=self.runtime,
        )
        self.chunk_workflow = ChunkUnderstandingWorkflow(
            runtime=self.runtime,
        )
        self.scheduler = ChunkScheduler(runtime=self.runtime)
        self.voiceprint_agent = VoiceprintAgent(runtime=self.runtime)
        self.prepare = VideoParsePrepare(
            runtime=self.runtime,
        )

        builder = StateGraph(RunState)
        builder.add_node("load", self._load)
        builder.add_node("prepare", self.prepare.run)
        builder.add_node("schedule_chunks", self.scheduler.run)
        builder.add_node("voiceprint", self._prepare_voiceprint)
        builder.add_node("wait_for_completion", self._wait_for_completion)
        builder.add_edge(START, "load")
        builder.add_conditional_edges(
            "load",
            self._route_from_persisted_stage,
            {
                "prepare": "prepare",
                "schedule": "schedule_chunks",
                "wait": "wait_for_completion",
                "done": END,
            },
        )
        builder.add_edge("prepare", "schedule_chunks")
        builder.add_edge("schedule_chunks", "voiceprint")
        builder.add_edge("voiceprint", "wait_for_completion")
        builder.add_edge("wait_for_completion", END)
        self._graph = builder.compile()

    async def InvokeVideoParse(self, video_parse_task_id: str) -> RunState:
        """Run or resume an existing task without replaying committed stages."""

        return await self._graph.ainvoke({
            "video_parse_task_id": video_parse_task_id,
        })

    async def _prepare_voiceprint(self, state: RunState) -> RunState:
        if state["inputs"].parse_config.voiceprint_mode != "off":
            await self.runtime.prepare_voiceprint_scoring(
                state["video_parse_task_id"], state["lease"],
            )
        elif state["inputs"].parse_config.voiceprint is not None:
            await self.voiceprint_agent.InvokeVoiceprint(
                state["video_parse_task_id"], state["lease"],
            )
        return state

    async def _load(self, state: RunState) -> RunState:
        snapshot = await self.runtime.load_video_run(state["video_parse_task_id"])
        if snapshot.status in {
            VideoTaskStatus.PENDING,
            VideoTaskStatus.PREPARING,
            VideoTaskStatus.PARSING,
            VideoTaskStatus.INDEXING,
        } and snapshot.lease is None:
            raise ParseError("Active Video task is missing its lease fence", retryable=False)
        result: RunState = {
            **state,
            "inputs": snapshot.inputs,
            "duration_seconds": snapshot.duration_seconds,
            "status": snapshot.status,
            "video_chunks": snapshot.chunks,
        }
        if snapshot.lease is not None:
            result["lease"] = snapshot.lease
        return result

    @staticmethod
    def _route_from_persisted_stage(state: RunState) -> str:
        status = state["status"]
        if status in {VideoTaskStatus.PENDING, VideoTaskStatus.PREPARING}:
            return "prepare"
        if status == VideoTaskStatus.PARSING:
            return "schedule"
        if status == VideoTaskStatus.INDEXING:
            return "wait"
        if status == VideoTaskStatus.COMPLETE:
            return "done"
        failure_stage = (
            "indexing"
            if status == VideoTaskStatus.INDEXING_FAILED
            else "parsing"
        )
        raise ParseError(
            f"Video task has an unrecovered {failure_stage} failure",
            retryable=False,
        )

    async def _wait_for_completion(self, state: RunState) -> RunState:
        task_id = state["video_parse_task_id"]
        lease = state.get("lease")
        if lease is None:
            raise ParseError("Active Video task is missing its lease fence", retryable=False)
        while True:
            status = await self.runtime.advance_video_task(task_id, lease)
            state = {**state, "status": status}
            if status == VideoTaskStatus.COMPLETE:
                return state
            if status == VideoTaskStatus.PARSING_FAILED:
                raise ParseError("Video parsing failed", retryable=False)
            if status == VideoTaskStatus.INDEXING_FAILED:
                raise ParseError("Video indexing failed", retryable=False)
            await self.runtime.wait_for_video_change(task_id, timeout_seconds=1.0)
