from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from agent.utils.context_canvas_tools import build_context_canvas_tools
from agent.utils.tool import AgentTool
from ...common.state import ExecutionControl
from ..video_search_canvas import VideoSearchContextCanvas
from .shared import DraftReadAudit, _ToolCallBudget, _start_tool_call


_Reference = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=36)]
_Claim = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2_000)]


class DraftUpsert(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation: Literal["upsert_draft_item"]
    video_chunk_id: _Reference
    start_seconds: int | None = Field(default=None, strict=True)
    end_seconds: int | None = Field(default=None, strict=True)
    claim: _Claim | None = None


class DraftRemove(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation: Literal["remove_draft_item"]
    video_chunk_id: _Reference


class VideoUpsert(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation: Literal["upsert_video_item"]
    video_chunk_id: _Reference
    claim: _Claim | None = None


class VideoRemove(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation: Literal["remove_video_item"]
    video_chunk_id: _Reference


_DraftOperation = Annotated[DraftUpsert | DraftRemove, Field(discriminator="operation")]
_VideoOperation = Annotated[VideoUpsert | VideoRemove, Field(discriminator="operation")]
_Operation = Annotated[DraftUpsert | DraftRemove | VideoUpsert | VideoRemove,
                       Field(discriminator="operation")]


class WriteCanvasArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    batch_id: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]
    expected_revision: int = Field(strict=True, ge=0)
    operations: list[_Operation] = Field(min_length=1, max_length=100)


class QueryWriteCanvasArguments(WriteCanvasArguments):
    operations: list[_DraftOperation] = Field(min_length=1, max_length=100)


class ChatWriteCanvasArguments(WriteCanvasArguments):
    operations: list[_VideoOperation] = Field(min_length=1, max_length=100)


def build_video_search_canvas_tools(
    *, canvas: VideoSearchContextCanvas, role: Literal["query", "evaluator", "chat", "supervisor"],
    control: ExecutionControl, maximum_result_bytes: int, maximum_catalog_items: int,
    maximum_read_chars: int, budget: _ToolCallBudget | None = None,
    audit: DraftReadAudit | None = None,
) -> list[AgentTool]:
    return build_context_canvas_tools(
        canvas, role=role,
        read_collections=canvas.read_collections[role],
        write_args_schema={"query": QueryWriteCanvasArguments,
                           "chat": ChatWriteCanvasArguments}.get(role),
        maximum_result_bytes=maximum_result_bytes,
        maximum_read_chars=maximum_read_chars,
        maximum_catalog_items=maximum_catalog_items,
        check_control=control.check,
        on_call=lambda name: _start_tool_call(control, budget, name),
        on_read=audit.record if audit is not None else None,
    )
