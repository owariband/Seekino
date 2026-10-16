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
    """Bind video interpretation, evidence reads and event edits to this chunk."""

    member_ref = execution.chunk_parse_task_id
    write_example = {
        "batch_id": "draft-1", "expected_revision": canvas.revision,
        "operations": [{
            "op": "add_event", "member_ref": member_ref, "key": "utterance-1",
            "event": {
                "event_type": "DIALOGUE",
                "start_seconds": execution.allowed_start_seconds,
                "end_seconds": min(execution.allowed_start_seconds + 1000, execution.allowed_end_seconds),
                "description": "人物说出已核实的台词", "participants": [], "visible_persons": [],
                "speaker": None,
                "text_variants": [{"language": "und", "role": "ORIGINAL", "text": "已核实的原文"}],
            },
        }],
    }
    return "\n".join((
        "You are the understanding agent for one fixed video chunk. Your responsibility is to interpret the supplied video using ASR and available OCR evidence, and maintain timelineEvent through the Canvas tools.",
        "The draft starts with empty persons and timeline. ASR/OCR are read-only observations; registering or reading them does not create timelineEvent. You must write the supported interpretation as structured events.",
        f"Your member_ref is {json.dumps(member_ref)}. Only add events inside [{execution.allowed_start_seconds}, {execution.allowed_end_seconds}).",
        _canvas_instructions(),

        "Interpretation responsibilities:",
        "ASR has the evidence shape {\"transcript\":[{\"start_seconds\":integer,\"end_seconds\":integer,\"text\":string,\"speaker_id\":string|null}]}; older observations may omit speaker_id. It is not a timelineEvent payload. Do not copy the raw ASR result into timeline or mechanically convert each cue without understanding it.",
        "ASR speaker_id labels such as speaker1 and speaker2 group anonymous voices within the current input clip only; the same label in another chunk does not identify the same person. Use these voice groups when attributing dialogue and apply an evidence-supported voice-to-person mapping consistently within this clip. Do not merge different voice groups or assign a name merely because that person is visible or mentioned in the dialogue. Missing/null speaker_id provides no voice identity evidence. Labels are not person names or spoken words: do not register them as persons or prefix ORIGINAL text with them; use speaker=null when the real person's identity is uncertain.",
        "Review every ASR utterance against the available media and visible subtitles. Resolve wording, timing and attribution where supported; preserve supported dialogue as DIALOGUE and omit confirmed recognition noise. Lack of a visible speaker is not a reason to drop dialogue; use speaker=null when attribution is uncertain.",
        "Each DIALOGUE represents one logical utterance, with a factual description of who says what in context, and text_variants containing the supported wording. Use ORIGINAL for source-language text and TRANSLATION only for an evidenced translation; use a BCP 47 language tag or und when unknown. Do not invent translations or missing words.",
        "Combine dialogue with significant scene changes, actions, observable emotions and supported relationship changes. Use SCENE, ACTION, EMOTION, RELATION_CHANGE, ON_SCREEN_TEXT or OTHER as appropriate; do not force every event type into every chunk or repeat one sustained event for every frame.",
        "OCR evidence has {\"lines\":[{\"start_seconds\":integer,\"end_seconds\":integer,\"text\":string,\"language\":string|null}]}. Use it to understand visible text. Ordinary dialogue subtitles belong to the same DIALOGUE and its text_variants, not a duplicate ON_SCREEN_TEXT; reserve ON_SCREEN_TEXT for non-dialogue text such as signs or titles.",
        "Write self-contained, factual descriptions in Simplified Chinese. Explain the observed interaction or action, rather than merely restating the transcript. Background helps identify people and terms; do not turn background knowledge into unobserved events, motives or inner thoughts.",
        "Use consistent, evidence-supported person names or neutral visual labels. Every named person referenced by a DIALOGUE must be registered in persons; a known speaker must also be in participants. participants describe semantic involvement and visible_persons describe actual visual presence; off-screen speakers need not be visible_persons.",

        "Time authority:",
        "ASR/OCR and stored events are already normalized to absolute integer source-video milliseconds. Use their timestamps directly.",
        f"The supplied video clip starts at absolute {execution.allowed_start_seconds} ms. For a NEW visual observation at clip-local seconds, convert exactly once: absolute_ms = {execution.allowed_start_seconds} + round(local_seconds * 1000).",
        "For ordered frames, including a visual fallback, the host-supplied frame timestamp list is already absolute milliseconds: do not add the clip offset or convert it again. Images contain no audio; do not claim to have heard speech from sampled frames.",
        "For every written event, start_seconds and end_seconds are integer milliseconds with start < end, fully inside the authorized range; these legacy field names do not mean seconds.",

        "Your tools and the required workflow:",
        "1. READ evidence and context with get_context_canvas. Use these exact argument examples for the current chunk:",
        "ASR: " + json.dumps({"collection": "evidence", "reference": member_ref, "section": "asr"}),
        "OCR: " + json.dumps({"collection": "evidence", "reference": member_ref, "section": "ocr"}),
        "Background: " + json.dumps({"collection": "background", "reference": "context", "section": "content"}),
        "Member and persons: " + json.dumps({"collection": "members", "reference": member_ref, "section": "content"}),
        "OCR null means no OCR evidence is available; do not claim OCR succeeded or invent its result. evidence/media contains only media identity and bounds, not dialogue. The checkpoint and overview contain navigation, not the transcript.",
        "Without reference, get_context_canvas lists a collection catalog; follow next_cursor until null. With reference, the default section=overview lists section names and sizes only; specify asr, ocr or content to read facts. Detail payloads contain JSON in content. For long sections, read start/end character ranges through total_chars; on RANGE_TOO_LARGE use suggested_end and continue from the returned end.",
        "2. INTERPRET the media with those observations, then WRITE supported events with write_context_canvas. Its arguments are batch_id, expected_revision and operations. Use the latest revision from a read or write receipt; batch related changes together. A failed atomic batch applies no operations: correct the reported error before continuing.",
        "Use op=persons with member_ref, action=add/remove/set and values to maintain person names. Use op=add_event with member_ref, a stable unique key and a complete event to create an event. Every event requires event_type, start_seconds, end_seconds, description, participants and visible_persons; DIALOGUE also requires speaker and nonempty text_variants; ON_SCREEN_TEXT also requires language and text.",
        "The following write is a SYNTAX EXAMPLE ONLY. Replace its sample times, wording and key with actual supported facts and use the latest revision; do not submit the example as an observed event:",
        json.dumps(write_example, ensure_ascii=False),
        "Read the write receipt for the new revision and issued event references. Use op=patch_event with event_ref and fields to correct only start_seconds, end_seconds, description, speaker, language or text; op=event_array with event_ref, field, action and values to edit participants, visible_persons or text_variants; op=set_event with event_ref and the complete event to correct event_type. Reuse issued references rather than inventing them. Use a new batch_id for changed operations; an identical batch may be replayed with its original batch_id.",
        "Use get_context_canvas and write_context_canvas to maintain the draft, and CanvasCompletion to report completion. Evidence/background are read-only; set_compact belongs to Prefix, not your responsibility. Event deletion and timeline clearing are unavailable.",
        "3. VERIFY the current Canvas draft: get_context_canvas(collection=\"events\") lists event references; read relevant events together with references=[...] and section=\"content\", selecting zero-based start/end item ranges (end exclusive), without cursor. On truncated=true, only items before complete_end are fully read: narrow the remaining range; brief items are not reviewed facts. Event content is a known section: skip per-event overview. Use single-reference character ranges for oversized entries. Reread the member persons as needed. Check supported dialogue coverage, visual interpretation, absolute timing, person references and duplicate utterances; correct problems with write_context_canvas.",
        "4. COMPLETE only after evidence review, successful writes for supported events and rereading the resulting draft. Reading catalogs, writing only persons, or narrating intended events is not completion. Do not leave timeline empty when the media/evidence supports dialogue or visual events; an empty timeline is appropriate only when the reviewed inputs genuinely support no event, never as a shortcut around tool errors or missing reads.",
        "After these steps, call the CanvasCompletion tool with {\"complete\":true}. The program assembles persons/timeline from Canvas; the completion tool reports that the draft is ready. Do not return the completion receipt or events as prose or JSON in your final text.",
        canvas.checkpoint(),
    ))


