"""Video Parse V2 domain algorithms and policies."""

from .planning import ChunkPlan, IntervalPlan, interval_size, plan_chunks, plan_intervals
from .schemas import VideoParseResult
from .validation import (
    ChunkResultUpdate,
    IntervalRefinementProposal,
    PrefixRefinementProposal,
    RefinementMember,
    ValidatedIntervalRefinement,
    ValidatedPrefixRefinement,
    apply_chunk_update,
    ordered_scope_union,
    validate_interval_refinement,
    validate_prefix_refinement,
    validate_result_time_range,
)

__all__ = [
    "ChunkPlan",
    "ChunkResultUpdate",
    "IntervalPlan",
    "IntervalRefinementProposal",
    "PrefixRefinementProposal",
    "RefinementMember",
    "ValidatedIntervalRefinement",
    "ValidatedPrefixRefinement",
    "VideoParseResult",
    "apply_chunk_update",
    "interval_size",
    "ordered_scope_union",
    "plan_chunks",
    "plan_intervals",
    "validate_interval_refinement",
    "validate_prefix_refinement",
    "validate_result_time_range",
]
