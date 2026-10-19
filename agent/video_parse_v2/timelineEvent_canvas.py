"""Execution-local parse facts, immutable observations and explicit event edits."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from copy import deepcopy
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agent.utils.context_canvas import ContextCanvas
from agent.utils.errors import AgentToolFeedbackError
from engine.parsev2.schemas import DialogueTextVariant, TimelineEvent, VideoParseResult
from engine.parsev2.validation import (
    ChunkResultUpdate, ValidatedIntervalRefinement, ValidatedPrefixRefinement,
    validate_result_time_range,
)
from server.constants.parse import VOICEPRINT_B0_VOICE_TOP1_MIN_LEAD, VOICEPRINT_DECISION_REASON_MAX_CHARS


def content_fingerprint(value: object) -> str:
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode()).hexdigest()


class _Operation(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EventFields(_Operation):
    start_seconds: int | None = Field(default=None, ge=0, strict=True)
    end_seconds: int | None = Field(default=None, gt=0, strict=True)
    description: str | None = Field(default=None, min_length=1, pattern=r"\S")
    speaker: str | None = None
    language: str | None = None
    text: str | None = Field(default=None, min_length=1, pattern=r"\S")

    @model_validator(mode="after")
    def explicit_fields(self):
        if not self.model_fields_set:
            raise ValueError("at least one event field must be supplied")
        if any(getattr(self, field) is None for field in self.model_fields_set - {"speaker", "language"}):
            raise ValueError("only speaker and language may be explicitly null")
        return self


class AddEvent(_Operation):
    op: Literal["add_event"]
    member_ref: str
    key: str = Field(min_length=1, pattern=r"\S")
    event: TimelineEvent


class PatchEvent(_Operation):
    op: Literal["patch_event"]
    event_ref: str
    fields: EventFields


class SetEvent(_Operation):
    op: Literal["set_event"]
    event_ref: str
    event: TimelineEvent


class PersonsOperation(_Operation):
    op: Literal["persons"]
    member_ref: str
    action: Literal["add", "remove", "set"]
    values: list[Annotated[str, Field(min_length=1, pattern=r"\S")]]


class EventArrayOperation(_Operation):
    op: Literal["event_array"]
    event_ref: str
    field: Literal["participants", "visible_persons", "text_variants"]
    action: Literal["add", "remove", "set"]
    values: list[str | DialogueTextVariant]

    @model_validator(mode="after")
    def validate_element_type(self):
        if self.field == "text_variants":
            if any(not isinstance(value, DialogueTextVariant) for value in self.values):
                raise ValueError("text_variants operations require complete language/role/text values")
        elif any(not isinstance(value, str) or not value.strip() for value in self.values):
            raise ValueError("person array operations require nonblank person names")
        return self


class CompactOperation(_Operation):
    op: Literal["set_compact"]
    series_compact: str
    video_compact: str


class VoiceDecision(_Operation):
    op: Literal["voice_decision"]
    event_ref: str
    decision: Literal["keep", "voice_top1", "other", "null"]
    reason: str = Field(min_length=1, max_length=VOICEPRINT_DECISION_REASON_MAX_CHARS, pattern=r"\S")
    evidence_type: Literal["named_address", "visible_speaking", "text_identity", "contradiction"] | None = None


ParseOperation = Annotated[
    AddEvent | PatchEvent | SetEvent | PersonsOperation | EventArrayOperation | CompactOperation | VoiceDecision,
    Field(discriminator="op"),
]


class ParseCanvasWrite(_Operation):
    batch_id: str = Field(min_length=1, pattern=r"\S")
    expected_revision: int = Field(ge=0, strict=True)
    operations: list[ParseOperation] = Field(min_length=1)


class CanvasCompletion(_Operation):
    complete: Literal[True]


class FinishVoiceprintReview(_Operation):
    complete: Literal[True]


class PrefixCompactCompletion(CanvasCompletion):
    """The configured zero-tool Prefix path can update summaries only."""

    series_compact: str
    video_compact: str


class ParseContextCanvas(ContextCanvas):
    """Share storage and transactions while enforcing one bound parse window."""

    def __init__(
        self, *, scope: str, members: Mapping[str, dict[str, Any]],
        evidence: Mapping[str, dict[str, Any]] | None = None,
        background: Mapping[str, Any] | None = None,
        stage: Literal["understanding", "refine", "prefix"] = "refine",
        compact: dict[str, str] | None = None,
        compact_limits: tuple[int, int] | None = None,
        write_bounds: tuple[int, int] | None = None,
    ) -> None:
        super().__init__(scope=scope)
        if not members:
            raise ValueError("a Parse Canvas must bind at least one member")
        self.stage = stage
        self._authority = deepcopy(dict(members))
        self._initial = {
            ref: VideoParseResult.model_validate(member["content"])
            for ref, member in members.items()
        }
        self._speaker_source_members = {
            ref for ref, result in self._initial.items()
            if any(getattr(event, "speaker_source", None) is not None for event in result.timeline)
        }
        self._compact_limits = compact_limits
        self._voiceprint_modes: dict[str, str] = {}
        self._write_bounds = write_bounds or (
            min(member["bounds"][0] for member in members.values()),
            max(member["bounds"][1] for member in members.values()),
        )

        def initialize(draft):
            draft["members"] = {}
            draft["events"] = {}
            draft["evidence"] = deepcopy(dict(evidence or {}))
            draft["background"] = {"context": deepcopy(dict(background or {}))}
            draft["compact"] = {"prefix": deepcopy(compact or {})}
            for ref, member in self._authority.items():
                draft["members"][ref] = {
                    "sequence_no": member["sequence_no"],
                    "content_version": member["version"],
                    "scope": list(member.get("scope", [member["sequence_no"]])),
                    "persons": list(self._initial[ref].persons),
                    "start_ms": member["bounds"][0],
                    "end_ms": member["bounds"][1],
                }
                for position, event in enumerate(self._initial[ref].timeline):
                    value = event.model_dump(mode="json")
                    identity = {
                        "member_ref": ref, "version": member["version"],
                        "position": position, "fingerprint": content_fingerprint(value),
                    }
                    event_ref = "event:" + content_fingerprint([self.id, identity])[:32]
                    draft["events"][event_ref] = {
                        "member_ref": ref, "event": value, "source": identity,
                    }

        self.mutate(initialize, readonly_collections=("evidence", "background"))
        self._initial_identities = {
            ref: value["source"] for ref, value in self.items("events")
        }
        self._initial_times = {
            ref: (value["event"]["start_seconds"], value["event"]["end_seconds"])
            for ref, value in self.items("events")
        }

    @classmethod
    def from_refine(cls, snapshot):
        refs = snapshot["member_sequence_nos"]
        canvas = cls(
            scope="refine:" + snapshot["interval_parse_task_id"],
            members={
                ref: {
                    "sequence_no": sequence, "version": snapshot["member_versions"][ref],
                    "bounds": snapshot["member_bounds"][ref],
                    "scope": snapshot["member_scopes"][ref],
                    "content": snapshot["member_contents"][ref],
                } for ref, sequence in refs.items()
            },
            evidence=snapshot.get("member_evidence", {}),
            background=snapshot.get("background", {}),
            write_bounds=snapshot.get("write_bounds"),
        )
        canvas._apply_voiceprint()
        return canvas

    def _apply_voiceprint(self) -> None:
        """Bind prepared evidence only after the original snapshot is captured."""
        evidence = dict(self.items("evidence"))
        self._voiceprint_modes = {
            member_ref: value["voiceprint"]["mode"]
            for member_ref, value in evidence.items()
            if value.get("voiceprint", {}).get("mode") in {"flag", "prefill"}
        }
        if not self._voiceprint_modes:
            return

        def apply(draft):
            draft["voiceprint_review"] = {}
            for reference, value in draft["events"].items():
                member_ref, event = value["member_ref"], value["event"]
                if member_ref not in self._voiceprint_modes or event["event_type"] != "DIALOGUE":
                    continue
                event.setdefault("speaker_source", "understanding")
                prepared = evidence[member_ref]["voiceprint"]
                lines = prepared.get("lines", [])
                matching = [line for line in lines if (
                    line["start_ms"], line["end_ms"]
                ) == (event["start_seconds"], event["end_seconds"])]
                positioned = [line for line in matching
                              if line.get("position") == value["source"]["position"]]
                line = positioned[0] if len(positioned) == 1 else matching[0] if len(matching) == 1 else None
                if line is None:
                    continue
                candidates = line.get("candidates") or []
                value["voice_evidence"] = {
                    **{key: deepcopy(line[key]) for key in (
                        "start_ms", "end_ms", "understanding_speaker", "duration_ms",
                        "band", "conflict", "reason", "gap",
                        "label_score", "label_rank", "label_count", "label_margin",
                        "label_ref_count", "label_ref_consistency", "label_doubtful",
                        "label_ref_higher_count", "label_ref_higher_fraction",
                        "label_doubtful_basis",
                    ) if key in line},
                    "candidates": [{key: deepcopy(candidate[key]) for key in (
                        "speaker", "similarity", "reference_count", "independent_cue_count",
                    ) if key in candidate} for candidate in candidates],
                    "line_id": line.get("id"), "event_ref": reference, "valid": True,
                    "evidence_ref": member_ref, "evidence_section": "voiceprint",
                    "method_version": prepared.get("method_version"),
                }
                if (self._voiceprint_modes[member_ref] == "prefill" and line.get("band") == "ACCEPT"
                        and candidates and event["speaker_source"] != "refine_edit"):
                    event["speaker"] = candidates[0]["speaker"]
                    event["speaker_source"] = "voice_default"
                    member = draft["members"][member_ref]
                    if event["speaker"] not in member["persons"]:
                        member["persons"].append(event["speaker"])
                    if event["speaker"] not in event["participants"]:
                        event["participants"].append(event["speaker"])
            self._validate_draft(draft)

        self.mutate(apply)

    def voiceprint_review(self) -> dict[str, Any] | None:
        if not self._voiceprint_modes:
            return None
        conflicts, accept_disagreements, doubtful_labels = [], [], []
        for reference, value in self.items("events"):
            evidence = value.get("voice_evidence")
            if not evidence or not evidence["valid"]:
                continue
            target = {
                "event_ref": reference, "member_ref": value["member_ref"],
                "line_id": evidence.get("line_id"),
                "start_ms": evidence["start_ms"], "end_ms": evidence["end_ms"],
                "understanding_speaker": evidence.get("understanding_speaker"),
                **{key: evidence[key] for key in (
                    "label_score", "label_rank", "label_count", "label_margin",
                    "label_ref_count", "label_ref_consistency", "label_doubtful",
                    "label_ref_higher_count", "label_ref_higher_fraction",
                    "label_doubtful_basis",
                ) if key in evidence},
            }
            if evidence.get("conflict") or evidence.get("band") == "CONFLICT":
                conflicts.append(target)
            candidates = evidence.get("candidates") or []
            if (evidence.get("band") == "ACCEPT" and candidates
                    and candidates[0]["speaker"] != evidence.get("understanding_speaker")):
                accept_disagreements.append(target)
            if evidence.get("label_doubtful"):
                doubtful_labels.append(target)
        return {"modes": dict(self._voiceprint_modes), "conflicts": conflicts,
                "accept_disagreements": accept_disagreements, "doubtful_labels": doubtful_labels}

    def _required_voice_events(self, draft):
        if self.stage != "refine" or not self._voiceprint_modes:
            return {}
        required = {}
        for reference, value in draft["events"].items():
            event, evidence = value["event"], value.get("voice_evidence")
            if (value["member_ref"] not in self._voiceprint_modes or event["event_type"] != "DIALOGUE"
                    or not evidence or not evidence.get("valid")
                    or not self._write_bounds[0] <= event["start_seconds"] < event["end_seconds"] <= self._write_bounds[1]):
                continue
            candidates = evidence.get("candidates") or []
            if (evidence.get("label_doubtful") or evidence.get("conflict") or evidence.get("band") == "CONFLICT"
                    or evidence.get("band") == "ACCEPT" and candidates
                    and candidates[0]["speaker"] != evidence.get("understanding_speaker")):
                required[reference] = value
        return required

    @staticmethod
    def _voice_null_requires_contradiction(evidence):
        gap = evidence.get("gap")
        return evidence.get("band") == "ACCEPT" or gap is not None and gap >= VOICEPRINT_B0_VOICE_TOP1_MIN_LEAD

    @classmethod
    def _voice_decision_consistent(cls, value):
        record, event, evidence = value["voice_decision"], value["event"], value["voice_evidence"]
        speaker, understanding = event.get("speaker"), evidence.get("understanding_speaker")
        candidates = evidence.get("candidates") or []
        top1 = candidates[0]["speaker"] if candidates else None
        if record["decision"] == "keep":
            return speaker == understanding and (not evidence.get("label_doubtful") or record.get("evidence_type") in {
                "named_address", "visible_speaking", "text_identity",
            })
        if record["decision"] == "voice_top1":
            return top1 is not None and speaker == top1
        if record["decision"] == "other":
            return isinstance(speaker, str) and bool(speaker.strip()) and speaker not in {understanding, top1}
        return speaker is None and (not cls._voice_null_requires_contradiction(evidence)
                                    or record.get("evidence_type") == "contradiction")

    def _voiceprint_review_status(self, draft):
        required = self._required_voice_events(draft)
        missing = [reference for reference, value in required.items() if "voice_decision" not in value]
        inconsistent = [reference for reference, value in required.items()
                        if "voice_decision" in value and not self._voice_decision_consistent(value)]
        return {
            "ready": not missing and not inconsistent, "missing": missing, "inconsistent": inconsistent,
            "retry_hint": ("Record a voice_decision for each listed event (edit the speaker only if your decision "
                           "changes it), then call "
                           "finish_voiceprint_review again.") if missing or inconsistent else "",
        }

    def voiceprint_review_status(self) -> dict[str, Any]:
        return self._voiceprint_review_status(self.snapshot().collections)

    def _voiceprint_decision_records(self, draft):
        records = []
        for reference, value in self._required_voice_events(draft).items():
            evidence = value["voice_evidence"]
            candidates = evidence.get("candidates") or []
            records.append({
                "event_ref": reference, "member_chunk_id": value["member_ref"],
                "start_ms": evidence["start_ms"], "end_ms": evidence["end_ms"],
                "understanding_speaker": evidence.get("understanding_speaker"),
                "voice_top1": candidates[0]["speaker"] if candidates else None,
                "gap": evidence.get("gap"), "band": evidence.get("band"),
                **{key: evidence[key] for key in (
                    "label_score", "label_rank", "label_count", "label_margin",
                    "label_ref_count", "label_ref_consistency", "label_doubtful",
                    "label_ref_higher_count", "label_ref_higher_fraction",
                    "label_doubtful_basis",
                ) if key in evidence},
                **value.get("voice_decision", {}), "final_speaker": value["event"].get("speaker"),
            })
        return records

    def finish_voiceprint_review(self) -> dict[str, Any]:
        def finish(draft):
            status = self._voiceprint_review_status(draft)
            if self._voiceprint_modes:
                gate = draft["voiceprint_review"]
                gate.clear()
                if status["ready"]:
                    gate["signature"] = content_fingerprint(self._voiceprint_decision_records(draft))
            return status

        with self._lock:
            result = self.mutate(finish)
            return {**result, "revision": self.revision}

    def voiceprint_review_passed(self) -> bool:
        if not self._voiceprint_modes:
            return True
        draft = self.snapshot().collections
        return (self._voiceprint_review_status(draft)["ready"]
                and draft["voiceprint_review"].get("signature")
                == content_fingerprint(self._voiceprint_decision_records(draft)))

    def voiceprint_decisions(self) -> list[dict[str, Any]]:
        with self._lock:
            if not self.voiceprint_review_passed():
                raise AgentToolFeedbackError(
                    "VOICEPRINT_REVIEW_REQUIRED", "Voice conflict decisions have not passed the current review gate.",
                    "Resolve the required decisions and call finish_voiceprint_review before completing.",
                )
            return self._voiceprint_decision_records(self.snapshot().collections)

    @classmethod
    def from_prefix(cls, context):
        config = context["inputs"].parse_config.prefix_context
        return cls(
            scope="prefix:" + context["chunk_parse_task_id"], stage="prefix",
            members={context["chunk_parse_task_id"]: {
                "sequence_no": context["sequence_no"], "version": context["content_version"],
                "bounds": (context["allowed_start_seconds"], context["allowed_end_seconds"]),
                "scope": context["scope"], "content": context["content"],
            }},
            evidence={context["chunk_parse_task_id"]: context.get("evidence", {})},
            background=context.get("background", {}),
            compact={
                "series_compact": context["series_prefix_compact"],
                "video_compact": context["video_prefix_compact"],
            },
            compact_limits=(config.series_compact_max_chars, config.video_compact_max_chars),
            write_bounds=context.get("write_bounds"),
        )

    def catalog(self, collection: str, *, role: str) -> list[dict[str, Any]]:
        self._check_role(role)
        return [
            {
                "reference": ref,
                **({"member_ref": value["member_ref"], "event_type": value["event"]["event_type"]}
                   if collection == "events" else {}),
                **({"speaker": value["event"].get("speaker"), **({"voice": {
                    "top1": (value["voice_evidence"]["candidates"][0]["speaker"]
                             if value["voice_evidence"].get("candidates") else None),
                    "gap": value["voice_evidence"].get("gap"), "band": value["voice_evidence"].get("band"),
                    "conflict": value["voice_evidence"].get("conflict", False),
                    "label_doubtful": value["voice_evidence"].get("label_doubtful", False),
                    "label_rank": (f'{value["voice_evidence"]["label_rank"]}/{value["voice_evidence"]["label_count"]}'
                                   if value["voice_evidence"].get("label_rank") is not None
                                   and value["voice_evidence"].get("label_count") is not None else None),
                    "label_margin": value["voice_evidence"].get("label_margin"),
                    **({"label_doubtful_basis": value["voice_evidence"]["label_doubtful_basis"]}
                       if "label_doubtful_basis" in value["voice_evidence"] else {}),
                }} if value.get("voice_evidence", {}).get("valid") else {})}
                   if collection == "events" and value["member_ref"] in self._voiceprint_modes
                   and value["event"]["event_type"] == "DIALOGUE" else {}),
                **({"sequence_no": value["sequence_no"], "content_version": value["content_version"],
                    "scope": value["scope"], "start_ms": value["start_ms"], "end_ms": value["end_ms"]}
                   if collection == "members" else {}),
            } for ref, value in self.items(collection)
        ]

    def sections(self, collection: str, reference: str, *, role: str) -> dict[str, str]:
        self._check_role(role)
        value = self.get(collection, reference)
        if value is None:
            raise ValueError("unknown Canvas reference; read the current catalog")
        sections = {"content": json.dumps(value, ensure_ascii=False, sort_keys=True)}
        if collection == "evidence":
            sections.update({
                key: json.dumps(item, ensure_ascii=False, sort_keys=True)
                for key, item in value.items()
            })
        return sections

    def checkpoint(self, *, role: str | None = None) -> str:
        return json.dumps({
            "scope": self.scope, "canvas_id": self.id, "revision": self.revision,
            "stage": self.stage,
            "write_bounds_ms": self._write_bounds,
            "collections": {name: self.catalog(name, role=role or self.stage)
                            for name in ("members", "events", "evidence", "background", "compact")},
            "read": "Use get_context_canvas to restore exact facts; checkpoint is navigation only.",
        }, ensure_ascii=False, sort_keys=True)

    def write(
        self, *, role: str, batch_id: str, expected_revision: int,
        operations: list[dict[str, Any] | BaseModel], maximum_result_bytes: int | None = None,
    ):
        self._check_role(role)
        request = ParseCanvasWrite.model_validate({
            "batch_id": batch_id, "expected_revision": expected_revision,
            "operations": operations,
        })

        def apply(draft, ops):
            results = []
            selected_speakers = set()
            voice_before = content_fingerprint(self._voiceprint_decision_records(draft)) if self._voiceprint_modes else None
            for index, payload in enumerate(ops):
                operation = ParseCanvasWrite.model_validate({
                    "batch_id": batch_id, "expected_revision": expected_revision,
                    "operations": [payload],
                }).operations[0]
                if isinstance(operation, AddEvent):
                    self._member(draft, operation.member_ref)
                    reference = "event:" + content_fingerprint([
                        self.id, operation.member_ref, operation.key,
                    ])[:32]
                    value = {
                        "member_ref": operation.member_ref,
                        "event": operation.event.model_dump(mode="json"),
                        "source": {"key": operation.key},
                    }
                    existing = draft["events"].get(reference)
                    previous = existing["event"] if existing is not None else {}
                    value["event"].pop("speaker_source", None)
                    if (self._tracks_speaker_source(operation.member_ref)
                            and value["event"]["event_type"] == "DIALOGUE"):
                        value["event"]["speaker_source"] = (
                            "refine_edit" if value["event"]["speaker"] != previous.get("speaker")
                            else previous.get("speaker_source") or "understanding"
                        )
                    if existing is not None and existing != value:
                        raise ValueError("add_event key was already used for different facts")
                    draft["events"][reference] = value
                elif isinstance(operation, SetEvent):
                    reference = operation.event_ref
                    value = self._event(draft, reference)
                    previous = value["event"]
                    value["event"] = operation.event.model_dump(mode="json")
                    value["event"].pop("speaker_source", None)
                    if (self._tracks_speaker_source(value["member_ref"])
                            and value["event"]["event_type"] == "DIALOGUE"):
                        if value["event"]["speaker"] != previous.get("speaker"):
                            selected_speakers.add(reference)
                            value["event"]["speaker_source"] = "refine_edit"
                        else:
                            value["event"]["speaker_source"] = previous.get("speaker_source") or "understanding"
                    if (value["event"].get("speaker") != previous.get("speaker")
                            and "voice_decision" in value and not self._voice_decision_consistent(value)):
                        value.pop("voice_decision")
                elif isinstance(operation, PatchEvent):
                    reference = operation.event_ref
                    value = self._event(draft, reference)
                    event = value["event"]
                    previous_speaker = event.get("speaker")
                    event.update(operation.fields.model_dump(mode="json", exclude_unset=True))
                    if (self._tracks_speaker_source(value["member_ref"])
                            and "speaker" in operation.fields.model_fields_set
                            and event["speaker"] != previous_speaker):
                        selected_speakers.add(reference)
                        event["speaker_source"] = "refine_edit"
                    if (event.get("speaker") != previous_speaker
                            and "voice_decision" in value and not self._voice_decision_consistent(value)):
                        value.pop("voice_decision")
                elif isinstance(operation, VoiceDecision):
                    reference = operation.event_ref
                    required = self._required_voice_events(draft)
                    if reference not in required:
                        raise AgentToolFeedbackError(
                            "INVALID_VOICEPRINT_DECISION", "This event has no required voice conflict in the write window.",
                            "Use a current conflict event_ref with valid voice evidence inside the write bounds.",
                        )
                    evidence = required[reference]["voice_evidence"]
                    if (operation.decision == "keep" and evidence.get("label_doubtful")
                            and operation.evidence_type not in {"named_address", "visible_speaking", "text_identity"}):
                        raise AgentToolFeedbackError(
                            "VOICEPRINT_KEEP_EVIDENCE_REQUIRED", "Keeping a doubtful label requires specific identity evidence.",
                            "Supply evidence_type=named_address, visible_speaking or text_identity and cite the observation "
                            "in reason; otherwise choose a supported speaker or null.",
                        )
                    if (operation.decision == "null" and self._voice_null_requires_contradiction(evidence)
                            and operation.evidence_type != "contradiction"):
                        raise AgentToolFeedbackError(
                            "VOICEPRINT_NULL_EVIDENCE_REQUIRED", "Abstaining from this voice ranking requires specific evidence.",
                            "Supply evidence_type=contradiction and cite the contradictory observation, very short audio, "
                            "singing or overlap in reason; otherwise choose the supported voice candidate.",
                        )
                    required[reference]["voice_decision"] = operation.model_dump(
                        exclude={"op", "event_ref"}, exclude_none=True,
                    )
                elif isinstance(operation, PersonsOperation):
                    reference = operation.member_ref
                    member = self._member(draft, reference)
                    member["persons"] = self._array(member["persons"], operation.action, operation.values)
                elif isinstance(operation, EventArrayOperation):
                    reference = operation.event_ref
                    event = self._event(draft, reference)["event"]
                    if operation.field not in event:
                        raise ValueError("this event has no requested array field")
                    values = [item.model_dump(mode="json") if isinstance(item, BaseModel) else item
                              for item in operation.values]
                    event[operation.field] = self._array(event[operation.field], operation.action, values)
                else:
                    if self.stage != "prefix" or self._compact_limits is None:
                        raise ValueError("compact summaries can only be written by Prefix")
                    if (len(operation.series_compact) > self._compact_limits[0]
                            or len(operation.video_compact) > self._compact_limits[1]):
                        raise ValueError("compact summary exceeds its configured character budget")
                    reference = "prefix"
                    draft["compact"][reference] = operation.model_dump(exclude={"op"})
                results.append({"operation": index, "reference": reference})
            for reference, value in draft["events"].items():
                evidence, event = value.get("voice_evidence"), value["event"]
                if evidence and (event["event_type"] != "DIALOGUE" or (
                    event["start_seconds"], event["end_seconds"]
                ) != (evidence["start_ms"], evidence["end_ms"])):
                    evidence["valid"] = False
                    evidence["invalid_reason"] = "audio_interval_changed"
                    value.pop("voice_decision", None)
                    if (event.get("speaker_source") == "voice_default"
                            and reference not in selected_speakers):
                        event["speaker"] = evidence.get("understanding_speaker")
                        event["speaker_source"] = "understanding"
            self._validate_draft(draft)
            if self._voiceprint_modes and voice_before != content_fingerprint(self._voiceprint_decision_records(draft)):
                draft["voiceprint_review"].clear()
            return results

        try:
            return self.atomic_batch(
                batch_id=batch_id, expected_revision=expected_revision,
                operations=[op.model_dump(mode="json", exclude_unset=True) for op in request.operations],
                apply=apply, role=role, maximum_result_bytes=maximum_result_bytes,
            )
        except AgentToolFeedbackError:
            raise
        except ValueError as error:
            raise AgentToolFeedbackError(
                "INVALID_OPERATIONS", "Canvas batch validation failed: " + str(error),
                "Read the affected entries and retry a corrected whole batch; no operation was applied.",
            ) from error

    def result(self, member_ref: str) -> VideoParseResult:
        snapshot = self.snapshot()
        return self._result(snapshot.collections, member_ref)

    def register_evidence(self, reference: str, value: dict[str, Any]) -> None:
        self.put("evidence", reference, value, readonly=True)

    def interval_result(self) -> ValidatedIntervalRefinement:
        updates = []
        for member_ref, member in self._authority.items():
            current = self.result(member_ref)
            original = self._initial[member_ref]
            fields = {field: getattr(current, field) for field in ("persons", "timeline")
                      if getattr(current, field) != getattr(original, field)}
            if fields:
                updates.append(ChunkResultUpdate(sequence_no=member["sequence_no"], **fields))
        return ValidatedIntervalRefinement(updates=updates)

    def prefix_result(self) -> ValidatedPrefixRefinement:
        compact = self.get("compact", "prefix")
        updates = self.interval_result().updates
        return ValidatedPrefixRefinement(
            series_compact=compact["series_compact"], video_compact=compact["video_compact"],
            update=updates[0] if updates else None,
        )

    def _check_role(self, role):
        if role != self.stage:
            raise AgentToolFeedbackError(
                "CANVAS_ROLE_DENIED", "This Canvas belongs to another parse stage.",
                "Use only the tools bound to your current execution.",
            )

    def _member(self, draft, reference):
        if reference not in self._authority or reference not in draft["members"]:
            raise ValueError("member reference is outside the bound parse window")
        return draft["members"][reference]

    def _tracks_speaker_source(self, member_ref):
        return member_ref in self._voiceprint_modes or member_ref in self._speaker_source_members

    def _event(self, draft, reference):
        if reference not in draft["events"]:
            raise ValueError("unknown event reference; read the current event catalog")
        value = draft["events"][reference]
        self._member(draft, value["member_ref"])
        if reference in self._initial_identities and value["source"] != self._initial_identities[reference]:
            raise ValueError("event source identity changed")
        return value

    @staticmethod
    def _array(current, action, values):
        if action == "set":
            return deepcopy(values)
        if action == "remove":
            return [value for value in current if value not in values]
        return [*current, *(deepcopy(value) for value in values if value not in current)]

    def _result(self, draft, member_ref):
        member = self._member(draft, member_ref)
        timeline = [value["event"] for value in draft["events"].values()
                    if value["member_ref"] == member_ref]
        timeline.sort(key=lambda event: event["start_seconds"])
        return VideoParseResult.model_validate({"persons": member["persons"], "timeline": timeline})

    def _validate_draft(self, draft):
        start = min(member["bounds"][0] for member in self._authority.values())
        end = max(member["bounds"][1] for member in self._authority.values())
        for reference in self._initial_identities:
            self._event(draft, reference)
        for reference, value in draft["events"].items():
            event = value["event"]
            times = (event["start_seconds"], event["end_seconds"])
            if self._initial_times.get(reference) != times and (
                times[0] < self._write_bounds[0] or times[1] > self._write_bounds[1]
            ):
                raise ValueError("new events and timing changes must stay inside the authorized write window")
        for member_ref in self._authority:
            validate_result_time_range(self._result(draft, member_ref), start_seconds=start, end_seconds=end)
