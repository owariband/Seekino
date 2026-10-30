"""Bounded, version-pinned retrieval for the local MCP endpoint."""

from __future__ import annotations

import base64
import binascii
import json
from typing import Any, Literal
from urllib.parse import urlencode

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from engine.rag.contracts import VideoChunkEvidence, VideoChunkRetrievalIdentity
from engine.rag.indexing.events import compute_event_id
from engine.rag.retrieval.contracts import EventIndexNotReadyError

from .library import LibraryService
from .media import MediaMissingError, MediaNotReadyError, MediaService
from .video_event_retrieval import VideoEventRetrievalService
from .video_parse_errors import VideoParseConflictError
from .video_parse_persistence import VideoParsePersistenceService


class _EvidenceRef(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal[1] = 1
    identity: VideoChunkRetrievalIdentity
    video_chunk_id: str = Field(min_length=1, max_length=64)
    event_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    timeline_index: int = Field(ge=0)
    offset: int = Field(default=0, ge=0)

    def encode(self) -> str:
        return base64.urlsafe_b64encode(self.model_dump_json().encode()).decode()


class McpRetrievalService:
    """Reuse formal Event retrieval; never accept a caller-selected tenant."""

    def __init__(
        self, *, library: LibraryService, media: MediaService,
        event_retrieval: VideoEventRetrievalService | None,
        parse_persistence: VideoParsePersistenceService, local_tenant_id: str,
    ) -> None:
        if not isinstance(local_tenant_id, str) or not local_tenant_id.strip():
            raise ValueError("local_tenant_id must be nonblank")
        self._library, self._media = library, media
        self._events, self._persistence = event_retrieval, parse_persistence
        self._tenant_id = local_tenant_id

    def list_series(self, offset: int = 0, limit: int = 20) -> dict[str, Any]:
        _integer(offset, "offset", 0, 1_000_000)
        _integer(limit, "limit", 1, 50)
        states = self._library.list_series(tenant_id=self._tenant_id, offset=offset, limit=limit + 1)
        items = [{
            "series_id": state.series_id, "name": (state.display_name or state.name)[:255],
            "status": state.status, "video_count": state.video_count,
            "ready_video_count": state.ready_video_count,
        } for state in states[:limit]]
        return {"items": items, "next_offset": offset + limit if len(states) > limit else None}

    def retrieve(
        self, query: str, series_ids: list[str] | None = None, top_k: int = 5,
        event_types: list[str] | None = None, speaker: str | None = None,
    ) -> dict[str, Any]:
        _integer(top_k, "top_k", 1, 20)
        if not isinstance(query, str) or not query.strip() or len(query) > 4000:
            raise ValueError("INVALID_ARGUMENT: query must contain 1–4000 characters.")
        if self._events is None:
            raise ValueError("EVENT_INDEX_NOT_READY: Event retrieval is not configured; no search was performed.")
        if series_ids is None:
            states = self._library.list_series(tenant_id=self._tenant_id, status="READY", limit=51)
            if len(states) > 50:
                raise ValueError("SERIES_SELECTION_REQUIRED: Use list_series and select at most 50 series.")
            series_ids = [state.series_id for state in states]
            if not series_ids:
                raise ValueError("NO_SEARCHABLE_SERIES: No READY series; use list_series to inspect parsing status.")
        if (not isinstance(series_ids, list) or not 1 <= len(series_ids) <= 50
                or any(not isinstance(value, str) or not 1 <= len(value) <= 64 for value in series_ids)):
            raise ValueError("INVALID_ARGUMENT: series_ids must contain 1–50 IDs.")
        if (event_types is not None and (not isinstance(event_types, list) or len(event_types) > 7
                or any(not isinstance(value, str) for value in event_types))):
            raise ValueError("INVALID_ARGUMENT: event_types must be a list of supported Event types.")
        if speaker is not None and (not isinstance(speaker, str) or not speaker.strip() or len(speaker) > 255):
            raise ValueError("INVALID_ARGUMENT: speaker must contain 1–255 characters.")
        try:
            found = self._events.search(
                query, tenant_id=self._tenant_id, series_ids=series_ids, top_k=top_k,
                event_types=event_types, speaker=speaker,
            )
        except EventIndexNotReadyError as error:
            raise ValueError("EVENT_INDEX_NOT_READY: Event coverage is unavailable or rebuilding; this is not an empty match.") from error
        except ValueError as error:
            raise ValueError("INVALID_ARGUMENT: Check series IDs, Event types and speaker filters.") from error
        result = found.chunk_result
        by_chunk = {hit.evidence.video_chunk_id: hit.evidence for hit in result.hits}
        items, truncated = [], False
        used_bytes = 0
        for match in found.matches:
            evidence = by_chunk[match.video_chunk_id]
            identity = self._identity(evidence)
            if (identity.series_id not in series_ids or identity.parse_task_id != match.parse_task_id
                    or identity.content_version != match.version):
                raise ValueError("EVIDENCE_SCOPE_MISMATCH: Retrieved evidence does not match the requested sources.")
            ref = _EvidenceRef(identity=identity, video_chunk_id=match.video_chunk_id,
                               event_id=match.event_id, timeline_index=match.timeline_index)
            _validate_event_ref(ref, evidence)
            event, clipped = _event(evidence.all_time_events[match.timeline_index])
            card = {
                "evidence_ref": ref.encode(), "series_id": evidence.series_id,
                "series_name": evidence.series_name[:255], "video_id": evidence.video_id,
                "episode_order": evidence.episode_order, "video_chunk_id": evidence.video_chunk_id,
                "event_id": match.event_id, "start_ms": match.start_ms, "end_ms": match.end_ms,
                "event": event, "evidence_truncated": clipped,
                "fused_rank": match.fused_rank, "channel_ranks": match.channel_ranks,
            }
            size = len(json.dumps(card, ensure_ascii=False).encode())
            if len(items) >= 40 or used_bytes + size > 48_000:
                truncated = True
                break
            items.append(card)
            used_bytes += size
            truncated |= clipped
        warnings = [str(value)[:500] for value in result.warnings[:8]]
        if truncated:
            warnings.append("Output was truncated; narrow the query or series scope. read_evidence returns bounded source context.")
        return {
            "query": query.strip(), "granularity": "event", "top_k_unit": "distinct_chunks",
            "requested_top_k": top_k, "matched_chunk_count": len(result.hits),
            "matched_event_count": len(found.matches), "returned_event_count": len(items),
            "series_ids": list(result.series_ids), "partial": result.partial,
            "truncated": truncated, "warnings": warnings, "items": items,
        }

    def read_evidence(self, evidence_ref: str) -> dict[str, Any]:
        try:
            if not isinstance(evidence_ref, str) or not 1 <= len(evidence_ref) <= 4096:
                raise ValueError("invalid reference size")
            raw = base64.b64decode(evidence_ref, altchars=b"-_", validate=True)
            ref = _EvidenceRef.model_validate_json(raw)
        except (ValueError, binascii.Error, ValidationError) as error:
            raise ValueError("INVALID_EVIDENCE_REF: Use an evidence_ref returned by retrieve or read_evidence.") from error
        if ref.identity.tenant_id != self._tenant_id:
            raise ValueError("EVIDENCE_SCOPE_MISMATCH: Evidence is outside this local library.")
        try:
            hydrated = self._persistence.hydrate_retrieval_candidates([(ref.video_chunk_id, ref.identity)])
        except (VideoParseConflictError, ValueError) as error:
            raise ValueError("EVIDENCE_STALE: Source changed or is no longer available; retrieve again.") from error
        if len(hydrated) != 1:
            raise ValueError("EVIDENCE_STALE: Source is no longer available; retrieve again.")
        evidence = hydrated[0].evidence
        if self._identity(evidence) != ref.identity or evidence.video_chunk_id != ref.video_chunk_id:
            raise ValueError("EVIDENCE_STALE: Source identity changed; retrieve again.")
        _validate_event_ref(ref, evidence)
        timeline = evidence.all_time_events
        if ref.offset >= len(timeline):
            raise ValueError("INVALID_EVIDENCE_REF: Context offset is outside this Chunk.")
        matched_event, truncated = _event(timeline[ref.timeline_index])
        events, size, end = [], 0, ref.offset
        for index in range(ref.offset, min(len(timeline), ref.offset + 8)):
            event, clipped = _event(timeline[index])
            cost = len(json.dumps(event, ensure_ascii=False).encode())
            if events and size + cost > 24_000:
                break
            events.append({"timeline_index": index, **event})
            size += cost
            truncated |= clipped
            end = index + 1
        more = end < len(timeline)
        warnings = []
        if truncated:
            warnings.append("Some source text was truncated; verify the original media before relying on omitted details.")
        if more:
            warnings.append("More Chunk context is available through next_evidence_ref.")
        original_url = None
        try:
            self._media.get_original(evidence.video_id, tenant_id=self._tenant_id, video=evidence)
            original_url = "/api/videos/content?" + urlencode({
                "tenantId": self._tenant_id, "videoId": evidence.video_id, "variant": "original",
            })
        except (MediaMissingError, MediaNotReadyError, OSError):
            warnings.append("Original media is unavailable; source text remains readable.")
        playback = self._media.playback_view(evidence.video_id, tenant_id=self._tenant_id, video=evidence)
        if playback.status != "READY":
            warnings.append("Browser playback is unavailable; inspect original_url if present.")
        return {
            "evidence_ref": evidence_ref, "series_id": evidence.series_id,
            "series_name": evidence.series_name[:255], "video_id": evidence.video_id,
            "episode_order": evidence.episode_order, "video_chunk_id": evidence.video_chunk_id,
            "parse_task_id": ref.identity.parse_task_id, "content_version": ref.identity.content_version,
            "index_generation": ref.identity.index_generation, "event_id": ref.event_id,
            "chunk_start_ms": evidence.start_seconds, "chunk_end_ms": evidence.end_seconds,
            "duration_ms": evidence.duration_seconds, "matched_event": matched_event,
            "context_events": events, "context_offset": ref.offset, "context_event_count": len(timeline),
            "next_evidence_ref": ref.model_copy(update={"offset": end}).encode() if more else None,
            "truncated": truncated, "warnings": warnings,
            "media": {"original_url": original_url, "playback_url": playback.url,
                      "playback_status": playback.status},
        }

    def _identity(self, evidence: VideoChunkEvidence) -> VideoChunkRetrievalIdentity:
        if evidence.tenant_id != self._tenant_id:
            raise ValueError("EVIDENCE_SCOPE_MISMATCH: Evidence is outside this local library.")
        provenance = evidence.parse_provenance or {}
        try:
            return VideoChunkRetrievalIdentity(
                tenant_id=self._tenant_id, series_id=evidence.series_id, video_id=evidence.video_id,
                parse_task_id=provenance["parseTaskId"], content_version=provenance["contentVersion"],
                index_generation=provenance["indexGeneration"],
            )
        except (KeyError, ValidationError) as error:
            raise ValueError("EVIDENCE_STALE: Source lacks a formal version identity; retrieve again.") from error


def _integer(value: int, name: str, minimum: int, maximum: int) -> None:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"INVALID_ARGUMENT: {name} must be an integer between {minimum} and {maximum}.")


