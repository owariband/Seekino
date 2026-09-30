from __future__ import annotations

from engine.parsev2.validation import (
    PrefixRefinementProposal,
    RefinementMember,
    ValidatedPrefixRefinement,
    validate_prefix_refinement,
)

from ..chunk_interval_listener import ChunkIntervalListener
from ..prompts import build_prefix_prompt
from ..runtime import VideoParseRuntime
from .context import PrefixContext
from .tools import build_tools


class PrefixContextAgent:
    """Optional background Agent bound to one persisted prefix cursor target."""

    def __init__(
        self,
        *,
        runtime: VideoParseRuntime,
        listener: ChunkIntervalListener,
    ) -> None:
        self._runtime = runtime
        self._listener = listener

    async def run(self, chunk_parse_task_id: str) -> None:
        context = await self._runtime.load_prefix_context(chunk_parse_task_id)
        if not context["inputs"].parse_config.compact_enabled:
            return

        prefix_config = context["inputs"].parse_config.prefix_context
        if prefix_config is None:
            raise ValueError("compact-enabled task is missing prefix_context config")
        prompt = build_prefix_prompt(
            sequence_no=context["sequence_no"],
            scope=context["scope"],
            content=context["content"],
            series_prefix_compact=context["series_prefix_compact"],
            video_prefix_compact=context["video_prefix_compact"],
        )
        proposal = await self._runtime.generate_prefix_refinement(
            context,
            prompt=prompt,
            tools=build_tools(context),
        )
        result = self._validate_result(context, proposal)
        await self._runtime.commit_prefix_refinement(context, result)
        await self._runtime.notify_prefix_committed(context)
        await self._listener.run_prefix_context_if_ready(context["inputs"].series_id)

    @staticmethod
    def _validate_result(
        context: PrefixContext,
        proposal: PrefixRefinementProposal,
    ) -> ValidatedPrefixRefinement:
        prefix_config = context["inputs"].parse_config.prefix_context
        if prefix_config is None:
            raise ValueError("prefix_context config is required")
        member = RefinementMember(
            sequence_no=context["sequence_no"],
            start_seconds=context["allowed_start_seconds"],
            end_seconds=context["allowed_end_seconds"],
            content=context["content"],
        )
        return validate_prefix_refinement(
            proposal,
            member=member,
            series_compact_max_chars=prefix_config.series_compact_max_chars,
            video_compact_max_chars=prefix_config.video_compact_max_chars,
        )
