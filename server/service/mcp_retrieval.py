"""Ranked retrieval and version-pinned browsing within one MCP library."""

from __future__ import annotations

import json
import unicodedata
from typing import Any
from urllib.parse import urlencode

from engine.common.background import character_name_groups
from engine.parsev2.camera import CameraReference, camera_replay
from engine.rag.contracts import VideoChunkEvidence, VideoChunkRetrievalIdentity
from engine.rag.indexing.events import compute_event_id
from engine.rag.retrieval.contracts import EventIndexNotReadyError, SeriesNotReadyError

from ..constants.media import CLOUD_CLIP_MAX_MS
from ..constants.identity import LOCAL_USER_ID
from ..model.dto.parseContentDTO import PublishedReference
from ..error.auth import AuthError
from ..error.retrieval import SeriesScopeNotFoundError
from ..model.dto.authDTO import AuthIdentity
from ..model.dto.libraryDTO import SeriesState
from ..repository.library import SeriesNotFoundError
from .access import require_tenant_write, series_access
from .pagination import decode_cursor, encode_cursor
from .library import LibraryService
from .media import MediaMissingError, MediaNotReadyError, MediaService, validate_clip_range
from .video_event_retrieval import VideoEventRetrievalService
from .mcp_evidence import (
    REFERENCE_PREFIX, EvidenceRef, decode_reference_evidence, encode_reference_evidence, evidence_clip_url, evidence_identity,
    published_chunk, resolve_reference_evidence,
)
from .video_parse_persistence import VideoParsePersistenceService


_PAGE_BYTES = 48_000
_BROWSE_CHUNKS_PER_PAGE = 8


