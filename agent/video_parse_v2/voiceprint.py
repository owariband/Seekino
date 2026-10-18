"""Voice reference preparation and its host-bound Tool contract."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal, NotRequired, TypedDict

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agent.utils.context_canvas import ContextCanvas
from agent.utils.context_canvas_tools import build_context_canvas_tools
from agent.utils.tool import AgentTool, build_agent_tool
from agent.utils.tool_result import JsonToolResult
from engine.parsev2.schemas import VideoParseResult

from .input import ParseInput
from .prompts import build_voiceprint_prompt
from .state import LeaseToken
from .timelineEvent_canvas import CanvasCompletion


class VoiceprintInput(TypedDict):
    """A fixed preparation source set, authorized by the existing Video execution."""

    video_parse_task_id: str
    inputs: ParseInput
    member_sequence_nos: dict[str, int]
    member_contents: dict[str, VideoParseResult]
    member_versions: dict[str, int]
    sources: dict[str, dict[str, Any]]
    member_evidence: NotRequired[dict[str, Any]]
    background: NotRequired[dict[str, Any]]


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


class VoiceprintAgent:
    """Select references; only host Tool commits can make the preparation ready."""

    def __init__(self, *, runtime) -> None:
        self._runtime = runtime

    async def InvokeVoiceprint(self, video_parse_task_id: str, lease: LeaseToken) -> None:
        snapshot = await self._runtime.load_voiceprint_input(video_parse_task_id, lease)
        if snapshot is None:
            return
        config = snapshot["inputs"].parse_config.voiceprint
        if config is None:
            return
        canvas = ContextCanvas(scope="voiceprint:" + video_parse_task_id)
        collections = {
            "sources": snapshot["sources"],
            "members": {
                ref: {"sequence_no": sequence, "source_version": snapshot["member_versions"][ref],
                      "content": snapshot["member_contents"][ref].model_dump(mode="json")}
                for ref, sequence in snapshot["member_sequence_nos"].items()
            },
            "evidence": snapshot.get("member_evidence", {}),
            "background": {"context": snapshot.get("background", {})},
        }
        canvas.mutate(lambda draft: draft.update(collections), readonly_collections=collections)
        receipt = await self._runtime.generate_voiceprint_preparation(
            snapshot, canvas=canvas,
            prompt=build_voiceprint_prompt(max_tool_calls=config.max_tool_calls, canvas=canvas),
        )
        CanvasCompletion.model_validate(receipt)
        await self._runtime.ensure_voiceprint_prepared(snapshot)
        await self._runtime.notify_voiceprint_prepared(snapshot)


def build_tools(
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
