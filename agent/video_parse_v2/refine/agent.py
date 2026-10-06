from __future__ import annotations

from engine.parsev2.validation import (
    IntervalRefinementProposal,
    RefinementMember,
    ValidatedIntervalRefinement,
    validate_interval_refinement,
    validate_member_scopes,
)

from ..listener import ChunkIntervalListener
from ..prompts import build_refine_prompt
from ..host import VideoParseRuntime
from .input import RefineInput


class IntervalRefine:
    """Independently refine one claimed complete Interval member set."""

    def __init__(
        self,
        *,
        runtime: VideoParseRuntime,
        listener: ChunkIntervalListener,
    ) -> None:
        self._runtime = runtime
        self._listener = listener

    async def run(self, interval_parse_task_id: str) -> None:
        snapshot = await self._runtime.load_refine_input(interval_parse_task_id)
        proposal = await self._generate_refinement(snapshot)
        result = self._validate_refinement(snapshot, proposal)
        await self._runtime.commit_interval_refinement(snapshot, result)
        await self._runtime.notify_interval_committed(snapshot)
        await self._listener.run_prefix_context_if_ready(snapshot["inputs"].series_id)

    async def _generate_refinement(
        self,
        snapshot: RefineInput,
    ) -> IntervalRefinementProposal:
        ordered_ids = sorted(
            snapshot["member_sequence_nos"],
            key=snapshot["member_sequence_nos"].__getitem__,
        )
        members = [
            {
                "sequence_no": snapshot["member_sequence_nos"][task_id],
                "scope": snapshot["member_scopes"][task_id],
                "content_version": snapshot["member_versions"][task_id],
                "content": snapshot["member_contents"][task_id].model_dump(mode="json"),
            }
            for task_id in ordered_ids
        ]
        prompt = build_refine_prompt(
            start_sequence_no=snapshot["start_sequence_no"],
            chunk_count=snapshot["chunk_count"],
            members=members,
        )
        return await self._runtime.generate_interval_refinement(
            snapshot,
            prompt=prompt,
        )

    @staticmethod
    def _validate_refinement(
        snapshot: RefineInput,
        proposal: IntervalRefinementProposal,
    ) -> ValidatedIntervalRefinement:
        expected_sequences = list(range(
            snapshot["start_sequence_no"],
            snapshot["start_sequence_no"] + snapshot["chunk_count"],
        ))
        mappings = (
            snapshot["member_sequence_nos"],
            snapshot["member_bounds"],
            snapshot["member_contents"],
            snapshot["member_versions"],
            snapshot["member_scopes"],
        )
        member_ids = set(snapshot["member_sequence_nos"])
        if any(set(mapping) != member_ids for mapping in mappings[1:]):
            raise ValueError("Refine member maps must have identical task IDs")
        if sorted(snapshot["member_sequence_nos"].values()) != expected_sequences:
            raise ValueError("Refine members do not match the fixed Interval range")

        scopes_by_sequence = {
            snapshot["member_sequence_nos"][task_id]: scope
            for task_id, scope in snapshot["member_scopes"].items()
        }
        validate_member_scopes(
            scopes_by_sequence,
            member_sequence_nos=expected_sequences,
        )
        members = sorted(
            (
                RefinementMember(
                    sequence_no=snapshot["member_sequence_nos"][task_id],
                    start_seconds=snapshot["member_bounds"][task_id][0],
                    end_seconds=snapshot["member_bounds"][task_id][1],
                    content=snapshot["member_contents"][task_id],
                )
                for task_id in member_ids
            ),
            key=lambda member: member.sequence_no,
        )
        return validate_interval_refinement(proposal, members=members)
