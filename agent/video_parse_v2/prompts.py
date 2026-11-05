"""Deterministic prompts for Video Parse V2 model stages."""

from __future__ import annotations

import json
from copy import deepcopy
from collections.abc import Sequence

from agent.common.chunk_understanding import (
    build_understanding_prompt as build_chunk_understanding_prompt,
)
from engine.parsev2.observations import ASRResult, OCRResult
from agent.utils.context_canvas import ContextCanvas

from .state import ChunkExecution
from .timelineEvent_canvas import ParseContextCanvas



def build_ocr_prompt(frame_timestamps: Sequence[int]) -> str:
    return (
        "Extract visible dialogue subtitles and other on-screen text verbatim from every ordered frame. "
        "Each image is preceded by its absolute timestamp label; use that label for the text in that image. "
        "Keep each visible language separately, including Chinese and Japanese subtitles shown together; "
        "set language to its BCP 47 tag (for example zh-Hans, zh-Hant or ja), or null when uncertain. "
        "Preserve the visible wording and punctuation. Do not translate, paraphrase, infer missing words, "
        "identify speakers or describe the scene. Omit unreadable text. "
        "Merge identical text only across consecutive frames where it remains visible; "
        "keep different text, languages and recurrences after a gap separate. "
        "Use absolute integer source-video milliseconds despite the legacy field names: "
        "start_seconds is the first matching frame timestamp; end_seconds is the last consecutive "
        "matching frame timestamp plus 1 millisecond, including text visible in only one frame. "
        "These are observed bounds, not inferred subtitle durations; do not extend them to unseen times. "
        "Return lines in time order, or an empty lines list when no text is readable. "
        f"Frame timestamps: {list(frame_timestamps)}."
    )


def _all_speakers(asr_result: ASRResult, background: dict) -> dict:
    """Keep whole-video voice groups distinct from background-provided people."""
    whole_video = (asr_result.get("speaker_scope") == "video"
                   and bool(asr_result.get("speaker_scope_id"))
                   and "speaker_ids" in asr_result)
    voice_groups = (list(asr_result["speaker_ids"]) if whole_video else sorted({
        cue["speaker_id"] for cue in asr_result["transcript"] if cue.get("speaker_id")
    }))
    document = background.get("important_extra_data") or {}
    people = []
    if document.get("format") == "json" and isinstance(document.get("content"), str):
        try:
            content = json.loads(document["content"])
        except ValueError:
            content = None
        if isinstance(content, dict) and isinstance(content.get("all_speakers"), list):
            people = content["all_speakers"]
    return {
        "scope": "video" if whole_video else "input_clip",
        "scope_id": asr_result["speaker_scope_id"] if whole_video else None,
        "voice_groups": voice_groups,
        "voice_group_count": len(voice_groups) if whole_video or voice_groups else None,
        "known_speakers": people,
    }