class McpRetrievalService:
    """Reuse formal Event retrieval; never accept a caller-selected tenant."""

    def __init__(
        self, *, library: LibraryService, media: MediaService,
        event_retrieval: VideoEventRetrievalService | None,
        parse_persistence: VideoParsePersistenceService, local_tenant_id: str, public_tenant_id: str | None = None,
        reference_store=None, editing_repository=None,
    ) -> None:
        if not isinstance(local_tenant_id, str) or not local_tenant_id.strip():
            raise ValueError("local_tenant_id must be nonblank")
        self._library, self._media = library, media
        self._events, self._persistence = event_retrieval, parse_persistence
        self._tenant_id = public_tenant_id if public_tenant_id is not None else local_tenant_id
        self._public = public_tenant_id is not None
        self._reference_store = reference_store
        self._editing = editing_repository

    def _template_series(self, viewer, *, series_id=None, template_id=None, version_id=None, write=False):
        actor = self._public_owner(viewer) if self._public else LOCAL_USER_ID
        if template_id is not None or version_id is not None:
            tenant, bound = self._editing.scope(template_id=template_id, version_id=version_id)
            if series_id is not None and series_id != bound:
                raise ValueError("TEMPLATE_NOT_FOUND")
            series = self._library.get_series(bound, tenant_id=tenant, owner_user_id=actor)
        else:
            if series_id is None:
                raise ValueError("INVALID_ARGUMENT: Select series_id for a new template.")
            series = self._library.resolve_series(series_id=series_id, owner_user_id=actor)
        if self._public:
            series_access(viewer, series)
            if write:
                if not viewer.editing_tenant_id:
                    raise ValueError("PERSONAL_LIBRARY_REQUIRED: Get a write-capable MCP credential.")
                require_tenant_write(viewer, series.tenant_id)
        elif series.tenant_id != self._tenant_id:
            raise ValueError("TEMPLATE_NOT_FOUND")
        return series

    def _template_series_scope(self, series_ids, viewer):
        if series_ids is not None:
            if (not isinstance(series_ids, list) or not 1 <= len(series_ids) <= 32
                    or any(not isinstance(value, str) or not value for value in series_ids)):
                raise ValueError("INVALID_ARGUMENT: Select 1–32 series IDs.")
            return [self._template_series(viewer, series_id=value) for value in dict.fromkeys(series_ids)]
        if self._public:
            actor = self._public_owner(viewer)
            public, _ = self._library.list_public_series_page(tenant_id=None, owner_user_id=actor, limit=51)
            own = (self._library.list_series(tenant_id=viewer.tenant_id, limit=51, owner_user_id=actor)
                   if viewer.tenant_id else [])
            states = list({state.series_id: state for state in [*public, *own]}.values())
        else:
            states = self._library.list_series(tenant_id=self._tenant_id, limit=51)
        if len(states) > 50:
            raise ValueError("SERIES_SELECTION_REQUIRED: Select the series to search.")
        return states

    def find_edit_templates(self, query, *, tags=None, series_ids=None, include_parse_techniques=True,
                            top_k=5, viewer: AuthIdentity | None = None):
        _integer(top_k, "top_k", 1, 8)
        if not isinstance(query, str) or not query.strip() or len(query) > 2000:
            raise ValueError("INVALID_ARGUMENT: Enter a technique query")
        states = self._template_series_scope(series_ids, viewer)
        scopes = [(state.tenant_id, state.series_id) for state in states]
        templates = (self._editing.search(scopes, query=query.strip(), limit=top_k, tags=tags)
                     if self._editing is not None else [])
        for template in templates:
            self._template_series(viewer, template_id=template["template_id"])
        if not include_parse_techniques:
            return {"techniques": [], "templates": templates}
        if self._reference_store is None:
            if self._editing is not None:
                return {"techniques": [], "templates": templates, "warnings": ["REFERENCE_INDEX_UNAVAILABLE"]}
            raise ValueError("REFERENCE_INDEX_UNAVAILABLE: Reference search is not configured")
        groups = {}
        for state in states:
            if not self._public or state.status == "READY":
                groups.setdefault(state.tenant_id, []).append(state.series_id)
        tasks = {}
        for tenant, selected in groups.items():
            tasks.update({task: tenant for task in self._persistence.reference_scope(tenant, selected)})
        hits = self._reference_store.search(query.strip(), tenant_id=None if self._public else self._tenant_id,
                                             task_ids=tasks, size=top_k * 3)
        notes = {}
        for tenant, selected in groups.items():
            candidates = [hit for hit in hits if tasks.get(hit.get("task_id")) == tenant]
            sources = self._persistence.reference_observations(candidates, tenant_id=tenant, series_ids=selected,
                                                               index_name=self._reference_store.index_name)
            notes.update({(source.key.task_id, source.key.chunk_id, source.key.segment_id): source for source in sources})
        techniques = []
        for hit in hits:
            source = notes.get((hit.get("task_id"), hit.get("chunk_id"), hit.get("segment_id")))
            if source is None:
                continue
            self._template_series(viewer, series_id=source.key.series_id)
            card = _reference_card(source)
            card.update(steps=[step.model_dump() for step in source.observation.note.motion.steps],
                        fine_starts_ms=source.observation.segment.fine_starts_ms)
            original = self._reference_original_url(source.key.video_id, tenant_id=source.key.tenant_id)
            card["media"] = _reference_media(original, card["evidence_ref"], card["start_ms"], card["end_ms"], self._public)
            techniques.append(card)
            if len(techniques) == top_k:
                break
        for selected in dict.fromkeys([item["series_id"] for item in [*techniques, *templates]]):
            self._template_series(viewer, series_id=selected)
        return {"techniques": techniques, "templates": templates}

    def _source_snapshot(self, evidence_ref, viewer):
        evidence = self.read_evidence(evidence_ref, viewer=viewer)
        fields = ("evidence_ref", "series_id", "video_id", "parse_task_id", "content_version", "index_generation",
                  "video_chunk_id", "segment_id", "technique_key", "start_ms", "end_ms", "note", "event_id")
        snapshot = {key: evidence[key] for key in fields if key in evidence}
        if "matched_event" in evidence:
            event = evidence["matched_event"]
            snapshot.update(note=event, start_ms=event["start_ms"], end_ms=event["end_ms"])
        key = decode_reference_evidence(evidence_ref) if evidence_ref.startswith(REFERENCE_PREFIX) else EvidenceRef.decode(evidence_ref).identity
        return {"tenant_id": key.tenant_id, **snapshot}

    def _source_status(self, snapshot, viewer):
        if not self._public and self._tenant_id != snapshot["tenant_id"]:
            return "unavailable"
        try:
            series = self._template_series(viewer, series_id=snapshot["series_id"])
            if series.tenant_id != snapshot["tenant_id"]:
                return "unavailable"
            self._library.get_video(snapshot["video_id"], tenant_id=snapshot["tenant_id"])
            self._source_snapshot(snapshot["evidence_ref"], viewer)
        except (SeriesNotFoundError, AuthError, LookupError):
            return "unavailable"
        except ValueError as error:
            if str(error).startswith("EVIDENCE_STALE"):
                return self._persistence.saved_source_status(snapshot)
            if str(error).startswith(("EVIDENCE_SCOPE_MISMATCH", "REFERENCE_INDEX_UNAVAILABLE")):
                return "unavailable"
            raise
        return "current"

    def get_edit_template(self, template_id, version_id=None, *, viewer=None):
        series = self._template_series(viewer, template_id=template_id, version_id=version_id)
        result = self._editing.read(series.tenant_id, series.series_id, template_id, version_id)
        if result.get("version"):
            for index, source in enumerate(result["version"]["sources"]):
                source["status"] = self._source_status(source, viewer)
                if self._public and source["status"] == "unavailable":
                    result["version"]["sources"][index] = {"status": "unavailable"}
        self._template_series(viewer, template_id=template_id)
        return result

    def list_edit_template_versions(self, template_id, *, viewer=None):
        series = self._template_series(viewer, template_id=template_id)
        result = self._editing.versions(series.tenant_id, series.series_id, template_id)
        self._template_series(viewer, template_id=template_id)
        return result

    def save_edit_template_version(self, *, source_refs, series_id=None, viewer=None, **arguments):
        series = self._template_series(viewer, series_id=series_id, template_id=arguments.get("template_id"), write=True)
        content = {name: arguments.pop(name) for name in
                   ("spec", "visual_requirements", "slots", "params_schema", "files", "entry", "runtime")}
        return self._editing.save(series.tenant_id, series.series_id,
                                  author=viewer.user_id if self._public else LOCAL_USER_ID,
                                  content=content, source_refs=source_refs,
                                  resolve_source=lambda ref: self._source_snapshot(ref, viewer), **arguments)

    def append_edit_template_note(self, *, viewer=None, **arguments):
        series = self._template_series(viewer, version_id=arguments["version_id"], write=True)
        return self._editing.append(series.tenant_id, series.series_id,
                                    author=viewer.user_id if self._public else LOCAL_USER_ID, **arguments)

    def set_default_edit_template_version(self, *, viewer=None, **arguments):
        series = self._template_series(viewer, template_id=arguments["template_id"], write=True)
        return self._editing.set_default(series.tenant_id, series.series_id,
                                         author=viewer.user_id if self._public else LOCAL_USER_ID, **arguments)

    def _reference_original_url(self, video_id, *, tenant_id=None):
        tenant_id = tenant_id or self._tenant_id
        try:
            self._media.get_original(video_id, tenant_id=tenant_id)
        except (MediaMissingError, MediaNotReadyError, OSError):
            return None
        return "/api/videos/content?" + urlencode({"tenantId": tenant_id, "videoId": video_id, "variant": "original"})

    def get_style_card(self, video_id, *, viewer: AuthIdentity | None = None):
        if self._reference_store is None:
            raise ValueError("REFERENCE_INDEX_UNAVAILABLE: Reference search is not configured")
        tenant_id = self._tenant_id
        if self._public:
            series = self._library.resolve_series(video_id=video_id, owner_user_id=self._public_owner(viewer))
            self._template_series(viewer, series_id=series.series_id)
            tenant_id = series.tenant_id
        task_id, card, sources = self._persistence.reference_style_card(
            tenant_id=tenant_id, video_id=video_id, index_name=self._reference_store.index_name,
        )
        exemplars = [_reference_card(source) for source in sources]
        style = card.model_dump()
        if "signals" in style:
            refs = {item["segment_id"]: item["evidence_ref"] for item in exemplars}
            for signal in style["signals"]:
                for example in signal["examples"]:
                    example["evidence_ref"] = refs[example["segment_id"]]
        if self._public:
            self._template_series(viewer, series_id=series.series_id)
        return {"video_id": video_id, "parse_task_id": task_id, "style_card": style, "exemplars": exemplars,
                "style_card_format": "anchored_signals" if "signals" in style else "legacy_technique_groups"}

    def _public_owner(self, viewer: AuthIdentity | None) -> str:
        if viewer is None or viewer.mode != "cloud" or viewer.public_tenant_id != self._tenant_id:
            raise ValueError("EVIDENCE_SCOPE_MISMATCH: Missing Cloud library identity.")
        return viewer.user_id

    def _require_series(self, series_ids: list[str], viewer: AuthIdentity | None) -> None:
        if self._public:
            owner = self._public_owner(viewer)
            for series_id in dict.fromkeys(series_ids):
                self._library.require_public_ready_series(series_id, tenant_id=self._tenant_id, owner_user_id=owner)

    def _series_page(self, offset: int, limit: int, viewer: AuthIdentity | None, *, ready_only: bool = False) -> list[SeriesState]:
        if self._public:
            states, _ = self._library.list_public_series_page(
                tenant_id=self._tenant_id, owner_user_id=self._public_owner(viewer), status="READY",
                offset=offset, limit=limit,
            )
            return states
        return self._library.list_series(tenant_id=self._tenant_id, offset=offset, limit=limit,
                                         **({"status": "READY"} if ready_only else {}))

    def list_series(self, offset: int = 0, limit: int = 20, *, viewer: AuthIdentity | None = None) -> dict[str, Any]:
        _integer(offset, "offset", 0, 1_000_000)
        _integer(limit, "limit", 1, 50)
        states = self._series_page(offset, limit + 1, viewer)
        items = [{
            "series_id": state.series_id, "name": (state.display_name or state.name)[:255],
            "status": state.status, "video_count": state.video_count,
            "ready_video_count": state.ready_video_count,
        } for state in states[:limit]]
        return {"items": items, "next_offset": offset + limit if len(states) > limit else None}

    def retrieve(
        self, query: str, series_ids: list[str] | None = None, top_k: int = 5,
        event_types: list[str] | None = None, speaker: str | None = None, cursor: str | None = None,
        *, viewer: AuthIdentity | None = None,
    ) -> dict[str, Any]:
        _integer(top_k, "top_k", 1, 20)
        if not isinstance(query, str) or not query.strip() or len(query) > 4000:
            raise ValueError("INVALID_ARGUMENT: query must contain 1–4000 characters.")
        if self._events is None:
            raise ValueError("EVENT_INDEX_NOT_READY: Event retrieval is not configured; no search was performed.")
        if series_ids is None:
            states = self._series_page(0, 51, viewer, ready_only=True)
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
        self._require_series(series_ids, viewer)
        try:
            found = self._events.search(
                query, tenant_id=self._tenant_id, series_ids=series_ids, top_k=top_k,
                event_types=event_types, speaker=speaker,
            )
        except (EventIndexNotReadyError, SeriesNotReadyError) as error:
            raise ValueError("EVENT_INDEX_NOT_READY: Event coverage is unavailable or rebuilding; this is not an empty match.") from error
        except ValueError as error:
            raise ValueError("INVALID_ARGUMENT: Check series IDs, Event types and speaker filters.") from error
        result = found.chunk_result
        if self._public:
            if not set(result.series_ids).issubset(series_ids):
                raise SeriesNotFoundError("检索结果超出所选公开系列")
            self._require_series(series_ids, viewer)
        by_chunk = {hit.evidence.video_chunk_id: hit.evidence for hit in result.hits}
        items, truncated = [], False
        used_bytes = 0
        page_scope = {"tool": "retrieve", "tenant": self._tenant_id, "series": series_ids,
                      "query": query.strip(), "top_k": top_k, "types": event_types, "speaker": speaker,
                      "events": [match.event_id for match in found.matches]}
        offset = _page_offset(cursor, page_scope, len(found.matches))
        for match in found.matches[offset:]:
            evidence = by_chunk[match.video_chunk_id]
            identity = evidence_identity(evidence, self._tenant_id)
            if (identity.series_id not in series_ids or identity.parse_task_id != match.parse_task_id
                    or identity.content_version != match.version):
                raise ValueError("EVIDENCE_SCOPE_MISMATCH: Retrieved evidence does not match the requested sources.")
            card = _event_card(evidence, identity, match.timeline_index, match.event_id)
            card.update(fused_rank=match.fused_rank, channel_ranks=match.channel_ranks)
            size = len(json.dumps(card, ensure_ascii=False).encode())
            if len(items) >= 40 or used_bytes + size > _PAGE_BYTES:
                truncated = True
                break
            items.append(card)
            used_bytes += size
            truncated |= card["evidence_truncated"]
        next_offset = offset + len(items)
        more = next_offset < len(found.matches)
        if self._public:
            warnings = ["Results cover only part of the requested footage."] if result.partial else []
        else:
            warnings = [str(value)[:500] for value in result.warnings[:8]]
        if truncated:
            warnings.append("Inspect source media for abbreviated details. Follow next_cursor for more ranked results when present.")
        return {
            "query": query.strip(), "granularity": "event", "top_k_unit": "distinct_chunks",
            "requested_top_k": top_k, "matched_chunk_count": len(result.hits),
            "matched_event_count": len(found.matches), "returned_event_count": len(items),
            "series_ids": list(result.series_ids), "partial": result.partial,
            "truncated": truncated, "warnings": warnings, "items": items,
            "next_cursor": encode_cursor(next_offset, scope=page_scope) if more else None,
        }

    def browse_events(
        self, series_id: str, visible_names: list[str] | None = None,
        limit: int = 24, cursor: str | None = None,
        *, viewer: AuthIdentity | None = None,
    ) -> dict[str, Any]:
        """Walk every published Timeline Event in episode order, without a similarity cutoff."""
        _integer(limit, "limit", 1, 50)
        self._require_series([series_id], viewer)
        if visible_names is not None and (
            not isinstance(visible_names, list) or not 1 <= len(visible_names) <= 32
            or any(not isinstance(n, str) or not n.strip() or len(n) > 255 for n in visible_names)
        ):
            raise ValueError("INVALID_ARGUMENT: Supply one or more visible names or omit the filter.")
        try:
            chunks = self._persistence.list_published_timeline_chunks(series_id=series_id, tenant_id=self._tenant_id)
        except (SeriesScopeNotFoundError, ValueError) as error:
            raise ValueError("INVALID_ARGUMENT: Select an accessible series from list_series.") from error
        resolved_names = set(visible_names or ())
        ambiguous = set()
        if visible_names:
            series = self._library.get_series(series_id, tenant_id=self._tenant_id)
            groups = character_name_groups(series.character_bible, series.important_extra_data)
            for requested in visible_names:
                name = _normalized_name(requested)
                matches = [group for group in groups if any(_normalized_name(n) == name for n in group)]
                if len(matches) == 1:
                    resolved_names.update(matches[0])
                elif len(matches) > 1:
                    ambiguous.add(requested)
        names = {_normalized_name(n) for n in resolved_names}
        scope = {"tool": "browse_events", "tenant": self._tenant_id, "series": series_id, "names": sorted(names),
                 "chunks": [(c.video_id, c.parse_task_id, c.video_chunk_id, c.content_version,
                             c.index_generation, c.event_count) for c in chunks]}
        total = sum(c.event_count for c in chunks)
        position = _page_offset(cursor, scope, total)
        items, used_bytes, scanned = [], 0, 0
        chunk_end = 0
        for chunk in chunks:
            chunk_start = chunk_end
            chunk_end += chunk.event_count
            if position >= chunk_end:
                continue
            if scanned == _BROWSE_CHUNKS_PER_PAGE or len(items) == limit:
                break
            identity = VideoChunkRetrievalIdentity(
                tenant_id=self._tenant_id, series_id=series_id, video_id=chunk.video_id,
                parse_task_id=chunk.parse_task_id, content_version=chunk.content_version,
                index_generation=chunk.index_generation,
            )
            evidence = published_chunk(self._persistence, chunk.video_chunk_id, identity, tenant_id=self._tenant_id)
            if len(evidence.all_time_events) != chunk.event_count:
                raise ValueError("EVIDENCE_STALE: The Timeline changed during browsing.")
            scanned += 1
            for index in range(position - chunk_start, chunk.event_count):
                event = evidence.all_time_events[index]
                if not names or any(_normalized_name(n) in names for n in event["visible_persons"]):
                    event_id = compute_event_id(identity.parse_task_id, chunk.video_chunk_id, identity.content_version, index)
                    card = _event_card(evidence, identity, index, event_id)
                    size = len(json.dumps(card, ensure_ascii=False).encode())
                    if items and (len(items) == limit or used_bytes + size > _PAGE_BYTES):
                        break
                    items.append(card)
                    used_bytes += size
                position += 1
            if position < chunk_end:
                break
        self._require_series([series_id], viewer)
        return {"series_id": series_id, "published_event_count": total, "returned_event_count": len(items),
                "resolved_visible_names": sorted(resolved_names)[:32], "resolved_name_count": len(resolved_names),
                "ambiguous_names": sorted(ambiguous),
                "items": items, "next_cursor": encode_cursor(position, scope=scope) if position < total else None}

    def read_evidence(
        self, evidence_ref: str, *, start_ms: int | None = None, end_ms: int | None = None, cloud: bool = False,
        viewer: AuthIdentity | None = None,
    ) -> dict[str, Any]:
        if isinstance(evidence_ref, str) and evidence_ref.startswith(REFERENCE_PREFIX):
            if self._reference_store is None:
                raise ValueError("REFERENCE_INDEX_UNAVAILABLE: Reference search is not configured")
            key = decode_reference_evidence(evidence_ref)
            tenant_id = self._tenant_id
            if self._public:
                series = self._template_series(viewer, series_id=key.series_id)
                if series.tenant_id != key.tenant_id:
                    raise ValueError("EVIDENCE_SCOPE_MISMATCH")
                tenant_id = series.tenant_id
            source = resolve_reference_evidence(key, self._persistence, tenant_id=tenant_id,
                                                index_name=self._reference_store.index_name)
            segment = source.observation.segment
            if start_ms is None and end_ms is None:
                start_ms, end_ms = segment.start_ms, segment.end_ms
                if self._public or cloud:
                    end_ms = min(end_ms, start_ms + CLOUD_CLIP_MAX_MS)
            validate_clip_range(start_ms, end_ms, duration_ms=source.duration_ms, cloud=self._public or cloud)
            original_url = self._reference_original_url(source.key.video_id, tenant_id=tenant_id)
            if self._public:
                self._template_series(viewer, series_id=key.series_id)
            return {**_reference_card(source), "duration_ms": source.duration_ms, "kind": "edit_reference",
                    "note": source.observation.note.model_dump(), "measured": segment.model_dump(),
                    "media": _reference_media(original_url, evidence_ref, start_ms, end_ms, self._public or cloud),
                    "warnings": [] if original_url else ["Original media is unavailable."]}
        ref = EvidenceRef.decode(evidence_ref)
        cloud = self._public or cloud
        tenant_id = self._tenant_id
        if self._public:
            series = self._template_series(viewer, series_id=ref.identity.series_id)
            if series.tenant_id != ref.identity.tenant_id:
                raise ValueError("EVIDENCE_SCOPE_MISMATCH")
            tenant_id = series.tenant_id
        evidence = ref.resolve(self._persistence, tenant_id=tenant_id)
        clip_requested = start_ms is not None or end_ms is not None
        if clip_requested:
            validate_clip_range(start_ms, end_ms, duration_ms=evidence.duration_seconds, cloud=cloud)
        timeline = evidence.all_time_events
        source_event = timeline[ref.timeline_index]
        matched_event, truncated = _event(source_event)
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
        url = None
        if clip_requested or not cloud:
            try:
                self._media.get_original(evidence.video_id, tenant_id=tenant_id, video=evidence)
                if clip_requested:
                    url = evidence_clip_url(evidence_ref, start_ms, end_ms)
                else:
                    url = "/api/videos/content?" + urlencode({
                        "tenantId": tenant_id, "videoId": evidence.video_id, "variant": "original",
                    })
            except (MediaMissingError, MediaNotReadyError, OSError):
                warnings.append("Original media is unavailable; source text remains readable.")
        if clip_requested:
            media = {"clip_url": url, "start_ms": start_ms, "end_ms": end_ms, "mime_type": "video/mp4"}
        elif cloud:
            media = {"max_clip_ms": CLOUD_CLIP_MAX_MS}
        else:
            playback = self._media.playback_view(evidence.video_id, tenant_id=tenant_id, video=evidence)
            if playback.status != "READY":
                warnings.append("Browser playback is unavailable; inspect original_url if present.")
            media = {"original_url": url, "playback_url": playback.url, "playback_status": playback.status}
        result = {
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
            "media": media,
        }
        if source_event["event_type"] == "SCENE" and source_event.get("camera_ref") is not None:
            observation = CameraReference.model_validate(source_event["camera_ref"])
            result["camera_replay"] = camera_replay(observation, matched_event["start_ms"], matched_event["end_ms"],
                                                    evidence.camera_hints)
        if self._public:
            self._template_series(viewer, series_id=evidence.series_id)
        return result


