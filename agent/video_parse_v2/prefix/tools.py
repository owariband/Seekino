"""Target-bound, non-persisting tools for the optional Prefix Agent."""

from __future__ import annotations

from typing import Annotated

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, ConfigDict, Field, model_validator

from engine.parsev2.schemas import TimelineEvent
from engine.parsev2.validation import (
    ChunkResultUpdate,
    apply_chunk_update,
    validate_result_time_range,
)

from .context import PrefixContext


_PersonName = Annotated[str, Field(min_length=1, pattern=r"\S")]


class _BoundChunkUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    persons: list[_PersonName] | None = None
    timeline: list[TimelineEvent] | None = None

    @model_validator(mode="after")
    def require_update(self) -> "_BoundChunkUpdate":
        if self.persons is None and self.timeline is None:
            raise ValueError("persons or timeline is required")
        return self


def build_tools(context: PrefixContext) -> list[BaseTool]:
    """Build one proposal-only tool whose target ID is fixed by the application."""

    prefix_config = context["inputs"].parse_config.prefix_context
    if prefix_config is None or prefix_config.max_tool_calls == 0:
        return []

    def propose_current_chunk_update(
        persons: list[str] | None = None,
        timeline: list[TimelineEvent] | None = None,
    ) -> str:
        payload: dict[str, object] = {"sequence_no": context["sequence_no"]}
        if persons is not None:
            payload["persons"] = persons
        if timeline is not None:
            payload["timeline"] = timeline
        update = ChunkResultUpdate.model_validate(payload)
        candidate = apply_chunk_update(context["content"], update)
        validate_result_time_range(
            candidate,
            start_seconds=context["allowed_start_seconds"],
            end_seconds=context["allowed_end_seconds"],
        )
        return update.model_dump_json(exclude_unset=True)

    return [StructuredTool.from_function(
        func=propose_current_chunk_update,
        name="propose_current_chunk_update",
        description=(
            "Propose persons/timeline changes for the one Chunk bound by the host. "
            "This validates but does not persist the proposal."
        ),
        args_schema=_BoundChunkUpdate,
    )]
