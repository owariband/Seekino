"""Voiceprint preparation, scoring and registration rules over persistence DTOs.

The caller owns the transaction; repositories perform SQL and explicit saves.
"""

from __future__ import annotations

import math
import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from datetime import datetime
from typing import Any

from pydantic import ValidationError
from sqlalchemy.orm import Session

from agent.video_parse_v2.scheduler import voiceprint_preparation_min_chunks
from engine.parsev2.voiceprint import CharacterVoiceprint, Voiceprint, VoiceprintReference
from engine.parsev2.validation import parse_content_fingerprint
from engine.parsev2.voiceprint_matching import is_label_doubtful

from ..constants.parse import (
    RUNTIME_DETAIL_KEY,
    VOICEPRINT_PREPARATION_KEY as _VOICEPRINT_PREPARATION_KEY,
    VOICEPRINT_REFS_PER_SPEAKER,
    VOICEPRINT_CONSISTENCY_MIN_REFS,
    VOICEPRINT_CONSISTENCY_MAX_CANDIDATE_MEAN,
    VOICEPRINT_B0_MIN_AUDIO_MS,
    VOICEPRINT_B0_METHOD_VERSION,
    VOICEPRINT_B0_INCLUSIVE_TAIL_METHOD_VERSION,
    VOICEPRINT_B0_COHERENCE_METHOD_VERSION,
)
from ..model.dto.jobDTO import IntervalParseLease, VideoParseLease, VideoParseTaskData
from ..model.job import (
    ChunkTaskPhase,
    VideoParseTaskStatus,
)
from ..utils.retry import task_retry_delay
from ..utils.time import datetime_to_unix_time_ms
from .video_parse_errors import (
    VideoParseConflictError,
    VideoParseSnapshotConflictError,
    VoiceprintRegistrationRejectedError,
    VideoParseLeaseLostError,
)
from .video_parse_task import (
    _locked_video_lease,
    _ensure_runtime,
    _execution_config,
)
from .video_parse_content import (
    _interval_for_lease,
)

from ..repository import video_parse_task as task_store
from ..repository import video_parse_chunk as chunk_store


def _voiceprint_event_fingerprint(event: Mapping[str, Any]) -> str:
    """Unrelated canvas edits do not invalidate a still-identical voice anchor."""
    identity = {key: event.get(key) for key in (
        "event_type", "speaker", "start_seconds", "end_seconds", "text_variants",
    )}
    return hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


def _voiceprint_scoring_snapshot(
    session: Session, parent: VideoParseTaskData, lease: VideoParseLease, started_at: datetime,
) -> dict[str, Any]:
    """Freeze every terminal Understanding source; scoring remains execution-local."""
    rows = chunk_store.list_task_chunk_content_rows(
        session, parent.id, video_id=parent.video_id, include_missing_content=True, for_update=True,
    )
    committed = [(task, chunk, result) for task, chunk, result in rows
                 if result is not None and result.content is not None]
    failed = [(task, chunk) for task, chunk, result in rows
              if (result is None or result.content is None)
              and task.phase == ChunkTaskPhase.PARSE and task.status == "FAILED"]
    if (len(rows) != parent.chunk_count
        or [chunk.sequence_no for _task, chunk, _result in rows] != list(range(parent.chunk_count))
        or len(committed) + len(failed) != parent.chunk_count):
        raise VideoParseConflictError("voiceprint scoring needs every Understanding chunk to be terminal")
    detail = _ensure_runtime(parent.detail)
    phase = dict(detail[RUNTIME_DETAIL_KEY].get(_VOICEPRINT_PREPARATION_KEY) or {})
    versions = {chunk.id: result.version for _task, chunk, result in committed}
    fingerprints = {chunk.id: parse_content_fingerprint(result.content) for _task, chunk, result in committed}
    failed_chunks = [{"chunk_task_id": task.id, "chunk_id": chunk.id,
                      "sequence_no": chunk.sequence_no, "last_error": deepcopy(task.last_error)}
                     for task, chunk in failed]
    same_execution = (phase.get("active") is True and phase.get("lease_version") == lease.lease_version
                      and phase.get("worker_id") == lease.worker_id)
    if same_execution:
        if (phase.get("source_versions") != versions or phase.get("source_fingerprints") != fingerprints
            or phase.get("failed_chunks") != failed_chunks):
            raise VideoParseSnapshotConflictError("Voiceprint source snapshot changed during scoring")
    else:
        existing_models = {json.dumps(record["model"], sort_keys=True)
                           for record in dict(detail.get("voiceprint") or {}).get("voiceprintList", [])}
        if len(existing_models) > 1:
            raise VideoParseSnapshotConflictError("Voiceprint sources contain incompatible encoder revisions")
        if existing_models:
            identity = json.loads(next(iter(existing_models)))
            if phase.get("encoder_model") is not None and phase["encoder_model"] != identity:
                raise VideoParseSnapshotConflictError("Voiceprint encoder differs from the frozen task")
            phase["encoder_model"] = identity
        committed_times = sorted(result.create_time for _task, _chunk, result in committed)
        previous_count = voiceprint_preparation_min_chunks(parent.chunk_count)
        previous_at = committed_times[previous_count - 1] if len(committed_times) >= previous_count else None
        terminal_times = [*committed_times, *(datetime_to_unix_time_ms(task.finish_time)
                                             for task, _chunk in failed if task.finish_time is not None)]
        terminal_at = max(terminal_times, default=datetime_to_unix_time_ms(started_at))
        phase.update({"active": True, "lease_version": lease.lease_version, "worker_id": lease.worker_id,
                      "source_chunk_ids": list(versions), "source_versions": versions,
                      "source_fingerprints": fingerprints, "failed_chunks": failed_chunks,
                      "timing": {"previous_75_percent_at_ms": previous_at,
                                 "all_understanding_terminal_at_ms": terminal_at,
                                 "extra_gate_wait_ms": terminal_at - previous_at if previous_at is not None else None,
                                 "preparation_started_at_ms": datetime_to_unix_time_ms(started_at)}})
        detail["voiceprint"] = Voiceprint().model_dump(mode="json")
    detail[RUNTIME_DETAIL_KEY][_VOICEPRINT_PREPARATION_KEY] = phase
    parent.detail = detail
    parent.update_time = datetime_to_unix_time_ms(started_at)
    task_store.save_task(session, parent)
    video = task_store.get_video(session, parent.video_id)
    if video is None:
        raise VideoParseConflictError("Voiceprint source Video no longer exists")
    sources = {}
    for task, chunk, result in committed:
        for position, event in enumerate(dict(result.content).get("timeline", [])):
            if event.get("event_type") != "DIALOGUE":
                continue
            fingerprint = _voiceprint_event_fingerprint(event)
            source_id = f"{chunk.id}:{position}:{fingerprint}"
            sources[source_id] = {"source_id": source_id, "chunk_task_id": task.id, "chunk_id": chunk.id,
                                  "event_index": position, "position": position,
                                  "source_version": result.version, "event_fingerprint": fingerprint,
                                  "speaker": event.get("speaker"), "start_ms": event["start_seconds"],
                                  "end_ms": event["end_seconds"],
                                  "text_variants": deepcopy(event["text_variants"])}
    speakers = sorted({source["speaker"] for source in sources.values() if source["speaker"]})
    return {"member_sequence_nos": {chunk.id: chunk.sequence_no for _task, chunk, _result in committed},
            "member_contents": {chunk.id: deepcopy(result.content) for _task, chunk, result in committed},
            "member_versions": versions,
            "member_evidence": {chunk.id: deepcopy(result.detail or {}) for _task, chunk, result in committed},
            "sources": sources, "speaker_list": speakers, "source_media_ref": video.source_uri,
            "voiceprint": deepcopy(detail["voiceprint"]), "failed_chunks": failed_chunks,
            "timing": deepcopy(phase["timing"])}


