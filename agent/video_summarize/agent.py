from __future__ import annotations

from collections.abc import Callable

from agent.utils.tool import AgentTool

from .prompts import build_summarize_prompt
from .state import SummaryCanvas, VideoSummarizeInput, VideoSummarizeLimits, VideoSummaryResult
from .tools import build_tools


class VideoSummarizeAgent:
    def __init__(self, *, runtime) -> None:
        self._runtime = runtime

    async def InvokeSummarize(
        self, inputs: VideoSummarizeInput, *, inspect_video_frames: AgentTool,
        limits: VideoSummarizeLimits, check_control: Callable[[], None],
    ) -> VideoSummaryResult:
        check_control()
        canvas = SummaryCanvas(inputs)
        tools = build_tools(
            canvas, inputs=inputs, inspect_video_frames=inspect_video_frames,
            limits=limits, check_control=check_control,
        )
        await self._runtime.generate_video_summary(
            inputs, prompt=build_summarize_prompt(inputs, limits), tools=tools,
        )
        check_control()
        if canvas.result is None:
            raise RuntimeError("Video summary model finished without a successful submit_video_summary")
        return canvas.result
