from __future__ import annotations

import base64
from collections.abc import Callable
from copy import deepcopy
from hashlib import sha256
import re
from typing import Any
from unicodedata import normalize

from pydantic import ValidationError

from agent.utils.context_canvas_tools import build_context_canvas_tools
from agent.utils.errors import AgentToolFeedbackError
from agent.utils.tool import AgentTool, build_agent_tool
from agent.utils.tool_output import bounded_json
from agent.utils.tool_result import JsonToolResult, ToolResult
from gateway.media import ToolImage, ToolImageArtifact

from .state import SummaryCanvas, VideoSummarizeInput, VideoSummarizeLimits, VideoSummaryResult, VideoSummarySubmission


def _source_strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from _source_strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _source_strings(child)


def _episode_numbers(quote: str) -> set[int]:
    quote = normalize("NFKC", quote)
    numbers = set()
    for token in re.findall(r"第\s*([0-9〇零一二三四五六七八九十百千两]+)\s*[集話话回]", quote):
        if token.isascii() and token.isdigit():
            numbers.add(int(token))
            continue
        digits = {character: value for value, character in enumerate("零一二三四五六七八九")}
        digits.update({"〇": 0, "两": 2})
        total, current = 0, 0
        for character in token:
            if character in "十百千":
                total += (current or 1) * {"十": 10, "百": 100, "千": 1000}[character]
                current = 0
            elif character in digits:
                current = current * 10 + digits[character]
            else:
                break
        else:
            numbers.add(total + current)
    numbers.update(int(value) for value in re.findall(r"\bEP(?:ISODE)?\.?\s*([0-9]+)\b", quote, re.IGNORECASE))
    numbers.update(int(value) for value in re.findall(r"#\s*([0-9]+)\b(?!\s*[.,/–—-]\s*\d)", quote))
    return numbers


def _is_preview(text: str) -> bool:
    return re.search(
        r"次回予告|下集[预預]告|下期[预預]告|next\s+episode\s+preview",
        normalize("NFKC", text), re.IGNORECASE,
    ) is not None


def _frame_image(canvas: SummaryCanvas, inspection_id: str, frame_index: int) -> ToolImage:
    inspection = canvas.inspections.get(inspection_id)
    if inspection is None or not 0 <= frame_index < len(inspection[1].images):
        raise AgentToolFeedbackError(
            "UNKNOWN_FRAME", "Frame evidence must reference an image returned by a successful inspection.",
            "Use an issued inspection_id and its zero-based frame_index, or omit the unsupported field.",
        )
    return inspection[1].images[frame_index]