def _voiceprint_active_preparation(
    parent: VideoParseTaskData, lease: VideoParseLease,
) -> tuple[dict[str, Any], dict[str, Any]]:
    detail = _ensure_runtime(parent.detail)
    phase = dict(detail[RUNTIME_DETAIL_KEY].get(_VOICEPRINT_PREPARATION_KEY) or {})
    if (parent.is_deleted or (parent.parse_config.get("voiceprint") is None
                             and parent.parse_config.get("voiceprint_mode", "off") == "off")
        or dict(detail.get("voiceprint") or {}).get("ready") is True
        or phase.get("active") is not True or phase.get("lease_version") != lease.lease_version
        or phase.get("worker_id") != lease.worker_id):
        raise VideoParseLeaseLostError("Voiceprint preparation execution is no longer active")
    return detail, phase


def _voiceprint_reference_progress(
    references: Sequence[Mapping[str, Any]], speakers: set[str],
) -> dict[str, list[dict[str, Any]]]:
    progress = {speaker: {} for speaker in sorted(speakers)}
    for ref in references:
        if ref["speaker"] in progress:
            source_ref = f'{ref["chunk_id"]}:{ref["event_fingerprint"]}'
            progress[ref["speaker"]].setdefault(source_ref, {
                "source_ref": source_ref, "start_ms": ref["start_ms"], "end_ms": ref["end_ms"],
            })
    return {speaker: list(refs.values()) for speaker, refs in progress.items()}


def _voiceprint_registration_receipt(
    record: CharacterVoiceprint, reference: VoiceprintReference, *, refs_count: int, added: bool,
) -> dict[str, Any]:
    return {"added": added, "speaker": record.speaker, "model": record.model.model_dump(mode="json"),
            "refs_count": refs_count, "target_refs_count": VOICEPRINT_REFS_PER_SPEAKER,
            "reference": {**reference.model_dump(mode="json"),
                          "source_ref": f"{reference.chunk_id}:{reference.event_fingerprint}"}}


def _check_voiceprint_registration(
    session: Session, parent: VideoParseTaskData, lease: VideoParseLease, *, source_ref: str,
    speaker: str, start_ms: int, end_ms: int, model: str, encoder_revision: str,
    replace_source_ref: str | None = None,
) -> tuple[dict[str, Any], Voiceprint, int, dict[str, Any] | None]:
    detail, phase = _voiceprint_active_preparation(parent, lease)
    chunk_id, separator, fingerprint = source_ref.partition(":")
    if not separator or chunk_id not in phase["source_chunk_ids"] or speaker not in phase["speaker_list"]:
        raise VideoParseConflictError("voiceprint source or speaker is outside this preparation")
    rows = chunk_store.list_task_chunk_content_rows(
        session, parent.id, video_id=parent.video_id, chunk_ids=[chunk_id], for_update=True,
    )
    result = rows[0][2] if rows else None
    if result is None:
        raise VideoParseSnapshotConflictError("Voiceprint source no longer exists")
    event = next((event for event in dict(result.content or {}).get("timeline", [])
                  if event.get("event_type") == "DIALOGUE"
                  and _voiceprint_event_fingerprint(event) == fingerprint), None)
    if (event is None or event.get("speaker") != speaker
        or type(start_ms) is not int or type(end_ms) is not int
        or not event["start_seconds"] <= start_ms < end_ms <= event["end_seconds"]):
        raise VideoParseSnapshotConflictError("Voiceprint source identity or authorized segment changed")
    voiceprint = Voiceprint.model_validate(detail["voiceprint"])
    references = _valid_voiceprint_references(session, parent, voiceprint, for_update=True)
    if len(references) != sum(len(record.references) for record in voiceprint.voiceprintList):
        raise VideoParseSnapshotConflictError("Voiceprint registered source identity changed")
    count = len(_voiceprint_reference_progress(references, {speaker})[speaker])
    replacement = None
    if replace_source_ref is not None:
        old_chunk_id, old_separator, _old_fingerprint = replace_source_ref.partition(":")
        if not old_separator or old_chunk_id not in phase["source_chunk_ids"]:
            raise VoiceprintRegistrationRejectedError(
                "INVALID_VOICEPRINT_SOURCE", "Replacement source is outside this preparation.",
                "Use the old reference's source_ref returned by get_speaker_list.",
            )
        replacement = next(((item, ref) for item in voiceprint.voiceprintList for ref in item.references
                            if f"{ref.chunk_id}:{ref.event_fingerprint}" == replace_source_ref), None)
        if replacement is not None and (
            replacement[0].speaker != speaker or replacement[0].model.name != model
            or replacement[0].model.encoder_revision != encoder_revision
        ):
            raise VoiceprintRegistrationRejectedError(
                "VOICEPRINT_REPLACEMENT_MISMATCH", "Replacement must preserve the speaker and encoder version.",
                "Choose another authorized reference for the same speaker; do not move references between groups.",
            )
    for record in voiceprint.voiceprintList:
        for ref in record.references:
            if (ref.chunk_id, ref.event_fingerprint) != (chunk_id, fingerprint):
                continue
            if (record.speaker == speaker and record.model.name == model
                and record.model.encoder_revision == encoder_revision
                and (ref.start_ms, ref.end_ms) == (start_ms, end_ms)
                and (replacement is None or replacement[1] is ref)):
                receipt = _voiceprint_registration_receipt(
                    record, ref, refs_count=count, added=False,
                )
                if replace_source_ref is not None:
                    receipt["replaced_source_ref"] = replace_source_ref
                return detail, voiceprint, result.version, receipt
            if replacement is not None and replacement[1] is ref:
                continue  # Explicit replacement may refine this source's subrange without adding a case.
            raise VoiceprintRegistrationRejectedError(
                "VOICEPRINT_SOURCE_ALREADY_REGISTERED",
                f"Source {source_ref} already has a reference at {ref.start_ms}–{ref.end_ms} ms.",
                "Choose a different source_ref; another subrange of the same dialogue is not an independent case.",
            )
    if replace_source_ref is not None and replacement is None:
        raise VoiceprintRegistrationRejectedError(
            "VOICEPRINT_REFERENCE_NOT_FOUND", "The reference to replace is no longer registered.",
            "Refresh get_speaker_list; append normally if no existing reference needs replacing.",
        )
    for ref in references:
        if replacement is not None and f'{ref["chunk_id"]}:{ref["event_fingerprint"]}' == replace_source_ref:
            continue
        if ref["start_ms"] < end_ms and start_ms < ref["end_ms"]:
            raise VoiceprintRegistrationRejectedError(
                "VOICEPRINT_REFERENCE_OVERLAP",
                f'Requested range overlaps {ref["speaker"]}\'s reference at {ref["start_ms"]}–{ref["end_ms"]} ms.',
                "Choose another clean single-speaker range that does not overlap any registered reference.",
            )
    if replacement is not None:
        # This is a detached copy: only the final registration transaction persists the replacement.
        replacement[0].references.remove(replacement[1])
    return detail, voiceprint, result.version, None