def build_understanding_prompt(
    execution: ChunkExecution,
    *,
    asr_result: ASRResult,
    ocr_result: OCRResult | None,
) -> str:
    """Supply observations directly; the host validates and imports the JSON draft."""

    # Keep complete word alignment in persisted evidence for Refine. Initial
    # interpretation needs the complete cue text, times and anonymous voice IDs.
    transcript = {
        **asr_result,
        "transcript": [{key: value for key, value in cue.items() if key != "words"}
                       for cue in asr_result["transcript"]],
    }
    return "\n".join((
        "You are the understanding model for one fixed video chunk. Interpret the supplied media and observations and return one complete JSON object matching VideoParseResult.",
        "Return only the JSON object with persons and timeline, without Markdown fences, commentary or a completion receipt. No tools are available. The host validates and saves the complete result.",
        "Interpretation responsibilities:",
        "Speaker attribution uses all_speakers, the supplied Series background and the media together. An explicit background voice-to-person correspondence is identity evidence. For a background-identified sole lecturer or narrator, a matching single dominant whole-video voice and consistent media support that named attribution; use the supplied name and reuse it across utterances, without requiring new voiceprint verification for each cue. A cast list, a name merely mentioned in speech, or a voice count alone is insufficient. If evidence is missing or conflicting, DIALOGUE.speaker MUST be JSON null; keep intelligible dialogue and continue without repeatedly debating the missing identity.",
        "ASR has the evidence shape {\"transcript\":[{\"start_seconds\":integer,\"end_seconds\":integer,\"text\":string,\"speaker_id\":string|null}]}; older observations may omit speaker_id. It is not a timelineEvent payload. Do not copy the raw ASR result into timeline or mechanically convert each cue without understanding it.",
        "ASR speaker_id labels group anonymous voices. When speaker_scope=video, speaker_scope_id identifies one whole-video grouping and speaker_ids lists its dominant voice groups: the same label within that scope is the same candidate voice across chunks. Otherwise labels are scoped only to the current input clip. Reuse these supplied groups; do not rediscover speaker count from frames. Voice grouping alone does not establish a named or visible person; combine it with the explicit identity context supplied in all_speakers and Series background. Once that context supports a mapping, reuse it rather than treating each cue as a new identity problem. Keep unresolved or conflicting mappings null and continue; do not explain missing identity in event descriptions. Missing/null speaker_id remains unknown. Do not register anonymous IDs as persons, prefix dialogue with them, or infer a person merely from visibility or mention.",
        "Review every ASR utterance against the available media and visible subtitles. Resolve wording, timing and attribution where supported; preserve supported dialogue as DIALOGUE and omit confirmed recognition noise. Lack of a visible speaker is not a reason to drop dialogue; use speaker=null when attribution is uncertain.",
        "ASR voice grouping is fallible: one cue may merge different speakers. Do not attribute an entire cue to its first speaker when a turn change is evident. Separate the utterances where their boundaries are supported, and leave any unresolved speaker null.",
        ("ASR comes from the complete source video. Trust its supplied wording and timestamps by default; preserve the complete cue as DIALOGUE even when it extends beyond this Chunk's pictures. Do not split or clip a cue merely to fit the Chunk or a sentence; Refine will reconcile overlaps."
         if execution.dialogue_bounds is not None else
         "Each DIALOGUE represents one supported utterance within the authorized clip."),
        "DIALOGUE includes a factual description and the supported wording in text_variants. Use ORIGINAL for source-language text and TRANSLATION only for an evidenced translation; use a BCP 47 language tag or und when unknown. Do not invent translations or missing words.",
        "Combine dialogue with significant scene changes, actions, observable emotions and supported relationship changes. Use SCENE, ACTION, EMOTION, RELATION_CHANGE, ON_SCREEN_TEXT or OTHER as appropriate; do not force every event type into every chunk or repeat one sustained event for every frame.",
        "OCR evidence has {\"lines\":[{\"start_seconds\":integer,\"end_seconds\":integer,\"text\":string,\"language\":string|null}]}. Use it to understand visible text. Ordinary dialogue subtitles belong to the same DIALOGUE and its text_variants, not a duplicate ON_SCREEN_TEXT; reserve ON_SCREEN_TEXT for non-dialogue text such as signs or titles.",
        "Write self-contained, factual descriptions in Simplified Chinese. Explain the observed interaction or action, rather than merely restating the transcript. Background helps identify people and terms; do not turn background knowledge into unobserved events, motives or inner thoughts.",
        "Use consistent, evidence-supported person names or neutral visual labels. Every named person referenced by a DIALOGUE must be registered in persons; a known speaker must also be in participants. participants describe semantic involvement and visible_persons describe actual visual presence; off-screen speakers need not be visible_persons.",

        "The host supplies the actual visual input range and native-video start separately. Extra visual context before or after the authorized chunk is provided only to help interpret this chunk; do not write those outside visual events or expand the visual range. For NEW native-video observations, convert local seconds exactly once using the host-supplied visual start, not the writable chunk start.",
        "For ordered frames, including a visual fallback, the host-supplied frame timestamp list is already absolute milliseconds: do not add the clip offset or convert it again. Images contain no audio; do not claim to have heard speech from sampled frames.",
        "For every written event, start_seconds and end_seconds are integer milliseconds with start < end; these legacy field names do not mean seconds. Apply the separate visual and DIALOGUE ranges supplied below.",

        "Order timeline by start_seconds. Check dialogue coverage, visual facts, person references, duplicate utterances and authorized time bounds before returning. An empty timeline is valid only when the supplied inputs support no event.",
        "speaker_source is host-owned provenance; omit it from your output.",
        "OCR null means no separate OCR evidence is available. You may read text directly from the supplied media; do not claim an OCR service ran.",
        "all_speakers: " + json.dumps(
            _all_speakers(asr_result, execution.background),
            ensure_ascii=False, allow_nan=False, sort_keys=True,
        ),
        build_chunk_understanding_prompt(
            start_ms=execution.allowed_start_seconds,
            end_ms=execution.allowed_end_seconds,
            asr_result=transcript,
            ocr_result=ocr_result,
            dialogue_bounds=execution.dialogue_bounds,
        ),
        "Background context: " + json.dumps(
            execution.background, ensure_ascii=False, allow_nan=False, sort_keys=True,
        ),
    ))


