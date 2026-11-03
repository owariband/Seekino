"""Video ASR identity, validated persistence and lease-fenced recovery."""
from __future__ import annotations

import hashlib
import json
import logging
from copy import deepcopy
from datetime import datetime
from functools import lru_cache
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from sqlalchemy.orm import Session

from engine.common.transcription import TranscriptAnalysis
from engine.parsev2.diarization import DiarizationResult
from ..repository import video_parse_content as result_store
from ..repository import video_parse_task as task_store
from ..utils.time import datetime_to_unix_time_ms
from .parse_control import is_paused
from .video_parse_errors import VideoParseConflictError, VideoParseLeaseLostError
from .video_parse_task import _locked_video_lease, _requires_video_asr, _video_failure_status

logger = logging.getLogger(__name__)
ASR_SCHEMA_VERSION = 1


def json_digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


@lru_cache(maxsize=16)
def _weight_digest(root: str, manifest: tuple[tuple[str, int, int, int], ...]) -> str:
    hashes = []
    for name, size, modified, changed in manifest:
        path = Path(root) / name
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
                digest.update(block)
        stat = path.stat()
        if (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns) != (size, modified, changed):
            raise VideoParseConflictError("ASR model files changed while computing their identity")
        hashes.append([name, size, digest.hexdigest()])
    return json_digest(hashes)


def _local_weights(path_value: str) -> str:
    root = Path(path_value).expanduser().resolve(strict=True)
    if root.is_file():
        files, root = [root], root.parent
    else:
        files = sorted(path for path in root.rglob("*")
                       if path.is_file() and not any(part.startswith(".") for part in path.relative_to(root).parts))
    if not files:
        raise ValueError("ASR model directory is empty")
    manifest = []
    for path in files:
        stat = path.stat()
        manifest.append((str(path.relative_to(root)), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns))
    return _weight_digest(str(root), tuple(manifest))


def build_asr_identity(inputs, model, source) -> dict:
    """Hash effective configuration without persisting credentials or loading a model."""
    configuration = source.model_dump(mode="json", exclude={"api_key_env"})
    # The endpoint may include private routing parameters; retain only its digest.
    if "base_url" in configuration:
        configuration["base_url"] = json_digest(configuration["base_url"])
    producer = {"source_configuration": configuration}
    if source.adapter == "whisperx":
        producer["weights"] = _local_weights(model.model)
        languages = [source.language] if source.language else sorted(source.alignment_models)
        producer["alignment_weights"] = {language: _local_weights(source.alignment_models[language])
                                         for language in languages}
        producer["packages"] = {}
        for package in ("whisperx", "faster-whisper", "ctranslate2", "torch"):
            try:
                producer["packages"][package] = version(package)
            except PackageNotFoundError:
                producer["packages"][package] = None
    elif source.adapter != "openai_audio":
        raise ValueError("whole-video ASR persistence requires an audio provider")
    return {
        "schema_version": ASR_SCHEMA_VERSION,
        "media_sha256": inputs.content_hash, "duration_ms": inputs.duration_seconds,
        "size_bytes": inputs.total_bytes,
        "model_binding": model.model_dump(mode="json", exclude_none=True),
        "producer": producer,
    }


def validate_asr(payload: dict, duration_ms: int) -> dict:
    analysis = TranscriptAnalysis.model_validate(payload, strict=True)
    previous = -1
    for cue in analysis.transcript:
        if not previous <= cue.start_seconds < cue.end_seconds <= duration_ms:
            raise ValueError("persisted ASR cues must be ordered and inside the original video")
        previous = cue.start_seconds
        for word in cue.words or []:
            if word.start_ms is not None and not cue.start_seconds <= word.start_ms < word.end_ms <= cue.end_seconds:
                raise ValueError("persisted ASR word is outside its cue")
    return analysis.model_dump(mode="json")


def _current(session: Session, lease, identity: dict, now: datetime, *, field: str = "asr"):
    task = _locked_video_lease(session, lease, now)
    if (task.is_deleted or is_paused(task) or _video_failure_status(task) is not None
            or task.status not in {"PREPARING", "PARSING"}
            or task_store.latest_video_task_id(session, task.video_id) != task.id):
        raise VideoParseLeaseLostError("Video ASR execution is no longer current")
    if not _requires_video_asr(task):
        raise VideoParseConflictError("task does not use whole-video ASR")
    video = task_store.get_video(session, task.video_id)
    if (video is None or identity.get("schema_version") != ASR_SCHEMA_VERSION
            or identity.get("media_sha256") != video.hash
            or identity.get("duration_ms") != video.duration_ms
            or identity.get("size_bytes") != video.size_bytes):
        raise VideoParseConflictError("ASR input does not match the task's video")
    # Compare the frozen binding through its normal parser, including default scope.
    from agent.video_parse_v2.input import ParseConfig
    model = getattr(ParseConfig.model_validate(task.parse_config).models, field)
    if model is None or identity.get("model_binding") != model.model_dump(mode="json", exclude_none=True):
        raise VideoParseConflictError("ASR model does not match the task's frozen binding")
    return task