def _voiceprint_consistency(references: Sequence[Mapping[str, Any]], speakers: set[str]) -> dict[str, Any]:
    """Flag low within-speaker mean similarity; this does not establish a character's name."""
    groups: dict[tuple[str, str, str], list[Mapping[str, Any]]] = {}
    for ref in references:
        key = (ref["speaker"], ref["model"]["name"], ref["model"]["encoder_revision"])
        groups.setdefault(key, []).append(ref)
    suspects = []
    checked_speakers = set()
    thresholds = {"minimum_references": VOICEPRINT_CONSISTENCY_MIN_REFS,
                  "max_candidate_mean": VOICEPRINT_CONSISTENCY_MAX_CANDIDATE_MEAN}
    for (speaker, _model, _revision), refs in groups.items():
        count = len(refs)
        if count < VOICEPRINT_CONSISTENCY_MIN_REFS:
            continue
        checked_speakers.add(speaker)
        vectors = []
        for ref in refs:
            norm = math.hypot(*ref["vector"])
            vectors.append([value / norm for value in ref["vector"]])
        pair_sums = [0.0] * count
        for i, vector in enumerate(vectors):
            for j in range(i):
                similarity = max(-1.0, min(1.0, math.fsum(a * b for a, b in zip(vector, vectors[j]))))
                pair_sums[i] += similarity
                pair_sums[j] += similarity
        for index, ref in enumerate(refs):
            candidate_mean = pair_sums[index] / (count - 1)
            if candidate_mean <= VOICEPRINT_CONSISTENCY_MAX_CANDIDATE_MEAN:
                suspects.append({"speaker": speaker, "source_ref": f'{ref["chunk_id"]}:{ref["event_fingerprint"]}',
                                 "start_ms": ref["start_ms"], "end_ms": ref["end_ms"],
                                 "model": ref["model"], "duration_ms": ref["duration_ms"],
                                 "reference_count": count, "candidate_mean": candidate_mean})
    return {"suspect_references": suspects,
            "consistency": {"similarity_metric": "cosine", "similarity_is_probability": False,
                            "thresholds": thresholds, "checked_speakers": sorted(checked_speakers),
                            "insufficient_speakers": sorted(speakers - checked_speakers)}}


def _valid_voiceprint_references(
    session: Session, parent: VideoParseTaskData, voiceprint: Voiceprint, *, for_update: bool,
) -> list[dict[str, Any]]:
    chunk_ids = {ref.chunk_id for item in voiceprint.voiceprintList for ref in item.references}
    if not chunk_ids:
        return []
    if voiceprint.ready:
        # 约定：准备完成的声源标签与音频区间视为正确的静态成果。
        # 不复核可变 timeline，不因其文本、speaker 或时间编辑作废参考或重跑 Refine。
        owned_chunks = set(chunk_store.list_owned_chunk_ids(
            session, parent.id, parent.video_id, sorted(chunk_ids),
        ))
        return [{"speaker": record.speaker, "model": record.model.model_dump(mode="json"),
                 "confidence": record.confidence, "source_start_ms": reference.start_ms,
                 "source_end_ms": reference.end_ms, **reference.model_dump(mode="json")}
                for record in voiceprint.voiceprintList for reference in record.references
                if reference.chunk_id in owned_chunks]
    rows = {chunk.id: result for _task, chunk, result in chunk_store.list_task_chunk_content_rows(
        session, parent.id, video_id=parent.video_id, chunk_ids=sorted(chunk_ids),
        for_update=for_update, order_by="chunk_id",
    )}
    valid = []
    for record in voiceprint.voiceprintList:
        for reference in record.references:
            result = rows.get(reference.chunk_id)
            if result is None or result.version < reference.source_version:
                continue
            event = next((event for event in dict(result.content or {}).get("timeline", [])
                          if event.get("event_type") == "DIALOGUE"
                          and _voiceprint_event_fingerprint(event) == reference.event_fingerprint), None)
            if (event is None or event.get("speaker") != record.speaker
                or not event["start_seconds"] <= reference.start_ms < reference.end_ms <= event["end_seconds"]):
                continue
            value = {"speaker": record.speaker, "model": record.model.model_dump(mode="json"),
                     "confidence": record.confidence, "source_start_ms": event["start_seconds"],
                     "source_end_ms": event["end_seconds"], **reference.model_dump(mode="json")}
            if value not in valid:
                valid.append(value)
    return valid