def _reference_card(source: PublishedReference) -> dict[str, Any]:
    key, obs = source.key, source.observation
    return {"evidence_ref": encode_reference_evidence(key), "series_id": key.series_id, "video_id": key.video_id,
            "source_filename": source.source_filename, "parse_task_id": key.task_id, "video_chunk_id": key.chunk_id,
            "content_version": key.version, "index_generation": key.generation, "segment_id": key.segment_id,
            "start_ms": obs.segment.start_ms, "end_ms": obs.segment.end_ms, "technique_key": obs.technique_key,
            "name": obs.note.name, "summary": obs.note.summary, "palette": [p.model_dump() for p in obs.segment.palette]}


def _reference_media(original_url, evidence_ref, start_ms, end_ms, cloud):
    if cloud:
        end_ms = min(end_ms, start_ms + CLOUD_CLIP_MAX_MS)
    return {"original_url": None if cloud else original_url,
            "clip_url": evidence_clip_url(evidence_ref, start_ms, end_ms) if original_url else None,
            "start_ms": start_ms, "end_ms": end_ms, "mime_type": "video/mp4"}


def _normalized_name(name: str) -> str:
    return unicodedata.normalize("NFKC", name).strip().casefold()


def _page_offset(cursor: str | None, scope: dict, total: int) -> int:
    try:
        offset = decode_cursor(cursor, scope=scope)
        if offset > total:
            raise ValueError
    except (ValueError, AttributeError) as error:
        raise ValueError("INVALID_CURSOR: Restart browsing with the same current scope and filters.") from error
    return offset


def _event_card(evidence: VideoChunkEvidence, identity: VideoChunkRetrievalIdentity,
                index: int, event_id: str) -> dict[str, Any]:
    ref = EvidenceRef(identity=identity, video_chunk_id=evidence.video_chunk_id,
                       event_id=event_id, timeline_index=index)
    ref.validate_event(evidence)
    event, clipped = _event(evidence.all_time_events[index])
    return {
        "evidence_ref": ref.encode(), "series_id": identity.series_id, "series_name": evidence.series_name[:255],
        "video_id": identity.video_id, "episode_order": evidence.episode_order,
        "video_chunk_id": evidence.video_chunk_id, "event_id": event_id,
        "start_ms": event["start_ms"], "end_ms": event["end_ms"], "event": event, "evidence_truncated": clipped,
    }


def _integer(value: int, name: str, minimum: int, maximum: int) -> None:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"INVALID_ARGUMENT: {name} must be an integer between {minimum} and {maximum}.")


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
    ordered = {key: source[key] for key in ("event_type", "start_seconds", "end_seconds", "camera_ref") if key in source}
    ordered.update(source)
    return project(ordered), truncated