def _validate_event_ref(ref: _EvidenceRef, evidence: VideoChunkEvidence) -> None:
    identity = ref.identity
    if (ref.timeline_index >= len(evidence.all_time_events)
            or ref.event_id != compute_event_id(identity.parse_task_id, ref.video_chunk_id,
                                               identity.content_version, ref.timeline_index)):
        raise ValueError("INVALID_EVIDENCE_REF: Event identity does not match its source Timeline.")


def _event(source: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Preserve canonical fields, rename millisecond keys and bound large text."""
    truncated = False
    remaining_chars = 2000

    def project(value: Any) -> Any:
        nonlocal truncated, remaining_chars
        if isinstance(value, str):
            limit = min(1600, remaining_chars)
            truncated |= len(value) > limit
            text = value[:limit]
            remaining_chars -= len(text)
            return text
        if isinstance(value, list):
            truncated |= len(value) > 8
            return [project(item) for item in value[:8]]
        if isinstance(value, dict):
            return {key.removesuffix("_seconds") + "_ms" if key.endswith("_seconds") else key: project(item)
                    for key, item in value.items()}
        return value

    # Keep type/timing before potentially long descriptions and name lists.
    ordered = {key: source[key] for key in ("event_type", "start_seconds", "end_seconds") if key in source}
    ordered.update(source)
    return project(ordered), truncated