def _record_voiceprint_failure(
    parent: VideoParseTaskData, now: datetime, *, pending_failure: bool = False,
) -> tuple[int | None, bool]:
    """Update the caller's snapshot for real or interrupted preparation failures."""
    detail = _ensure_runtime(parent.detail)
    phase = dict(detail[RUNTIME_DETAIL_KEY].get(_VOICEPRINT_PREPARATION_KEY) or {})
    if dict(detail.get("voiceprint") or {}).get("ready") is True:
        return None, False
    enabled = (parent.parse_config.get("voiceprint") is not None
               or parent.parse_config.get("voiceprint_mode", "off") != "off")
    if phase.get("active") is not True and (not pending_failure or not enabled):
        return None, False
    failures = phase.get("failures", 0)
    if type(failures) is not int or failures < 0:
        raise ValueError("persisted Voiceprint failure count must be a nonnegative integer")
    failures += 1
    phase.update({"failures": failures, "active": False})
    detail[RUNTIME_DETAIL_KEY][_VOICEPRINT_PREPARATION_KEY] = phase
    parent.detail = detail
    execution = _execution_config(parent.parse_config)
    parent.next_execute_time = now + task_retry_delay(
        execution["retry_initial_delay_seconds"], execution["retry_max_delay_seconds"], failures,
    )
    return failures, failures > execution["max_retry_count"]


def begin_voiceprint_preparation(
    session: Session,
    *,
    lease: VideoParseLease,
    started_at: datetime,
) -> dict[str, Any] | None:
    parent = _locked_video_lease(session, lease, started_at)
    if parent.is_deleted or task_store.latest_video_task_id(session, parent.video_id) != parent.id:
        raise VideoParseLeaseLostError("Voiceprint parent task is no longer current")
    if (parent.parse_config.get("voiceprint") is None
        and parent.parse_config.get("voiceprint_mode", "off") == "off"):
        raise VideoParseConflictError("voiceprint preparation is disabled")
    detail = _ensure_runtime(parent.detail)
    voiceprint = Voiceprint.model_validate(detail.get("voiceprint", {}))
    if voiceprint.ready:
        return None
    if parent.status != VideoParseTaskStatus.PARSING.value:
        raise VideoParseConflictError("voiceprint preparation requires a PARSING task")
    if parent.parse_config.get("voiceprint_mode", "off") != "off":
        return _voiceprint_scoring_snapshot(session, parent, lease, started_at)
    runtime = detail[RUNTIME_DETAIL_KEY]
    phase = dict(runtime.get(_VOICEPRINT_PREPARATION_KEY) or {})
    rows = chunk_store.list_task_chunk_content_rows(
        session, parent.id, video_id=parent.video_id, content_not_null=True,
    )
    required = voiceprint_preparation_min_chunks(parent.chunk_count)
    if len(rows) < required:
        raise VideoParseConflictError("voiceprint preparation needs three quarters of the Understanding results")
    same_execution = (phase.get("active") is True
                      and phase.get("lease_version") == lease.lease_version
                      and phase.get("worker_id") == lease.worker_id)
    if same_execution:
        selected = set(phase["source_chunk_ids"])
        rows = [row for row in rows if row[1].id in selected]
        if len(rows) != len(selected):
            raise VideoParseSnapshotConflictError("Voiceprint source snapshot is incomplete")
    else:
        speakers = sorted({event["speaker"] for _task, _chunk, result in rows
                           for event in dict(result.content or {}).get("timeline", [])
                           if event.get("event_type") == "DIALOGUE"
                           and isinstance(event.get("speaker"), str) and event["speaker"].strip()})
        phase.update({"active": True, "lease_version": lease.lease_version,
                      "worker_id": lease.worker_id,
                      "source_chunk_ids": [chunk.id for _task, chunk, _result in rows],
                      "speaker_list": speakers})
        # A failed attempt's partial registrations never become the next snapshot.
        voiceprint = Voiceprint()
    runtime[_VOICEPRINT_PREPARATION_KEY] = phase
    detail["voiceprint"] = voiceprint.model_dump(mode="json")
    parent.detail = detail
    parent.update_time = datetime_to_unix_time_ms(started_at)
    task_store.save_task(session, parent)
    video = task_store.get_video(session, parent.video_id)
    if video is None:
        raise VideoParseConflictError("Voiceprint source Video no longer exists")
    sources = {}
    for _task, chunk, result in rows:
        for event in dict(result.content or {}).get("timeline", []):
            if (event.get("event_type") != "DIALOGUE"
                or not isinstance(event.get("speaker"), str) or not event["speaker"].strip()):
                continue
            fingerprint = _voiceprint_event_fingerprint(event)
            sources[f"{chunk.id}:{fingerprint}"] = {
                "chunk_id": chunk.id, "source_version": result.version,
                "event_fingerprint": fingerprint, "speaker": event["speaker"],
                "start_ms": event["start_seconds"], "end_ms": event["end_seconds"],
                "text_variants": deepcopy(event["text_variants"]),
            }
    return {
        "member_sequence_nos": {chunk.id: chunk.sequence_no for _task, chunk, _result in rows},
        "member_contents": {chunk.id: deepcopy(result.content) for _task, chunk, result in rows},
        "member_versions": {chunk.id: result.version for _task, chunk, result in rows},
        "member_evidence": {chunk.id: deepcopy(result.detail or {}) for _task, chunk, result in rows},
        "sources": sources, "speaker_list": list(phase["speaker_list"]),
        "source_media_ref": video.source_uri,
        "voiceprint": voiceprint.model_dump(mode="json"),
    }


def bind_voiceprint_encoder(
    session: Session,
    *,
    lease: VideoParseLease,
    bound_at: datetime,
    identity: dict[str, str],
) -> None:
    parent = _locked_video_lease(session, lease, bound_at)
    detail, phase = _voiceprint_active_preparation(parent, lease)
    if task_store.latest_video_task_id(session, parent.video_id) != parent.id:
        raise VideoParseLeaseLostError("Voiceprint parent task is no longer current")
    if phase.get("encoder_model") is not None and phase["encoder_model"] != identity:
        raise VideoParseSnapshotConflictError("Voiceprint encoder revision differs from the frozen task")
    phase["encoder_model"] = identity
    detail[RUNTIME_DETAIL_KEY][_VOICEPRINT_PREPARATION_KEY] = phase
    parent.detail = detail
    parent.update_time = datetime_to_unix_time_ms(bound_at)
    task_store.save_task(session, parent)