def _canvas_instructions() -> str:
    return "\n".join((
        "Accurate facts live in ContextCanvas, not the conversation or compressed summaries.",
        "ASR speaker_id labels are anonymous voices. When evidence explicitly declares speaker_scope=video, matching speaker_scope_id and speaker_id refer to the same candidate voice across members of that scope. Without that declaration, labels remain local to one input clip. Use the supplied Series background as identity evidence: an explicit voice/person correspondence, or a background-identified sole lecturer with a matching single dominant whole-video voice and consistent media, can support a named speaker. Reuse that supported mapping across members; do not remove it merely because ASR uses anonymous IDs or no biometric verification was run. A cast list or voice count alone is insufficient. Missing or conflicting mappings remain null without repeated visual searches or uncertainty explanations in event descriptions.",
        "Use get_context_canvas for complete catalogs and evidence/event reads. Batch with references=[...]; section defaults to content. The Tool first tries all requested items. On truncated=true, returned items are complete: read remaining_references in smaller batches. Skip overview when the section is known. Only oversized single sections need reference with char_start/char_end character offsets. Catalogs are returned in full unless their byte budget is exceeded; only then pass next_cursor as catalog_cursor.",
        "Use write_context_canvas for explicit atomic batches; omitted events and fields remain unchanged.",
        "Address only the supplied opaque member/event references. Never guess an event by array index or mutable timestamp.",
        "Array changes require add, remove or set. Use op=remove_event with event_ref to explicitly remove an unsupported or duplicate event; omitted events remain unchanged.",
        "Use set_event to explicitly set one complete event, including arrays, when its event_type needs correction; retain the issued event_ref. patch_event retains omitted fields.",
        "All written event times are absolute integer source-video milliseconds; legacy start_seconds/end_seconds fields contain milliseconds.",
        "For already-normalized ASR/OCR and stored event times, do not multiply or divide by 1000 or apply a Chunk/window offset again.",
        "The final timeline is assembled by the program from Canvas. Final response text cannot replace it.",
    ))


def build_refine_prompt(*, snapshot, canvas: ParseContextCanvas) -> str:
    """Use the measured complete-JSON task; keep immutable evidence off the edit surface."""
    members = []
    duration = snapshot["inputs"].parse_config.chunk_duration * 1000
    for ref, sequence in sorted(snapshot["member_sequence_nos"].items(), key=lambda item: item[1]):
        evidence = deepcopy(snapshot.get("member_evidence", {}).get(ref, {}))
        for cue in (evidence.get("asr") or {}).get("transcript", []):
            cue.pop("words", None)
        members.append({
            "sequence_no": sequence,
            "nominal_chunk_range_ms": [sequence * duration,
                                      min((sequence + 1) * duration, snapshot["member_bounds"][ref][1])],
            "content": canvas.result(ref).model_dump(mode="json", exclude={"timeline": {"__all__": {"speaker_source"}}}),
            "evidence": evidence,
        })
    dialogue_bounds = snapshot.get("dialogue_bounds")
    time_rule = (
        f"Complete ASR dialogue may extend beyond nominal chunk boundaries; keep it within source-video {list(dialogue_bounds)} ms."
        if dialogue_bounds is not None else
        "Keep dialogue within the supplied member time range; no whole-video ASR authority is available."
    )
    return "\n".join((
        "Refine these adjacent chunks as one continuous excerpt. Return one complete IntervalRefinementResult JSON object, with no tools or commentary.",
        "The ASR comes from the source video. Prefer its dialogue wording and timestamps. Preserve complete dialogue across nominal chunk boundaries; do not truncate or split merely to fit a chunk. Remove repeated dialogue across adjacent chunks and repair genuine omissions or continuity errors using the supplied ASR. Keep each retained dialogue once, in a relevant member. Prefer existing event grouping; do not reconstruct every sentence's word-level timing.",
        "Use the supplied background for supported speaker names. Preserve existing visual facts; no new visual or voice evidence is available. Do not invent visual changes or reinterpret unidentified voices without evidence.",
        "Make only supported corrections. Keep unrelated events and their content unchanged. Maintain persons, participants and visible_persons consistently; a named speaker must be a participant. Omit host-owned speaker_source.",
        "All timestamps are absolute integer source-video milliseconds, including legacy *_seconds fields. Do not multiply or divide by 1000 or apply an offset again. Preserve existing time bounds unless supplied evidence supports a correction.",
        time_rule,
        "Prepared voiceprint evidence, when present, applies only to its recorded audio interval. A supplied prefilled speaker is a draft, not certainty; explicitly retain or correct it using the evidence. Do not reuse a score for changed audio bounds.",
        "Edit the supplied JSON directly. Return every input sequence_no exactly once, each with complete persons and timeline. Omitted events are removed, so preserve every unrelated event. Return only the members array wrapped in IntervalRefinementResult.",
        "Input: " + json.dumps({"background": snapshot.get("background", {}), "members": members},
                              ensure_ascii=False, allow_nan=False, sort_keys=True),
    ))

