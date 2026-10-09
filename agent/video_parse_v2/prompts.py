"""Deterministic prompts for Video Parse V2 model stages."""

from __future__ import annotations

import json
from collections.abc import Sequence

from agent.common.chunk_understanding import (
    build_understanding_prompt as build_chunk_understanding_prompt,
)
from engine.parsev2.observations import ASRResult, OCRResult

from .state import ChunkExecution
from .timelineEvent_canvas import ParseContextCanvas



def build_ocr_prompt(frame_timestamps: Sequence[int]) -> str:
    return (
        "Read only visible non-dialogue on-screen text from the ordered frames. "
        "Use absolute integer source-video milliseconds inside the authorized range. "
        f"Frame timestamps: {list(frame_timestamps)}."
    )


def build_understanding_prompt(
    execution: ChunkExecution,
    *,
    asr_result: ASRResult,
    ocr_result: OCRResult | None,
) -> str:
    """Describe trusted media bounds while leaving the formal Schema to Engine."""

    return build_chunk_understanding_prompt(
        start_ms=execution.allowed_start_seconds,
        end_ms=execution.allowed_end_seconds,
        asr_result=asr_result,
        ocr_result=ocr_result,
    )


def build_canvas_understanding_prompt(execution: ChunkExecution, *, canvas: ParseContextCanvas) -> str:
    return "\n".join((
        "Analyze the prepared media for one fixed video chunk.",
        f"Only add events inside [{execution.allowed_start_seconds}, {execution.allowed_end_seconds}).",
        _canvas_instructions(),
        "Read the original ASR/OCR and background with get_context_canvas. ASR/OCR are already normalized to absolute milliseconds.",
        "Use write_context_canvas to add persons and events to the empty draft; batch related operations together.",
        "Preserve uncertain dialogue attribution as null. Background helps understanding; non-dialogue participants need not all be registered persons.",
        "Return only {\"complete\":true} after your Canvas draft is complete.",
        canvas.checkpoint(),
    ))


def _canvas_instructions() -> str:
    return "\n".join((
        "Accurate facts live in ContextCanvas, not the conversation or compressed summaries.",
        "Use get_context_canvas for complete catalogs and precise evidence/event reads.",
        "Use write_context_canvas for explicit atomic batches; omitted events and fields remain unchanged.",
        "Address only the supplied opaque member/event references. Never guess an event by array index or mutable timestamp.",
        "Array changes require add, remove or set. Event deletion and timeline clearing are unavailable.",
        "Use set_event to explicitly set one complete event, including arrays, when its event_type needs correction; retain the issued event_ref. patch_event retains omitted fields.",
        "All times are absolute integer source-video milliseconds; legacy start_seconds/end_seconds fields contain milliseconds.",
        "Do not multiply or divide by 1000 or apply a Chunk/window offset again.",
        "The final timeline is assembled by the program from Canvas. Final response text cannot replace it.",
    ))


def build_refine_prompt(
    *,
    start_sequence_no: int,
    chunk_count: int,
    canvas: ParseContextCanvas,
) -> str:
    """Expose member content and scope while keeping write authority explicit."""

    end_sequence_no = start_sequence_no + chunk_count
    return "\n".join((
        f"Refine the fixed Interval [{start_sequence_no}, {end_sequence_no}).",
        _canvas_instructions(),
        "Read member scopes, current events and original ASR/OCR separately through get_context_canvas.",
        "Correct existing events or add supported omissions with write_context_canvas. Preserve unrelated events and existing time bounds unless evidence establishes a timing correction.",
        "Resolve names, references and cross-boundary events only from supplied evidence. Missing original observations are unavailable evidence, not permission to invent them.",
        "Return only {\"complete\":true} after reviewing the relevant members.",
        canvas.checkpoint(),
    ))


def build_prefix_prompt(
    *,
    sequence_no: int,
    scope: list[int],
    canvas: ParseContextCanvas,
    max_tool_calls: int,
) -> str:
    """Bind the optional background Agent to one current target."""

    return "\n".join((
        f"Maintain prefix context for the bound Chunk {sequence_no}.",
        f"The target has already consulted ordered scope {scope}.",
        _canvas_instructions(),
        f"This Prefix execution permits at most {max_tool_calls} model Tool calls. Related edits belong in one write batch.",
        "Exact bound facts and original evidence are supplied below. Use get_context_canvas to reread them when the Tool budget permits.",
        "With one Tool call, write the entire validated batch first and then return the completion receipt; an unnecessary read would consume the sole Tool call.",
        "Use set_compact to maintain both summaries within their configured budgets, and explicit event operations for supported corrections.",
        "Current bound members: " + json.dumps(dict(canvas.items("members")), ensure_ascii=False),
        "Current bound events with issued references: " + json.dumps([
            {"reference": reference, "member_ref": value["member_ref"], "event": value["event"]}
            for reference, value in canvas.items("events")
        ], ensure_ascii=False),
        "Original evidence: " + json.dumps(dict(canvas.items("evidence")), ensure_ascii=False),
        "Background: " + json.dumps(dict(canvas.items("background")), ensure_ascii=False),
        "Current compact summaries: " + json.dumps(canvas.get("compact", "prefix"), ensure_ascii=False),
        "Return only {\"complete\":true}; compact summaries and event results come from Canvas.",
        canvas.checkpoint(),
    ))