def finish_voiceprint_scoring(
    session: Session,
    *,
    lease: VideoParseLease,
    check_active: Callable[[], None] | None,
    descriptor: dict[str, str],
    finished_at: datetime,
    payload: dict[str, Any],
) -> dict[str, Any]:
    parent = _locked_video_lease(session, lease, finished_at)
    if check_active is not None:
        check_active()
    if parent.parse_config.get("voiceprint_mode", "off") == "off":
        raise VideoParseConflictError("voiceprint scoring is disabled")
    detail, phase = _voiceprint_active_preparation(parent, lease)
    if task_store.latest_video_task_id(session, parent.video_id) != parent.id:
        raise VideoParseLeaseLostError("Voiceprint parent task is no longer current")
    snapshot = _voiceprint_scoring_snapshot(session, parent, lease, finished_at)
    sources = snapshot["sources"]
    lines = payload.get("lines")
    samples = payload.get("samples")
    model = payload.get("model")
    if (not isinstance(payload.get("method_version"), str) or not payload["method_version"].strip()
        or not isinstance(payload.get("parameters"), dict)
        or not isinstance(model, dict) or not isinstance(model.get("name"), str)
        or not model["name"].strip() or not isinstance(model.get("encoder_revision"), str)
        or not model["encoder_revision"].strip()
        or not isinstance(lines, list) or not isinstance(samples, list)):
        raise VideoParseConflictError("voiceprint scoring result is incomplete")
    if phase.get("encoder_model") is not None and phase["encoder_model"] != model:
        raise VideoParseSnapshotConflictError("Voiceprint result encoder differs from the frozen task")
    label_policy = None
    label_loo = payload["method_version"] == VOICEPRINT_B0_METHOD_VERSION
    label_tail = label_loo or payload["method_version"] == VOICEPRINT_B0_INCLUSIVE_TAIL_METHOD_VERSION
    if label_tail or payload["method_version"] == VOICEPRINT_B0_COHERENCE_METHOD_VERSION:
        keys = ("label_min_rank", "label_min_margin", "label_min_duration_ms",
                "label_min_ref_count", "label_min_ref_higher_fraction" if label_tail else "label_min_consistency")
        if label_loo:
            keys += ("label_tail_min_refs",)
        if not set(keys).issubset(payload["parameters"]):
            raise VideoParseConflictError("voiceprint label policy is incomplete")
        label_policy = {key: payload["parameters"][key] for key in keys}
        try:
            is_label_doubtful({}, **label_policy)
        except (TypeError, ValueError) as error:
            raise VideoParseConflictError("voiceprint label policy is invalid") from error
    line_ids = [line.get("source_id") for line in lines if isinstance(line, dict)]
    if len(line_ids) != len(lines) or len(set(line_ids)) != len(lines) or set(line_ids) != set(sources):
        raise VideoParseConflictError("voiceprint scoring must cover every fixed dialogue exactly once")
    sample_ids = set()
    for sample in samples:
        source_id = sample.get("source_id") if isinstance(sample, dict) else None
        source = sources.get(source_id)
        if source is None or source_id in sample_ids:
            raise VideoParseConflictError("voiceprint sample is outside the fixed source set or duplicated")
        if sample.get("id") != source_id or "text_variants" in sample:
            raise VideoParseConflictError("voiceprint samples must use source IDs without transcript copies")
        sample_ids.add(source_id)
        if source["end_ms"] - source["start_ms"] < VOICEPRINT_B0_MIN_AUDIO_MS:
            raise VideoParseConflictError("voiceprint sample is shorter than the encoder minimum")
        if (sample.get("label") != source["speaker"]
            or any(sample.get(key) != source[key] for key in (
                "start_ms", "end_ms", "source_version", "event_fingerprint",
            )) or sample.get("model") != model["name"]
            or sample.get("encoder_revision") != model["encoder_revision"]):
            raise VideoParseSnapshotConflictError("voiceprint sample source identity changed")
        try:
            validated = VoiceprintReference.model_validate({
                **{key: source[key] for key in (
                    "chunk_id", "start_ms", "end_ms", "source_version", "event_fingerprint",
                )},
                "vector": sample.get("vector"), "pcm_sha256": sample.get("pcm_sha256"),
                "duration_ms": source["end_ms"] - source["start_ms"],
                "selection_basis": "Understanding label; B0 leave-one-out reference pool",
            })
        except ValidationError as error:
            raise VideoParseConflictError("voiceprint sample has an invalid CAM++ embedding") from error
        if not math.isclose(sum(value * value for value in validated.vector), 1.0,
                            rel_tol=1e-5, abs_tol=1e-5):
            raise VideoParseConflictError("voiceprint sample embedding must be L2-normalized")
    for line in lines:
        source = sources[line["source_id"]]
        if "vector" in line or "text_variants" in line:
            raise VideoParseConflictError("voiceprint scored lines cannot duplicate embeddings or transcripts")
        if any(line.get(key) != source[key] for key in ("start_ms", "end_ms")):
            raise VideoParseSnapshotConflictError("voiceprint scored interval changed")
        candidates = line.get("candidates")
        if (line.get("duration_ms") != source["end_ms"] - source["start_ms"]
            or line.get("band") not in {None, "ACCEPT", "CONFLICT", "NO_VOICE"}
            or not isinstance(line.get("reason"), str) or not line["reason"]
            or not isinstance(candidates, list) or len(candidates) > 3):
            raise VideoParseConflictError("voiceprint scored line is incomplete")
        names = []
        for candidate in candidates:
            if (not isinstance(candidate, dict) or not isinstance(candidate.get("speaker"), str)
                or not candidate["speaker"].strip()
                or type(candidate.get("similarity")) not in {int, float}
                or not -1 <= candidate["similarity"] <= 1):
                raise VideoParseConflictError("voiceprint candidate must contain a named cosine score")
            reference_ids = candidate.get("reference_ids")
            if ("src" in candidate or "vector" in candidate or "text_variants" in candidate
                or not isinstance(reference_ids, list)
                or any(not isinstance(reference_id, str) for reference_id in reference_ids)
                or len(set(reference_ids)) != len(reference_ids)
                or not set(reference_ids).issubset(sample_ids)
                or "reference_count" in candidate and candidate["reference_count"] != len(reference_ids)):
                raise VideoParseConflictError("voiceprint candidates must reference the shared sample pool")
            names.append(candidate["speaker"])
        if len(set(names)) != len(names) or candidates != sorted(
            candidates, key=lambda candidate: (-candidate["similarity"], candidate["speaker"]),
        ):
            raise VideoParseConflictError("voiceprint candidates must rank distinct characters")
        expected_gap = candidates[0]["similarity"] - candidates[1]["similarity"] if len(candidates) > 1 else None
        if (expected_gap is None and line.get("gap") is not None
            or expected_gap is not None and (type(line.get("gap")) not in {int, float}
                or not math.isclose(line["gap"], expected_gap, rel_tol=0, abs_tol=1e-9))):
            raise VideoParseConflictError("voiceprint gap must compare the first two distinct characters")
        if source["end_ms"] - source["start_ms"] < VOICEPRINT_B0_MIN_AUDIO_MS and candidates:
            raise VideoParseConflictError("voiceprint cannot score audio below the encoder minimum")
        if label_policy is not None:
            metrics = ("label_score", "label_rank", "label_count", "label_margin",
                       "label_ref_count", "label_ref_consistency")
            if label_tail:
                metrics += ("label_ref_higher_count", "label_ref_higher_fraction")
            if label_loo:
                metrics += ("label_doubtful_basis",)
            if (not set(metrics).issubset(line) or type(line.get("label_doubtful")) is not bool
                or line.get("understanding_speaker") != source["speaker"]):
                raise VideoParseConflictError("voiceprint label diagnostics are incomplete")
            values = [line[key] for key in metrics[:6]]
            if source["speaker"] in names:
                rank = names.index(source["speaker"]) + 1
                candidate = candidates[rank - 1]
                if (line["label_rank"] != rank or line["label_score"] != candidate["similarity"]
                    or line["label_ref_count"] != candidate.get("reference_count")
                    or "reference_consistency" in candidate
                    and line["label_ref_consistency"] != candidate["reference_consistency"]
                    or label_tail and (
                        line["label_ref_higher_count"] != candidate.get("reference_higher_count")
                        or line["label_ref_higher_fraction"] != candidate.get("reference_higher_fraction")
                    )):
                    raise VideoParseConflictError("voiceprint label diagnostics contradict the visible ranking")
            if any(value is not None for value in values):
                if (source["speaker"] is None or any(value is None for value in values)
                    or any(type(line[key]) is not int or line[key] < 1
                           for key in ("label_rank", "label_count", "label_ref_count"))
                    or line["label_rank"] > line["label_count"]
                    or line["label_rank"] <= len(names)
                    and names[line["label_rank"] - 1] != source["speaker"]
                    or line["label_count"] != line.get("candidate_count")
                    or any(type(line[key]) not in {int, float} or not math.isfinite(line[key])
                           for key in ("label_score", "label_margin", "label_ref_consistency"))
                    or not -1 <= line["label_score"] <= 1
                    or not -1 <= line["label_ref_consistency"] <= 1
                    or not candidates or line["label_margin"] < 0
                    or not math.isclose(line["label_margin"], candidates[0]["similarity"]
                                        - line["label_score"], rel_tol=0, abs_tol=1e-9)):
                    raise VideoParseConflictError("voiceprint label diagnostics are invalid")
                tail_missing = (line.get("label_ref_higher_count") is None
                                and line.get("label_ref_higher_fraction") is None)
                if label_tail and not (label_loo and tail_missing) and (
                    type(line["label_ref_higher_count"]) is not int
                    or not 0 <= line["label_ref_higher_count"] <= line["label_ref_count"]
                    or type(line["label_ref_higher_fraction"]) not in {int, float}
                    or not math.isfinite(line["label_ref_higher_fraction"])
                    or not math.isclose(line["label_ref_higher_fraction"],
                                        line["label_ref_higher_count"] / line["label_ref_count"],
                                        rel_tol=0, abs_tol=1e-12)
                ):
                    raise VideoParseConflictError("voiceprint label tail diagnostics are invalid")
            elif label_tail and (line["label_ref_higher_count"] is not None
                                 or line["label_ref_higher_fraction"] is not None):
                raise VideoParseConflictError("voiceprint unscored labels cannot have tail diagnostics")
            if line["label_doubtful"] != is_label_doubtful(line, **label_policy):
                raise VideoParseConflictError("voiceprint label flag does not match its diagnostics")
            if label_loo:
                basis = None
                if line["label_doubtful"]:
                    basis = ("rank_margin_only_few_refs" if line["label_ref_count"]
                             < label_policy["label_tail_min_refs"] else "tail")
                if line["label_doubtful_basis"] != basis:
                    raise VideoParseConflictError("voiceprint label basis does not match its policy")
    if label_policy is not None:
        hygiene = payload.get("hygiene")
        if not isinstance(hygiene, dict):
            raise VideoParseConflictError("voiceprint two-pass hygiene is missing")
        id_keys = ("excluded_reference_ids", "newly_flagged_ids", "cleared_flag_ids")
        if any(not isinstance(hygiene.get(key), list)
               or any(not isinstance(value, str) for value in hygiene[key])
               or len(hygiene[key]) != len(set(hygiene[key]))
               or not set(hygiene[key]).issubset(sources) for key in id_keys):
            raise VideoParseConflictError("voiceprint hygiene IDs are invalid")
        excluded = set(hygiene["excluded_reference_ids"])
        flagged = {line["source_id"] for line in lines if line["label_doubtful"]}
        if (hygiene.get("passes") != 2
            or hygiene.get("pass1_flag_count") != len(excluded)
            or hygiene.get("pass2_flag_count") != len(flagged)
            or hygiene.get("changed_flag_count") != len(excluded ^ flagged)
            or set(hygiene["newly_flagged_ids"]) != flagged - excluded
            or set(hygiene["cleared_flag_ids"]) != excluded - flagged
            or not excluded.issubset(sample_ids)
            or any(excluded.intersection(candidate["reference_ids"])
                   for line in lines for candidate in line["candidates"])):
            raise VideoParseConflictError("voiceprint two-pass hygiene does not match final scores")
    if payload.get("failed_chunks") != snapshot["failed_chunks"]:
        raise VideoParseSnapshotConflictError("voiceprint failed chunk set changed")
    timing = payload.get("timing")
    if not isinstance(timing, dict) or any(timing.get(key) != value
                                           for key, value in snapshot["timing"].items()):
        raise VideoParseSnapshotConflictError("voiceprint preparation timing does not match its snapshot")
    ready_at_ms = datetime_to_unix_time_ms(finished_at)
    previous_at_ms = snapshot["timing"]["previous_75_percent_at_ms"]
    detail = _ensure_runtime(parent.detail)
    phase = dict(detail[RUNTIME_DETAIL_KEY][_VOICEPRINT_PREPARATION_KEY])
    phase["active"] = False
    phase["timing"] = {**timing, "ready_at_ms": ready_at_ms,
                       "ready_minus_previous_75_percent_ms": ready_at_ms - previous_at_ms
                       if previous_at_ms is not None else None}
    detail[RUNTIME_DETAIL_KEY][_VOICEPRINT_PREPARATION_KEY] = phase
    manifest = Voiceprint.model_validate({
        "ready": True, "method_version": payload["method_version"], "model": model,
        "parameters": payload["parameters"], "line_count": len(lines), "sample_count": len(samples),
        **descriptor,
    }).model_dump(mode="json")
    detail["voiceprint"] = manifest
    if check_active is not None:
        check_active()
    parent.detail = detail
    parent.update_time = datetime_to_unix_time_ms(finished_at)
    task_store.save_task(session, parent)
    session.flush()
    if check_active is not None:
        check_active()
    return deepcopy(manifest)


