from __future__ import annotations

from ..timelineEvent_canvas import CanvasCompletion, ParseContextCanvas
from ..prompts import build_prefix_prompt


class PrefixContextAgent:
    """Maintain summaries and facts for the bound current prefix target."""

    def __init__(self, *, runtime) -> None:
        self._runtime = runtime

    async def run(self, chunk_parse_task_id: str) -> None:
        while True:
            context = await self._runtime.load_prefix_context(chunk_parse_task_id)
            if not context["inputs"].parse_config.compact_enabled:
                return
            if context["inputs"].parse_config.prefix_context is None:
                raise ValueError("compact-enabled task is missing prefix_context config")
            canvas = ParseContextCanvas.from_prefix(context)
            receipt = await self._runtime.generate_prefix_refinement(
                context, canvas=canvas,
                prompt=build_prefix_prompt(
                    sequence_no=context["sequence_no"], scope=context["scope"], canvas=canvas,
                    max_tool_calls=context["inputs"].parse_config.prefix_context.max_tool_calls,
                ),
            )
            CanvasCompletion.model_validate(receipt)
            committed = await self._runtime.commit_prefix_refinement(context, canvas.prefix_result())
            if committed is False:
                continue
            await self._runtime.notify_prefix_committed(context)
            return
