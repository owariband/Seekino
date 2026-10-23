"""Deterministic prompts for Video Parse V2 model stages."""

from __future__ import annotations

import json
from collections.abc import Sequence

from agent.common.chunk_understanding import (
    build_understanding_prompt as build_chunk_understanding_prompt,
)
from engine.parsev2.observations import ASRResult, OCRResult
from agent.utils.context_canvas import ContextCanvas
from server.constants.parse import VOICEPRINT_DECISION_REASON_MAX_CHARS

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
        "Mandatory speaker rule: if the available evidence cannot reliably identify who is speaking, DIALOGUE.speaker MUST be JSON null. Prefer leaving a low-confidence or conflicting attribution unknown to assigning the wrong person. Do not fill a name by guessing from conversational order, a visible or mentioned person, or a plausible plot. Keep intelligible dialogue even when its speaker is unknown; null is an accepted result, not a gap that must be filled.",
        "ASR has the evidence shape {\"transcript\":[{\"start_seconds\":integer,\"end_seconds\":integer,\"text\":string,\"speaker_id\":string|null}]}; older observations may omit speaker_id. It is not a timelineEvent payload. Do not copy the raw ASR result into timeline or mechanically convert each cue without understanding it.",
        "ASR speaker_id labels such as speaker1 and speaker2 group anonymous voices within the current input clip only; the same label in another chunk does not identify the same person. Use these voice groups when attributing dialogue and apply an evidence-supported voice-to-person mapping consistently within this clip. Do not merge different voice groups or assign a name merely because that person is visible or mentioned in the dialogue. Missing/null speaker_id provides no voice identity evidence. Labels are not person names or spoken words: do not register them as persons or prefix ORIGINAL text with them; use speaker=null when the real person's identity is uncertain.",
        "Review every ASR utterance against the available media and visible subtitles. Resolve wording, timing and attribution where supported; split merged utterances, adjust dialogue bounds and omit confirmed recognition noise. Original ASR cues remain read-only source observations, rather than mandatory event intervals. Lack of a visible speaker is not a reason to drop dialogue; use speaker=null when attribution is uncertain.",
        "ASR voice grouping is fallible: one cue may merge different speakers. Separate the utterances where their boundaries are supported, including changes established by context without a tool candidate. Acoustic-change candidates are review hints, not speaker identities, exact boundaries or permission to edit. No candidate does not establish a pure single-speaker interval; uncertain attribution may remain null.",
        "Each DIALOGUE represents one continuous speaking turn by one person. Keep that person's continuous turn together, including ordinary pauses; do not fragment it merely at punctuation, sentence boundaries, semantic transitions or changes of tone. Split where evidence establishes another speaker, an interruption or a separate turn. A single-speaker turn can contain several sentences; atomic means no mixed speakers, not the shortest possible sentence. Keep a factual description of who says what in context and text_variants containing the supported wording. Use ORIGINAL for source-language text and TRANSLATION only for an evidenced translation; use a BCP 47 language tag or und when unknown. Do not invent translations or missing words.",
        "Before combining consecutive ASR cues into one DIALOGUE, call analyze_speaker_consistency on the entire proposed start_ms/end_ms range and read its full evidence_ref. Separate per-cue host checks do not check the joins. Merge only when the media supports the same person's continuous turn and the combined-range analysis supplies enough support at the proposed joins without unresolved change candidates. Neither equal speaker_id labels nor equal assigned names establish this. analysis_status=completed only means computation finished; empty candidates, zero/few windows or insufficient_support alone do not mean a pass or certify one speaker. Read scan_reason_counts, candidates and uncovered from the full evidence; consider where support is missing rather than rejecting a whole long range just because its edges have limited support. When the tool is unavailable or support remains insufficient, preserve supported existing boundaries instead of merging on assumption.",
        "For a block_change or low_support candidate, review the suspicious range with real surrounding context using transcribe_segment and the available media before deciding to retain or split the turn. Keep unaffected same-speaker speech together. Candidate start_ms/end_ms is a review range, peak_ms is not an exact cut, and scan_count is not a requested number of segments. Local ASR speaker groups remain fallible; check the wording and turn boundaries against the media rather than treating a repeated group as proof. After resolving a change, check the complete proposed same-speaker span before combining cues across that area. Do not discard intelligible dialogue when evidence is insufficient; retain supported spans and keep uncertain identity null.",
        "Combine dialogue with significant scene changes, actions, observable emotions and supported relationship changes. Use SCENE, ACTION, EMOTION, RELATION_CHANGE, ON_SCREEN_TEXT or OTHER as appropriate; do not force every event type into every chunk or repeat one sustained event for every frame.",
        "OCR evidence has {\"lines\":[{\"start_seconds\":integer,\"end_seconds\":integer,\"text\":string,\"language\":string|null}]}. Use it to understand visible text. Ordinary dialogue subtitles belong to the same DIALOGUE and its text_variants, not a duplicate ON_SCREEN_TEXT; reserve ON_SCREEN_TEXT for non-dialogue text such as signs or titles.",
        "Write self-contained, factual descriptions in Simplified Chinese. Explain the observed interaction or action, rather than merely restating the transcript. Background helps identify people and terms; do not turn background knowledge into unobserved events, motives or inner thoughts.",
        "Use consistent, evidence-supported person names or neutral visual labels. Every named person referenced by a DIALOGUE must be registered in persons; a known speaker must also be in participants. participants describe semantic involvement and visible_persons describe actual visual presence; off-screen speakers need not be visible_persons.",

        "Time authority:",
        "ASR/OCR and stored events are already normalized to absolute integer source-video milliseconds. Use their timestamps directly.",
        "Understanding may create, split and retime DIALOGUE inside this chunk based on supported media/context, including after creation. Event times need not exactly match an original ASR cue, a local transcription or an acoustic-change candidate. If ASR is unavailable or empty, supported native-video dialogue may be created with observed bounds. Keep original observations unchanged.",
        "The host supplies the actual visual input range and native-video start separately. Extra visual context before or after the authorized chunk is provided only to help interpret this chunk; do not write those outside events or expand the writable range. For NEW native-video observations, convert local seconds exactly once using the host-supplied visual start, not the writable chunk start.",
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
        "When available, analyze_speaker_consistency(start_ms,end_ms) inspects the bound original audio for acoustic-change review candidates using CAM++ windows without VAD. It does not identify people, count speakers or establish overlap; silence, delivery and background sound can also change the vectors. transcribe_segment(start_ms,end_ms) requests a local-range ASR observation for wording and timing review. Its speaker_id labels belong to that transcription call; matching labels across original/local or different local calls do not establish the same voice. Both tools use absolute source-video milliseconds and return immutable evidence references plus the current Canvas revision. Read their evidence for provenance; do not overwrite the original ASR. Follow the combined-range check and candidate-review workflow above. These are evidence-review instructions, not additional Canvas write or completion authorization gates. A missing binding may make a tool unavailable: preserve supported boundaries and continue without inventing tool results.",
        "2. INTERPRET the media with those observations, then WRITE supported events with write_context_canvas. Its arguments are batch_id, expected_revision and operations. Use the latest revision from a read or write receipt; batch related changes together. A failed atomic batch applies no operations: correct the reported error before continuing.",
        "Use op=persons with member_ref, action=add/remove/set and values to maintain person names. Use op=add_event with member_ref, a stable unique key and a complete event to create an event. Every event requires event_type, start_seconds, end_seconds, description, participants and visible_persons; DIALOGUE also requires speaker and nonempty text_variants; ON_SCREEN_TEXT also requires language and text.",
        "The following write is a SYNTAX EXAMPLE ONLY. Replace its sample times, wording and key with actual supported facts and use the latest revision; do not submit the example as an observed event:",
        json.dumps(write_example, ensure_ascii=False),
        "Read the write receipt for the new revision and issued event references. Use op=patch_event with event_ref and fields to correct timing, description, speaker, language or text. To split an existing dialogue, shorten its interval and correct its wording, then add the remaining utterances with distinct stable keys in one atomic batch. Use op=event_array with event_ref, field, action and values to edit participants, visible_persons or text_variants; op=set_event with event_ref and the complete event to correct event_type or other supported facts. Reuse issued references rather than inventing them. Use a new batch_id for changed operations; an identical batch may be replayed with its original batch_id.",
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
    voiceprint = canvas.voiceprint_review()
    voice_instructions = () if voiceprint is None else (
        "Prepared B0 voice evidence is available in each member's evidence/voiceprint section and bound to each event's voice_evidence with the current event_ref. The event summary contains its original Understanding speaker, unchanged candidate ranking, cosine gap, duration and band. Read full reference sources and provenance through get_context_canvas collection=evidence, reference=voice_evidence.evidence_ref, section=voiceprint, matching line_id. Similarity is not a probability or calibrated confidence.",
        "Review every supplied voice default and every line in the explicit conflict and ACCEPT-disagreement lists below. In flag mode the program preserves every speaker. In prefill mode only ACCEPT lines have a voice top1 default in the working draft; leaving it unchanged accepts it. Modify speaker only for supported exceptions; an explicit null remains null. Candidates are evidence, and you may choose a person outside the top three. The program records speaker_source; do not set it yourself.",
        "Conversational context alone does not override a voice disagreement: it needs specific evidence, such as the speaker being addressed by name or visibly speaking. A single frame that does not show someone cannot by itself exclude an off-screen speaker. Record a brief factual correction basis without exposing hidden reasoning.",
        "Voice evidence applies only to its recorded start_ms/end_ms. Editing text or speaker does not invalidate it. A changed audio interval invalidates the old scores and removes an unreviewed voice default: use compare_speaker for the new interval or explicitly select a supported speaker/null. Never reuse an old score as if it described the new interval.",
        "compare_speaker returns a compact ranking and an evidence_ref/evidence_section for full immutable provenance through get_context_canvas. It also returns the current Canvas revision; use that revision for subsequent writes.",
        f"For every event in conflicts, accept_disagreements or doubtful_labels inside this window's write bounds whose voice_evidence is still valid, record a decision through write_context_canvas with op=voice_decision, event_ref, decision=keep|voice_top1|other|null and a nonblank reason of 1-{VOICEPRINT_DECISION_REASON_MAX_CHARS} characters.",
        "The decision must match the final speaker: keep equals a named understanding_speaker; voice_top1 equals candidates[0].speaker; other is a named speaker different from both; null means speaker is null. Use null, not keep, when Understanding had no named speaker and the final speaker is unknown. Edit speaker explicitly when needed, then record the decision. A contradictory later speaker edit invalidates the recorded decision; record a replacement and pass the gate again.",
        "For keep on a label_doubtful line, supply evidence_type=named_address|visible_speaking|text_identity and cite the concrete identity observation in reason. For null on a required line with available candidates, supply evidence_type=contradiction and cite the specific contradiction, very short audio, singing or overlap in reason. contradiction is not identity evidence for keep; evidence_type is optional otherwise.",
        "Earlier windows' committed decisions are information only: resolve every required conflict within this window again. An audio interval change invalidates old voice evidence and removes its decision requirement.",
        "Call finish_voiceprint_review after resolving all required events and before CanvasCompletion. If ready=false, correct every missing or inconsistent event_ref and retry the gate; CanvasCompletion is accepted only after the gate passes.",
        "Each dialogue line may carry label_doubtful. It means the line sounds clearly unlike the labeled character's other lines; it does not prove who did speak. On a doubtful line, keep the label only with specific evidence: the speaker is addressed by name, is visibly speaking, or the text identifies them. ASR speaker groups and turn order are not identity evidence; they are frequently wrong when two characters alternate.",
        "The voice ranking is audio evidence. Candidate cosine similarities, gap and reference counts are descriptive evidence, not calibrated confidence. Gap is not an acceptance threshold: a small, zero or unavailable gap alone does not justify null or restoring the Understanding label. ACCEPT marks prefill eligibility, not certainty of identity. On a CONFLICT or label_doubtful line, choose the voice's top candidate unless specific evidence contradicts it: someone else is addressed by name or visibly speaking, or the candidate is clearly absent from the scene. Choose null only for missing candidates, very short audio, singing, overlap or a specific contradiction. Keep the original label only with specific evidence for it.",
        "Required voice review lists: " + json.dumps(voiceprint, ensure_ascii=False, sort_keys=True),
    )
    return "\n".join((
        f"Refine the fixed Interval [{start_sequence_no}, {end_sequence_no}).",
        _canvas_instructions(),
        "Read member scopes, current events and original ASR/OCR separately through get_context_canvas.",
        "Correct existing events or add supported omissions with write_context_canvas. Preserve unrelated events and existing time bounds unless evidence establishes a timing correction.",
        "Resolve names, references and cross-boundary events only from supplied evidence. Missing original observations are unavailable evidence, not permission to invent them.",
        "Review unresolved speakers, apparent speaker changes and conflicts between ASR voice groups and surrounding events. When inspect_video_frames is available, use it to examine relevant source frames; narrow start_ms/end_ms and choose frame_count=1–4 inside its authorized range.",
        ("Frame observations can support visual identity and conversational context, but contain no audio. Do not claim voice confirmation from still images or force an uncertain speaker into a named person. Correct attribution only when the supplied evidence supports it; otherwise retain null and explain the remaining uncertainty in the event description."
         if voiceprint is None else
         "Frame observations can support visual identity and conversational context, but contain no audio. Do not claim voice confirmation from still images. Apply the voice ranking and null decision rules below, and explain any remaining uncertainty in the event description."),
        ("When compare_speaker is available, call it for unresolved dialogue or a named speaker whose attribution conflicts with other evidence. Use the exact target utterance's start_ms/end_ms; narrow merged speech where evidence establishes the boundary. Compare its candidate similarities, margin, reference identity evidence and source/duration quality. Cosine similarity is not a probability: 0.8 does not mean 80% certainty. Missing references or unreliable audio may leave speaker=null; a small candidate margin alone is not a reason to abstain. Write any supported correction through write_context_canvas."
         if voiceprint is None else
         "When compare_speaker is available, call it for unresolved dialogue or a named speaker whose attribution conflicts with other evidence. Use the exact target utterance's start_ms/end_ms; narrow merged speech where evidence establishes the boundary. Compare its candidate similarities, margin, reference identity evidence and source/duration quality. Cosine similarity is not a probability: 0.8 does not mean 80% certainty. Apply the voice ranking and null decision rules below. Write any supported correction through write_context_canvas."),
        *voice_instructions,
        "After reviewing the relevant members, call the CanvasCompletion tool with {\"complete\":true}. The program assembles the result from Canvas; do not return the completion receipt or events as prose or JSON in your final text.",
        canvas.checkpoint(),
    ))


def build_voiceprint_prompt(*, max_tool_calls: int, canvas: ContextCanvas) -> str:
    return "\n".join((
        "Prepare voice references for the bound video's fixed source snapshot. Do not edit timelineEvent.",
        "First call get_speaker_list for the program-derived names, source chunks, per-speaker refs_count, target_refs_count, registered references and coverage requirement. Read the target from the Tool result; do not invent a collection target.",
        "Use get_context_canvas to read sources, member events, ASR/OCR and background. Collections are read-only. Read catalogs without reference, then a reference's value section; use cursor or start/end ranges for bounded reads.",
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