def load_voiceprint_scoring(
    session: Session,
    *,
    lease: VideoParseLease | IntervalParseLease,
    loaded_at: datetime,
) -> dict[str, Any] | None:
    if isinstance(lease, IntervalParseLease):
        interval = _interval_for_lease(session, lease, loaded_at)
        parent = task_store.get_task(session, interval.video_task_id)
    else:
        parent = _locked_video_lease(session, lease, loaded_at)
    if (parent is None or parent.is_deleted
        or task_store.latest_video_task_id(session, parent.video_id) != parent.id):
        raise VideoParseLeaseLostError("Voiceprint parent task is no longer current")
    voiceprint = dict((parent.detail or {}).get("voiceprint") or {})
    if parent.parse_config.get("voiceprint_mode", "off") == "off" or voiceprint.get("ready") is not True:
        return None
    if "location" not in voiceprint:
        return None
    return Voiceprint.model_validate(voiceprint).model_dump(mode="json")


def precheck_voiceprint_registration(
    session: Session,
    *,
    lease: VideoParseLease,
    source_ref: str,
    speaker: str,
    start_ms: int,
    end_ms: int,
    model: str,
    encoder_revision: str,
    replace_source_ref: str | None,
    checked_at: datetime,
) -> dict[str, Any] | None:
    parent = _locked_video_lease(session, lease, checked_at)
    _detail, _voiceprint, _version, existing = _check_voiceprint_registration(
        session, parent, lease, source_ref=source_ref, speaker=speaker,
        start_ms=start_ms, end_ms=end_ms, model=model, encoder_revision=encoder_revision,
        replace_source_ref=replace_source_ref,
    )
    return existing


