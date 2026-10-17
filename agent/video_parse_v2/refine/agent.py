from __future__ import annotations

from engine.parsev2.validation import validate_member_scopes

from ..timelineEvent_canvas import CanvasCompletion, ParseContextCanvas
from ..prompts import build_refine_prompt
from .input import RefineInput


class IntervalRefine:
    """Review one bound snapshot and refresh only refinement after a conflict."""

    def __init__(self, *, runtime) -> None:
        self._runtime = runtime

    async def InvokeRefine(self, interval_parse_task_id: str) -> None:
        while True:
            snapshot = await self._runtime.load_refine_input(interval_parse_task_id)
            self._validate_snapshot(snapshot)
            canvas = ParseContextCanvas.from_refine(snapshot)
            receipt = await self._runtime.generate_interval_refinement(
                snapshot, canvas=canvas,
                prompt=build_refine_prompt(
                    start_sequence_no=snapshot["start_sequence_no"],
                    chunk_count=snapshot["chunk_count"], canvas=canvas,
                ),
            )
            CanvasCompletion.model_validate(receipt)
            result = canvas.interval_result()
            committed = await self._runtime.commit_interval_refinement(snapshot, result)
            if committed is False:
                # The worker retains its claim and completed observations. Only
                # the now-stale refinement is reviewed against fresh content.
                continue
            await self._runtime.notify_interval_committed(snapshot)
            return

    @staticmethod
    def _validate_snapshot(snapshot: RefineInput) -> None:
        expected = list(range(snapshot["start_sequence_no"], snapshot["start_sequence_no"] + snapshot["chunk_count"]))
        maps = [snapshot[name] for name in (
            "member_sequence_nos", "member_bounds", "member_contents", "member_versions", "member_scopes",
        )]
        if any(set(mapping) != set(maps[0]) for mapping in maps[1:]):
            raise ValueError("Refine member maps must have identical task IDs")
        if sorted(maps[0].values()) != expected:
            raise ValueError("Refine members do not match the fixed Interval range")
        validate_member_scopes(
            {maps[0][task_id]: scope for task_id, scope in snapshot["member_scopes"].items()},
            member_sequence_nos=expected,
        )