def build_tools(
    canvas: SummaryCanvas, *, inputs: VideoSummarizeInput, inspect_video_frames: AgentTool,
    limits: VideoSummarizeLimits, check_control: Callable[[], None],
) -> list[AgentTool]:
    tools = build_context_canvas_tools(
        canvas, role="summarize", read_collections=("chunks", "background"),
        maximum_result_bytes=limits.maximum_result_bytes,
        maximum_read_chars=limits.maximum_read_chars,
        maximum_catalog_items=limits.maximum_catalog_items,
        check_control=check_control, on_read=canvas.record_read,
    )

    def inspect_frames(**values: Any) -> ToolResult:
        check_control()
        result = inspect_video_frames.execute(**values)
        check_control()
        if not result.success:
            return result
        payload = result.payload if isinstance(result, JsonToolResult) else None
        artifact = result.artifact
        if (not isinstance(payload, dict) or not isinstance(artifact, ToolImageArtifact)
                or not artifact.images or payload.get("images_supplied") is not True
                or not isinstance(payload.get("inspection_id"), str) or not payload["inspection_id"].strip()
                or not isinstance(payload.get("frames"), list)
                or len(payload["frames"]) != len(artifact.images)
                or payload.get("frame_count") != len(artifact.images)
                or payload.get("video_id", inputs.video_id) != inputs.video_id):
            raise RuntimeError("Frame inspection did not return a valid bound image receipt")
        # Retain the exact images delivered by the injected host tool, never model URLs.
        for frame, image in zip(payload["frames"], artifact.images, strict=True):
            timestamp = frame.get("requested_timestamp_ms") if isinstance(frame, dict) else None
            if type(timestamp) is not int or not 0 <= timestamp < inputs.duration_ms:
                raise RuntimeError("Frame receipt position is outside the summarized video")
            header, encoded = image.data_url.split(",", 1)
            if not header.startswith("data:image/") or not header.endswith(";base64"):
                raise RuntimeError("Frame artifact is not an encoded source image")
            data = base64.b64decode(encoded, validate=True)
            if (frame.get("sha256") != sha256(data).hexdigest()
                    or frame.get("size_bytes") != len(data) or image.size_bytes != len(data)):
                raise RuntimeError("Frame receipt does not match the supplied image bytes")
        bounded_json(payload, limits.maximum_result_bytes)
        with canvas._lock:
            inspection_id = payload["inspection_id"]
            record = (deepcopy(payload), artifact)
            if inspection_id in canvas.inspections and canvas.inspections[inspection_id] != record:
                raise RuntimeError("Frame inspection reused an existing receipt identity")
            canvas.inspections[inspection_id] = record
        return result

    tools.append(build_agent_tool(
        func=inspect_frames, name="inspect_video_frames", description=inspect_video_frames.description,
        args_schema=inspect_video_frames.args_schema, maximum_result_bytes=limits.maximum_result_bytes,
    ))

    def submit_video_summary(**values: Any) -> JsonToolResult:
        check_control()
        try:
            submission = VideoSummarySubmission.model_validate(values)
        except ValidationError as error:
            raise AgentToolFeedbackError(
                "INVALID_SUMMARY", "Summary fields or evidence references are invalid.",
                "Follow the submit_video_summary schema; omit unsupported optional fields.",
            ) from error
        bounded_json(submission.model_dump(mode="json"), limits.maximum_result_bytes)
        with canvas._lock:
            missing = canvas.unread_chunks()
            if missing:
                raise AgentToolFeedbackError(
                    "INCOMPLETE_REVIEW", f"Complete content has not been read for {len(missing)} chunks.",
                    f"Unread chunk references (first {min(len(missing), 10)}): {missing[:10]}. "
                    "Read their complete content using references in a batch. Retry remaining_references in smaller batches. "
                    "Only oversized single sections need reference with char_start/char_end; "
                    "only returned content ranges count as reviewed; catalogs do not.",
                )
            if canvas.result is not None:
                if canvas.result.submission == submission:
                    return JsonToolResult(success=True, payload={"submitted": True, "replayed": True})
                raise AgentToolFeedbackError(
                    "ALREADY_SUBMITTED", "A validated summary has already been submitted.",
                    "Finish this execution without replacing the accepted result.",
                )
            supported = set()
            for evidence in submission.evidence:
                if evidence.chunk_parse_task_id is not None:
                    content = canvas.get("chunks", evidence.chunk_parse_task_id)["content"]
                    if evidence.field in {"title", "episode_order"}:
                        if evidence.field == "title" and submission.title_kind == "generated":
                            events = [event for event in content.get("timeline", ())
                                      if isinstance(event, dict)
                                      and any(evidence.quote in text for text in _source_strings(event))]
                            texts = _source_strings(content)
                        else:
                            events = [event for event in content.get("timeline", ())
                                      if isinstance(event, dict) and event.get("event_type") == "ON_SCREEN_TEXT"
                                      and evidence.quote in event.get("text", "")]
                            texts = [event["text"] for event in events]
                        # A substring must not conceal the source event's preview context.
                        if _is_preview(evidence.quote) or any(
                            _is_preview(" ".join(_source_strings(event))) for event in events
                        ):
                            raise AgentToolFeedbackError(
                                "PREVIEW_EVIDENCE", "The cited event identifies a next-episode preview.",
                                "Review this episode's temporal context and cite its own title card; "
                                "never trim preview markers from a quote to claim the next episode's identity.",
                            )
                    else:
                        texts = _source_strings(content)
                    if evidence.field == "cover" or not any(evidence.quote in text for text in texts):
                        raise AgentToolFeedbackError(
                            "UNSUPPORTED_EVIDENCE", "Chunk citation does not contain the claimed source text.",
                            "Quote exact ON_SCREEN_TEXT for official title/episode_order, exact content for generated title/description, "
                            "and a successfully inspected frame for cover.",
                        )
                else:
                    _frame_image(canvas, evidence.inspection_id, evidence.frame_index)
                    if _is_preview(evidence.quote):
                        raise AgentToolFeedbackError(
                            "PREVIEW_EVIDENCE", "The inspected-frame citation identifies a next-episode preview.",
                            "Use evidence belonging to the current episode, or omit the unsupported field.",
                        )
                if evidence.field == "title" and (submission.title is None or (
                    submission.title_kind == "official" and submission.title not in evidence.quote
                )):
                    raise AgentToolFeedbackError(
                        "UNSUPPORTED_TITLE", "Title does not match its quoted title-card text.",
                        "Use the explicit source title, or leave title null when uncertain.",
                    )
                if evidence.field == "episode_order" and _episode_numbers(evidence.quote) != {submission.episode_order}:
                    raise AgentToolFeedbackError(
                        "UNSUPPORTED_EPISODE", "Episode number does not match an unambiguous episode marker in its quote.",
                        "Quote the explicit 第N集/話/回, Episode/EP N or #N marker; omit episode_order if uncertain.",
                    )
                if evidence.field == "cover" and (
                    submission.cover is None or evidence.inspection_id != submission.cover.inspection_id
                    or evidence.frame_index != submission.cover.frame_index
                ):
                    raise AgentToolFeedbackError(
                        "UNSUPPORTED_COVER", "Cover evidence does not reference the selected inspected image.",
                        "Use matching inspection_id/frame_index in cover and its evidence.",
                    )
                supported.add(evidence.field)
            required = {"description"} | {
                field for field in ("title", "episode_order", "cover") if getattr(submission, field) is not None
            }
            if not required <= supported:
                raise AgentToolFeedbackError(
                    "MISSING_EVIDENCE", "Every submitted field needs its own valid source evidence.",
                    "Add grounded evidence or omit unsupported optional fields.",
                )
            image = (_frame_image(canvas, submission.cover.inspection_id, submission.cover.frame_index)
                     if submission.cover is not None else None)
            cited_inspections = {item.inspection_id for item in submission.evidence if item.inspection_id is not None}
            check_control()
            canvas.result = VideoSummaryResult(
                submission=submission.model_copy(deep=True), cover_image=image,
                frame_receipts=tuple(deepcopy(canvas.inspections[key][0]) for key in sorted(cited_inspections)),
            )
        return JsonToolResult(success=True, payload={"submitted": True})

    tools.append(build_agent_tool(
        func=submit_video_summary, name="submit_video_summary",
        description="Submit this video's grounded metadata after reading every chunk. Only a successful receipt completes the task.",
        args_schema=VideoSummarySubmission, maximum_result_bytes=limits.maximum_result_bytes,
    ))
    return tools