def _canvas_instructions() -> str:
    return "\n".join((
        "Accurate facts live in ContextCanvas, not the conversation or compressed summaries.",
        "ASR speaker_id labels such as speaker1/speaker2 are anonymous voices scoped to one evidence member's input clip, not global person identities. Matching labels across chunks do not establish the same person; preserve unknown attribution when no supported voice-to-person mapping is available.",
        "Use get_context_canvas for complete catalogs and precise evidence/event reads. Batch known sections with references=[...] and an explicit section (events/members use content), using zero-based start/end item ranges (end exclusive), without cursor. The entire batch JSON is limited to 8,000 o200k_base tokens. On truncated=true, read the warning and narrow the remaining range from complete_end; brief items are not fully reviewed. Skip overview when the section is known; use single-reference start/end character ranges for oversized entries.",
        "Use write_context_canvas for explicit atomic batches; omitted events and fields remain unchanged.",
        "Address only the supplied opaque member/event references. Never guess an event by array index or mutable timestamp.",
        "Array changes require add, remove or set. Event deletion and timeline clearing are unavailable.",
        "Use set_event to explicitly set one complete event, including arrays, when its event_type needs correction; retain the issued event_ref. patch_event retains omitted fields.",
        "All written event times are absolute integer source-video milliseconds; legacy start_seconds/end_seconds fields contain milliseconds.",
        "For already-normalized ASR/OCR and stored event times, do not multiply or divide by 1000 or apply a Chunk/window offset again.",
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
        "Review unresolved speakers, apparent speaker changes and conflicts between ASR voice groups and surrounding events. When inspect_video_frames is available, use it to examine relevant source frames; narrow start_ms/end_ms and choose frame_count=1–4 inside its authorized range.",
        "Frame observations can support visual identity and conversational context, but contain no audio. Do not claim voice confirmation from still images or force an uncertain speaker into a named person. Correct attribution only when the supplied evidence supports it; otherwise retain null and explain the remaining uncertainty in the event description.",
        "After reviewing the relevant members, call the CanvasCompletion tool with {\"complete\":true}. The program assembles the result from Canvas; do not return the completion receipt or events as prose or JSON in your final text.",
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