def build_voiceprint_prompt(*, max_tool_calls: int, canvas: ContextCanvas) -> str:
    return "\n".join((
        "Prepare voice references for the bound video's fixed source snapshot. Do not edit timelineEvent.",
        "First call get_speaker_list for the program-derived names, source chunks, per-speaker refs_count, target_refs_count, registered references and coverage requirement. Read the target from the Tool result; do not invent a collection target.",
        "Use get_context_canvas to read sources, member events, ASR/OCR and background. Collections are read-only. Read catalogs without reference, then batch references for their complete content. Read remaining_references in smaller batches; only oversized single entries need char_start/char_end. Only oversized catalogs require next_cursor as catalog_cursor.",
        "Choose clean single-speaker speech whose character identity is supported by the existing evidence. A non-null speaker or your own high confidence alone is not independent confirmation. Still frames, dialogue wording and nearby names cannot establish matching voice timbre; the first named anchor still requires credible identity evidence.",
        "Call register_voiceprint with an issued source_ref, speaker, precise start_ms/end_ms and selection_basis. Times are absolute source-video integer milliseconds; do not multiply or divide legacy start_seconds/end_seconds values by 1000. Shorten a mixed ASR segment to the evidenced single-speaker subrange. Never supply a local path, temporary URI or invented embedding.",
        "Before registering references, inspect each speaker's available chunk_ids and the members' sequence_no values to plan temporally distributed sampling. Sample early, middle and late portions of that speaker's available source chunks, alternating between separated chunks instead of filling the target from the first chunk in sequence. Prefer one reliable utterance per distinct available chunk before taking additional utterances from a chunk; a speaker may occur in only part of the video, so do not force coverage of regions without reliable speech.",
        "Collect as many useful, reliable independent references as the existing budget permits, aiming for target_refs_count per speaker. The target is a collection goal, not a registration limit; additional references are welcome when they add useful coverage. Prefer varied scenes, wording and speaking states after identity and clean single-speaker audio are established; do not impose emotion quotas or use doubtful samples to fill the target. When too few distinct chunks are available, separate independent utterances in the same chunk are allowed. Keep enough budget to finish preparation.",
        "Check each speaker's registered source_ref and start_ms/end_ms before selecting another reference. Exact repeated registration is idempotent and does not add a reference; another subrange of the same source_ref is not an independent sample. New audio must not overlap any registered reference, including another speaker's. Adjacent half-open intervals may touch. Follow message/retry_hint feedback and choose a different independent, non-overlapping source when registration is declined.",
        "Registration results report added, refs_count, target_refs_count and the selected source range. Use that progress and get_speaker_list when needed; reference vectors are host data and are not returned to you.",
        "finish_voiceprint_preparation reads the current persisted registry and checks each speaker's references against one another. A suspect_references result identifies acoustically inconsistent sources; it is a request to review the reference, not proof of another character's identity. The host leaves these references unchanged and ready=false. Replace a suspect by calling register_voiceprint with replace_source_ref and a reliable authorized reference for the same speaker, or call unregister_voiceprint with that source_ref when no suitable replacement is available. Explicit replacement may select another source or refine the same source's authorized subrange without adding an independent case. Replacement commits only after new audio extraction and embedding succeed. Then retry finish; skip_reasons cannot bypass a remaining suspect. Use the existing finite budget, and explain any shortfall after explicit removal rather than inventing samples. Prepared ready references are static and cannot be replaced or removed.",
        "Different aliases are deliberately not automatically merged in this version. Prefer the exact name returned by get_speaker_list and spend evidence on useful characters rather than uncertain NPCs.",
        "Coverage counts distinct required speakers with valid references, not registration calls or duplicate clips. The registered count must be strictly greater than half the fixed speaker list. An empty speaker list can finish with zero references; otherwise missing evidence cannot bypass the required majority.",
        "Meeting the majority coverage requirement alone is not the collection goal. Continue toward each speaker's reference target within the fixed authorized sources and finite execution budget. When targets are reached, usable evidence is exhausted or the budget is nearly spent, call finish_voiceprint_preparation before exhausting the budget with complete=true and skip_reasons for every speaker below target, including those with zero references. Explain the actual shortfall; do not invent low-quality references or describe model, audio extraction or database failures as a normal shortage.",
        "If finish reports missing coverage or missing_reason_speakers, use reference_counts, target_refs_count and its message to continue or supply the missing reasons within budget. A speaker below target may finish with an explanation when strict majority coverage is met. An empty list needs no reasons. Only a successful host commit makes ready=true; final prose or a completion receipt alone cannot finish preparation.",
        f"This execution permits at most {max_tool_calls} Tool calls. After successful preparation, return only {{\"complete\":true}}.",
        "Canvas navigation: " + json.dumps(canvas.checkpoint(), ensure_ascii=False),
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