def register_voiceprint(
    session: Session,
    *,
    lease: VideoParseLease,
    source_ref: str,
    speaker: str,
    start_ms: int,
    end_ms: int,
    selection_basis: str,
    confidence: float | None,
    model: str,
    encoder_revision: str,
    vector: Sequence[float],
    pcm_sha256: str,
    duration_ms: int,
    replace_source_ref: str | None,
    registered_at: datetime,
) -> dict[str, Any]:
    parent = _locked_video_lease(session, lease, registered_at)
    detail, voiceprint, source_version, existing = _check_voiceprint_registration(
        session, parent, lease, source_ref=source_ref, speaker=speaker,
        start_ms=start_ms, end_ms=end_ms, model=model, encoder_revision=encoder_revision,
        replace_source_ref=replace_source_ref,
    )
    if existing is not None:
        return existing
    if any(ref.pcm_sha256 == pcm_sha256 for item in voiceprint.voiceprintList for ref in item.references):
        raise VoiceprintRegistrationRejectedError(
            "VOICEPRINT_DUPLICATE_AUDIO", "The same PCM audio is already registered in this preparation.",
            "Choose another independent recording; a new source_ref does not make identical audio a new case.",
        )
    chunk_id, _, fingerprint = source_ref.partition(":")
    reference = VoiceprintReference(
        chunk_id=chunk_id, start_ms=start_ms, end_ms=end_ms, vector=list(vector),
        source_version=source_version, event_fingerprint=fingerprint,
        pcm_sha256=pcm_sha256, duration_ms=duration_ms, selection_basis=selection_basis,
    )
    record = next((item for item in voiceprint.voiceprintList if item.speaker == speaker
                   and item.model.name == model and item.model.encoder_revision == encoder_revision), None)
    if record is None:
        # 首版不推断同人异名：保留 Agent 的人物标签，避免额外身份归一流程。
        record = CharacterVoiceprint(speaker=speaker, model={"name": model, "encoder_revision": encoder_revision},
                                     references=[], confidence=confidence)
        voiceprint.voiceprintList.append(record)
    record.references.append(reference)
    detail["voiceprint"] = voiceprint.model_dump(mode="json")
    parent.detail = detail
    parent.update_time = datetime_to_unix_time_ms(registered_at)
    task_store.save_task(session, parent)
    count = len({(ref.chunk_id, ref.event_fingerprint) for item in voiceprint.voiceprintList
                 if item.speaker == speaker for ref in item.references})
    receipt = _voiceprint_registration_receipt(record, reference, refs_count=count, added=True)
    if replace_source_ref is not None:
        receipt["replaced_source_ref"] = replace_source_ref
    return receipt


def unregister_voiceprint(
    session: Session,
    *,
    lease: VideoParseLease,
    source_ref: str,
    removed_at: datetime,
) -> dict[str, Any]:
    parent = _locked_video_lease(session, lease, removed_at)
    detail, phase = _voiceprint_active_preparation(parent, lease)
    chunk_id, separator, _fingerprint = source_ref.partition(":")
    if not separator or chunk_id not in phase["source_chunk_ids"]:
        raise VoiceprintRegistrationRejectedError(
            "INVALID_VOICEPRINT_SOURCE", "Source is outside this preparation.",
            "Use a registered source_ref returned by get_speaker_list.",
        )
    voiceprint = Voiceprint.model_validate(detail["voiceprint"])
    for record in voiceprint.voiceprintList:
        reference = next((ref for ref in record.references
                          if f"{ref.chunk_id}:{ref.event_fingerprint}" == source_ref), None)
        if reference is None:
            continue
        record.references.remove(reference)
        if not record.references:
            voiceprint.voiceprintList.remove(record)
        detail["voiceprint"] = voiceprint.model_dump(mode="json")
        parent.detail = detail
        parent.update_time = datetime_to_unix_time_ms(removed_at)
        task_store.save_task(session, parent)
        count = sum(len(item.references) for item in voiceprint.voiceprintList if item.speaker == record.speaker)
        return {"removed": True, "speaker": record.speaker, "source_ref": source_ref,
                "refs_count": count, "target_refs_count": VOICEPRINT_REFS_PER_SPEAKER}
    return {"removed": False, "speaker": None, "source_ref": source_ref,
            "refs_count": None, "target_refs_count": VOICEPRINT_REFS_PER_SPEAKER}