def build_diarization_identity(inputs, model, source) -> dict:
    if source.adapter != "pyannote":
        raise ValueError("diarization requires a pyannote source")
    return {
        "schema_version": 1, "media_sha256": inputs.content_hash,
        "duration_ms": inputs.duration_seconds, "size_bytes": inputs.total_bytes,
        "model_binding": model.model_dump(mode="json", exclude_none=True),
        "producer": {"source_configuration": source.model_dump(mode="json"),
                     "weights": _local_weights(model.model),
                     "packages": {name: version(name) for name in ("pyannote.audio", "torch")}},
    }


def validate_diarization(payload: dict, duration_ms: int) -> dict:
    return DiarizationResult.model_validate(payload, strict=True).validate_duration(duration_ms).model_dump()


def _load_or_begin_audio(session, lease, identity, *, field, validator, now):
    task = _current(session, lease, identity, now, field=field)
    metadata = result_store.get_metadata(session, task.id, for_update=True)
    detail = deepcopy(metadata.detail if metadata else {})
    saved = dict(detail.get(field) or {})
    fingerprint = json_digest(identity)
    if saved and saved.get("input_fingerprint") != fingerprint:
        raise VideoParseConflictError("ASR input or producer changed; create a new parse version")
    payload = result_store.load_result(session, task.id, field) if metadata else None
    if payload is not None:
        try:
            value = validator(payload, identity["duration_ms"])
            if saved.get("result_sha256") != json_digest(value):
                raise ValueError("persisted audio result checksum mismatch")
            logger.info("video_%s_cache_hit task=%s", field, task.id)
            return value
        except (ValueError, TypeError) as error:
            logger.warning("video_%s_cache_invalid task=%s reason=%s", field, task.id, type(error).__name__)
    if not saved or payload is not None or saved.get("result_sha256"):
        detail[field] = {"input_fingerprint": fingerprint, "input": deepcopy(identity),
                         "schema_version": identity["schema_version"]}
        result_store.save_result(session, task.id, field=field, result=None, detail=detail,
                                 now_ms=datetime_to_unix_time_ms(now))
    # Reparse changes the task, not necessarily the media/model. Only reuse a
    # validated immutable result owned by this same Video, under the new lease.
    for candidate in result_store.previous_audio_metadata(session, task.video_id, task.id):
        prior = (candidate.detail or {}).get(field) or {}
        if prior.get("input_fingerprint") != fingerprint or not prior.get("result_sha256"):
            continue
        try:
            value = validator(result_store.load_result(session, candidate.video_task_id, field), identity["duration_ms"])
            if json_digest(value) != prior["result_sha256"]:
                continue
        except (ValueError, TypeError):
            continue
        _commit_audio(session, lease, identity, value, field=field, validator=validator, now=now,
                      reused_from_task_id=candidate.video_task_id)
        logger.info("video_%s_cache_reused task=%s source_task=%s", field, task.id, candidate.video_task_id)
        return value
    logger.info("video_%s_cache_miss task=%s", field, task.id)
    return None


def _commit_audio(session, lease, identity, payload, *, field, validator, now, reused_from_task_id=None):
    task = _current(session, lease, identity, now, field=field)
    metadata = result_store.get_metadata(session, task.id, for_update=True)
    detail = deepcopy(metadata.detail if metadata else {})
    saved = dict(detail.get(field) or {})
    if saved.get("input_fingerprint") != json_digest(identity):
        raise VideoParseConflictError("ASR input was not frozen before execution")
    value = validator(payload, identity["duration_ms"])
    checksum = json_digest(value)
    existing = result_store.load_result(session, task.id, field)
    if existing is not None:
        if saved.get("result_sha256") == checksum and json_digest(existing) == checksum:
            return
        raise VideoParseConflictError("completed ASR cannot be replaced by a late result")
    detail[field] = {**saved, "result_sha256": checksum,
                     "committed_lease_version": lease.lease_version,
                     "completed_at_ms": datetime_to_unix_time_ms(now)}
    if reused_from_task_id is not None:
        detail[field]["reused_from_task_id"] = reused_from_task_id
    result_store.save_result(session, task.id, field=field, result=value, detail=detail,
                             now_ms=datetime_to_unix_time_ms(now))


def load_or_begin_asr(session: Session, lease, identity: dict, *, now: datetime) -> dict | None:
    return _load_or_begin_audio(session, lease, identity, field="asr", validator=validate_asr, now=now)


def commit_asr(session: Session, lease, identity: dict, payload: dict, *, now: datetime) -> None:
    _commit_audio(session, lease, identity, payload, field="asr", validator=validate_asr, now=now)


def load_or_begin_diarization(session: Session, lease, identity: dict, *, now: datetime) -> dict | None:
    return _load_or_begin_audio(session, lease, identity, field="diarization", validator=validate_diarization, now=now)


def commit_diarization(session: Session, lease, identity: dict, payload: dict, *, now: datetime) -> None:
    _commit_audio(session, lease, identity, payload, field="diarization", validator=validate_diarization, now=now)
