"""Prepare voice references through host-bound capabilities."""

from __future__ import annotations

from typing import Any, NotRequired, TypedDict

from agent.utils.context_canvas import ContextCanvas
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