def get_voiceprint_speaker_list(
    session: Session,
    *,
    lease: VideoParseLease,
    loaded_at: datetime,
) -> dict[str, Any]:
    parent = _locked_video_lease(session, lease, loaded_at)
    detail = _ensure_runtime(parent.detail)
    voiceprint = Voiceprint.model_validate(detail["voiceprint"])
    phase = dict(detail[RUNTIME_DETAIL_KEY].get(_VOICEPRINT_PREPARATION_KEY) or {})
    if not voiceprint.ready:
        detail, phase = _voiceprint_active_preparation(parent, lease)
    references = _valid_voiceprint_references(session, parent, voiceprint, for_update=False)
    speakers = set(phase["speaker_list"])
    progress = _voiceprint_reference_progress(references, speakers)
    registered = {speaker for speaker, refs in progress.items() if refs}
    rows = [(chunk.id, result.content) for _task, chunk, result in chunk_store.list_task_chunk_content_rows(
        session, parent.id, video_id=parent.video_id, chunk_ids=phase["source_chunk_ids"],
    )]
    by_speaker = {speaker: [] for speaker in sorted(speakers)}
    for chunk_id, content in rows:
        for speaker in {event.get("speaker") for event in dict(content or {}).get("timeline", [])
                        if event.get("event_type") == "DIALOGUE"
                        and isinstance(event.get("speaker"), str) and event["speaker"].strip()} & speakers:
            by_speaker[speaker].append(chunk_id)
    return {"speaker_list": [{"speaker": speaker, "chunk_ids": chunk_ids,
                              "refs_count": len(progress[speaker]),
                              "target_refs_count": VOICEPRINT_REFS_PER_SPEAKER,
                              "references": progress[speaker]}
                             for speaker, chunk_ids in by_speaker.items()],
            "speaker_list_size": len(speakers), "registered_speaker_count": len(registered),
            "minimum_speaker_count": len(speakers) // 2 + 1 if speakers else 0,
            "missing_speakers": sorted(speakers - registered)}


def finish_voiceprint_preparation(
    session: Session,
    *,
    lease: VideoParseLease,
    skip_reasons: Mapping[str, str] | None,
    finished_at: datetime,
) -> dict[str, Any]:
    parent = _locked_video_lease(session, lease, finished_at)
    detail = _ensure_runtime(parent.detail)
    voiceprint = Voiceprint.model_validate(detail.get("voiceprint", {}))
    phase = dict(detail[RUNTIME_DETAIL_KEY].get(_VOICEPRINT_PREPARATION_KEY) or {})
    if not voiceprint.ready:
        detail, phase = _voiceprint_active_preparation(parent, lease)
    references = _valid_voiceprint_references(session, parent, voiceprint, for_update=True)
    speakers = set(phase.get("speaker_list", []))
    progress = _voiceprint_reference_progress(references, speakers)
    counts = {speaker: len(refs) for speaker, refs in progress.items()}
    registered = {speaker for speaker, count in counts.items() if count}
    missing = sorted(speakers - registered)
    reasons = {name: reason.strip() for name, reason in dict(skip_reasons or {}).items()
               if name in counts and counts[name] < VOICEPRINT_REFS_PER_SPEAKER
               and isinstance(reason, str) and reason.strip()}
    missing_reasons = sorted(name for name, count in counts.items()
                             if count < VOICEPRINT_REFS_PER_SPEAKER and name not in reasons)
    result = {"registered_speaker_count": len(registered), "speaker_list_size": len(speakers),
              "minimum_speaker_count": len(speakers) // 2 + 1 if speakers else 0,
              "missing_speakers": missing,
              "skip_reasons": reasons, "reference_counts": counts,
              "target_refs_count": VOICEPRINT_REFS_PER_SPEAKER,
              "missing_reason_speakers": [] if voiceprint.ready else missing_reasons}
    if not voiceprint.ready:
        if len(references) != sum(len(record.references) for record in voiceprint.voiceprintList):
            raise VideoParseSnapshotConflictError("Voiceprint registered source identity changed")
        result.update(_voiceprint_consistency(references, speakers))
        if result["suspect_references"]:
            return {"ready": False, **result,
                    "message": "Some references are acoustically inconsistent with their speaker group. "
                               "Review suspect_references and explicitly replace or unregister them, then retry. "
                               "skip_reasons cannot bypass these references; cosine is not identity probability."}
        if speakers and len(registered) * 2 <= len(speakers):
            return {"ready": False, **result,
                    "message": "Register valid references for strictly more than half the speaker list."}
        if missing_reasons:
            return {"ready": False, **result,
                    "message": f"Aim for {VOICEPRINT_REFS_PER_SPEAKER} independent references per speaker, "
                               "or explain each shortfall in skip_reasons."}
    voiceprint.ready = True
    phase["active"] = False
    detail["voiceprint"] = voiceprint.model_dump(mode="json")
    detail[RUNTIME_DETAIL_KEY][_VOICEPRINT_PREPARATION_KEY] = phase
    parent.detail = detail
    parent.update_time = datetime_to_unix_time_ms(finished_at)
    task_store.save_task(session, parent)
    return {"ready": True, **result, "message": "Voiceprint preparation is ready."}


def stop_voiceprint_preparation(
    session: Session,
    *,
    lease: VideoParseLease,
    stopped_at: datetime,
) -> bool:
    parent = _locked_video_lease(session, lease, stopped_at)
    detail = _ensure_runtime(parent.detail)
    phase = dict(detail[RUNTIME_DETAIL_KEY].get(_VOICEPRINT_PREPARATION_KEY) or {})
    if phase.get("lease_version") != lease.lease_version or phase.get("worker_id") != lease.worker_id:
        return False
    phase["active"] = False
    detail[RUNTIME_DETAIL_KEY][_VOICEPRINT_PREPARATION_KEY] = phase
    parent.detail = detail
    task_store.save_task(session, parent)
    return True


def load_voiceprint_references(
    session: Session,
    *,
    lease: IntervalParseLease,
    loaded_at: datetime,
) -> list[dict[str, Any]]:
    interval = _interval_for_lease(session, lease, loaded_at)
    parent = task_store.get_task(session, interval.video_task_id)
    voiceprint = Voiceprint.model_validate(dict(parent.detail or {}).get("voiceprint", {}))
    if not voiceprint.ready:
        return []
    return _valid_voiceprint_references(session, parent, voiceprint, for_update=False)
