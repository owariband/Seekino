"""Parse tool contracts and stage collections; execution stays with the host."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agent.utils.context_canvas import ContextCanvas
from agent.utils.context_canvas_tools import build_context_canvas_tools
from agent.utils.tool import AgentTool, build_agent_tool
from agent.utils.tool_result import JsonToolResult

from .timelineEvent_canvas import FinishVoiceprintReview, ParseCanvasWrite, ParseContextCanvas


class _Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True, allow_inf_nan=False)


class GetSpeakerListArguments(_Arguments):
    pass


class CompareSpeakerArguments(_Arguments):
    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_bounds(self):
        if self.end_ms <= self.start_ms:
            raise ValueError("end_ms must exceed start_ms")
        return self


class RegisterVoiceprintArguments(CompareSpeakerArguments):
    source_ref: str = Field(min_length=1)
    speaker: str = Field(min_length=1)
    selection_basis: str = Field(min_length=1)
    confidence: float | None = Field(default=None, ge=0, le=1)
    replace_source_ref: str | None = Field(default=None, min_length=1)


class UnregisterVoiceprintArguments(_Arguments):
    source_ref: str = Field(min_length=1)


class FinishVoiceprintArguments(_Arguments):
    complete: Literal[True]
    skip_reasons: dict[str, str] = Field(
        default_factory=dict,
        description="Explain every speaker below target_refs_count, including zero-reference speakers.",
    )


class AnalyzeSpeakerConsistencyArguments(CompareSpeakerArguments):
    """Analyze source audio in an absolute integer millisecond range."""


class TranscribeSegmentArguments(CompareSpeakerArguments):
    """Transcribe source media in an absolute integer millisecond range."""


def _build_canvas_tools(
    canvas: ParseContextCanvas, *, check_control: Callable[[], None] | None = None,
    on_call: Callable[[str], None] | None = None,
) -> list[AgentTool]:
    return build_context_canvas_tools(
        canvas, role=canvas.stage,
        read_collections=("members", "events", "evidence", "background", "compact"),
        write_args_schema=ParseCanvasWrite, maximum_result_bytes=32_000,
        maximum_read_chars=12_000, maximum_catalog_items=100,
        check_control=check_control, on_call=on_call,
    )


def build_understanding_tools(
    canvas: ParseContextCanvas, *,
    analyze_speaker_consistency: Callable[..., dict] | None = None,
    transcribe_segment: Callable[..., dict] | None = None,
    check_control: Callable[[], None] | None = None,
) -> list[AgentTool]:
    tools = _build_canvas_tools(canvas, check_control=check_control)

    def execute(callback: Callable[..., dict], **values: Any) -> JsonToolResult:
        if check_control is not None:
            check_control()
        payload = callback(**values)
        if check_control is not None:
            check_control()
        return JsonToolResult(success=True, payload=payload)

    if analyze_speaker_consistency is not None:
        tools.append(build_agent_tool(
            name="analyze_speaker_consistency", args_schema=AnalyzeSpeakerConsistencyArguments,
            description=(
                "Inspect acoustic consistency in an authorized source-video range, using absolute integer "
                "start_ms/end_ms. Returns change-review ranges, support limitations and an evidence reference. "
                "A candidate or peak is not an exact speaker boundary, identity or confidence. No candidate "
                "does not confirm one speaker. You may analyze any authorized range without a prior candidate. "
                "Use the evidence with the supplied media; it does not restrict Canvas timing edits."
            ),
            func=lambda **values: execute(analyze_speaker_consistency, **values),
            maximum_result_bytes=32_000,
        ))
    if transcribe_segment is not None:
        tools.append(build_agent_tool(
            name="transcribe_segment", args_schema=TranscribeSegmentArguments,
            description=(
                "Transcribe an authorized source-video subrange again, using absolute integer start_ms/end_ms. "
                "Returns new read-only ASR evidence and its source reference; original ASR and Canvas events "
                "stay unchanged. No prior acoustic candidate is required. Review the returned boundaries and "
                "wording before editing Canvas: successful transcription does not prove single-speaker audio."
            ),
            func=lambda **values: execute(transcribe_segment, **values), maximum_result_bytes=32_000,
        ))
    return tools


def build_refine_tools(
    canvas: ParseContextCanvas, *, compare_speaker: Callable[..., dict] | None = None,
    finish_voiceprint_review: Callable[..., JsonToolResult] | None = None,
    inspect_video_frames: AgentTool | None = None,
    check_control: Callable[[], None] | None = None,
) -> list[AgentTool]:
    tools = _build_canvas_tools(canvas, check_control=check_control)
    if finish_voiceprint_review is not None:
        tools.append(build_agent_tool(
            name="finish_voiceprint_review", args_schema=FinishVoiceprintReview,
            description=(
                "Check the current Canvas voice decisions before CanvasCompletion. "
                "Resolve every returned missing or inconsistent event_ref, then retry this gate."
            ),
            func=finish_voiceprint_review, maximum_result_bytes=32_000,
        ))
    if compare_speaker is not None:
        tools.append(build_compare_speaker_tool(compare_speaker=compare_speaker, check_control=check_control))
    if inspect_video_frames is not None:
        tools.append(inspect_video_frames)
    return tools


def build_prefix_tools(
    canvas: ParseContextCanvas, *, check_control: Callable[[], None] | None = None,
    on_call: Callable[[str], None] | None = None,
) -> list[AgentTool]:
    return _build_canvas_tools(canvas, check_control=check_control, on_call=on_call)


def build_voiceprint_tools(
    canvas: ContextCanvas, *, get_speaker_list: Callable[..., dict],
    register_voiceprint: Callable[..., dict], unregister_voiceprint: Callable[..., dict],
    finish_voiceprint_preparation: Callable[..., dict],
    check_control: Callable[[], None] | None = None,
    on_call: Callable[[str], None] | None = None,
) -> list[AgentTool]:
    tools = build_context_canvas_tools(
        canvas, role="voiceprint", read_collections=("sources", "members", "evidence", "background"),
        maximum_result_bytes=32_000, maximum_read_chars=12_000, maximum_catalog_items=100,
        check_control=check_control, on_call=on_call,
    )

    def execute(name: str, callback: Callable[..., dict], **values: Any) -> JsonToolResult:
        if check_control is not None:
            check_control()
        if on_call is not None:
            on_call(name)
        payload = callback(**values)
        if check_control is not None:
            check_control()
        return JsonToolResult(success=True, payload=payload)

    tools.extend([
        build_agent_tool(
            name="get_speaker_list", args_schema=GetSpeakerListArguments,
            description=(
                "Read the fixed speaker list, source chunks and coverage requirement. Each speaker includes "
                "refs_count, target_refs_count and registered source_ref/start_ms/end_ms ranges. No vectors."
            ),
            func=lambda: execute("get_speaker_list", get_speaker_list), maximum_result_bytes=32_000,
        ),
        build_agent_tool(
            name="register_voiceprint", args_schema=RegisterVoiceprintArguments,
            description=(
                "Register an evidenced named speaker using an issued source_ref and a precise absolute millisecond "
                "subrange inside it. The host extracts original audio and computes the embedding. "
                "Repeat for independent utterances of the same speaker. target_refs_count is a collection goal, "
                "not a registration limit; additional reliable independent references are allowed. Exact repeats "
                "are idempotent; reused sources and overlap with any registered speaker are declined with "
                "message/retry_hint. Returns added, progress and reference metadata without vectors. "
                "Optional replace_source_ref atomically replaces one unpublished reference for the same speaker. "
                "It may use another source or refine the old source's authorized subrange without adding a case. "
                "The old reference remains if extraction or persistence fails. "
                "Provide selection_basis; confidence evaluates attribution, not cosine similarity."
            ),
            func=lambda **values: execute("register_voiceprint", register_voiceprint, **values),
            maximum_result_bytes=32_000,
        ),
        build_agent_tool(
            name="unregister_voiceprint", args_schema=UnregisterVoiceprintArguments,
            description=(
                "Remove one registered source_ref during preparation when it is unsuitable and no reliable "
                "replacement is available. Repeated removal is idempotent. Returns removed and updated counts. "
                "Only unpublished references can be removed; ready references are static."
            ),
            func=lambda **values: execute("unregister_voiceprint", unregister_voiceprint, **values),
            maximum_result_bytes=32_000,
        ),
        build_agent_tool(
            name="finish_voiceprint_preparation", args_schema=FinishVoiceprintArguments,
            description=(
                "Request the host to validate references and persist ready=true. Coverage must exceed half "
                "the fixed speaker list; an empty list permits zero references. Supply skip_reasons for every "
                "speaker below target_refs_count, including zero references. Under-target speakers with reasons "
                "may finish when majority coverage is met. Missing coverage or reasons leaves preparation unready "
                "and returns progress plus a message. The host also checks within-speaker acoustic consistency. "
                "Suspect references leave ready=false without deleting them; replace or unregister them, then retry. "
                "skip_reasons cannot bypass suspects. Finish before exhausting the Tool budget."
            ),
            func=lambda **values: execute("finish_voiceprint_preparation", finish_voiceprint_preparation, **values),
            maximum_result_bytes=32_000,
        ),
    ])
    return tools


def build_compare_speaker_tool(
    *, compare_speaker: Callable[..., dict], check_control: Callable[[], None] | None = None,
) -> AgentTool:
    def compare(start_ms: int, end_ms: int) -> JsonToolResult:
        if check_control is not None:
            check_control()
        payload = compare_speaker(start_ms=start_ms, end_ms=end_ms)
        if check_control is not None:
            check_control()
        return JsonToolResult(success=True, payload=payload)

    return build_agent_tool(
        name="compare_speaker", args_schema=CompareSpeakerArguments, func=compare,
        description=(
            "Compare the original audio in this authorized absolute millisecond range with current-video "
            "voice references. Use for unknown or conflicting named speakers. Returns candidates, cosine "
            "similarities, margins, provenance and duration/quality; similarity is not a probability. "
            "Use Canvas to apply an evidenced attribution, or retain null when evidence is insufficient."
        ), maximum_result_bytes=32_000,
    )
