from __future__ import annotations

from copy import deepcopy
import json
from typing import Any
from uuid import uuid4

from pydantic import ValidationError

from engine.rag.contracts import VideoChunkSearchHit, VideoChunkSearchResult

from agent.utils.context_canvas import ContextCanvas
from agent.utils.errors import AgentToolFeedbackError

from ..common.errors import AgentModelInvocationError, UnknownToolItemError
from .errors import InvalidDraftRangeError
from .state import ChatRenderDraft, ChatVideoBlock, DraftItem, MarkdownRenderBlock


class VideoSearchContextCanvas(ContextCanvas):
    """Per-Kick evidence, draft, approval and rendering in the shared Canvas."""

    read_collections = {
        "query": ("retrieval", "draft", "history", "frame_inspections"),
        "evaluator": ("retrieval", "draft", "history", "frame_inspections"),
        "chat": ("approved", "render"),
        "supervisor": (
            "retrieval", "draft", "approved", "render", "review", "history", "frame_inspections",
        ),
    }

    def __init__(self, tenant_id: str, series_ids: tuple[str, ...]) -> None:
        if not tenant_id.strip():
            raise ValueError("tenant_id must not be empty")
        if not series_ids or any(not value.strip() for value in series_ids):
            raise ValueError("series_ids must contain nonempty values")
        if len(series_ids) != len(set(series_ids)):
            raise ValueError("series_ids must not contain duplicates")
        super().__init__(scope=f"video-search:{tenant_id}:{','.join(series_ids)}")
        self.tenant_id = tenant_id
        self.series_ids = series_ids
        self._markdown_start_revision = -1
        self._markdown_end_revision = -1

    def record_retrieval(self, result: VideoChunkSearchResult) -> None:
        if set(result.series_ids) != set(self.series_ids):
            raise AgentModelInvocationError("Retrieval changed the fixed Series scope")
        incoming: dict[str, dict[str, Any]] = {}
        for hit in result.hits:
            evidence = hit.evidence
            reference = hit.candidate.video_chunk_id
            if evidence.video_chunk_id != reference:
                raise AgentModelInvocationError("Retrieved Candidate and Evidence IDs do not match")
            if reference in incoming:
                raise AgentModelInvocationError("Retrieval returned duplicate VideoChunk IDs")
            if evidence.tenant_id != self.tenant_id or evidence.series_id not in self.series_ids:
                raise AgentModelInvocationError("Retrieved Evidence is outside the fixed scope")
            incoming[reference] = hit.model_dump(mode="json")

        def register(draft: dict) -> None:
            retrieval = draft.setdefault("retrieval", {})
            evidence = draft.setdefault("evidence", {})
            for reference, value in incoming.items():
                evidence.setdefault(reference, deepcopy(value))
                retrieval[reference] = value

        self.mutate(register, readonly_collections=("evidence",))

    def _hit(self, collection: str, reference: str) -> VideoChunkSearchHit:
        if reference not in dict(self.items(collection)):
            raise UnknownToolItemError(
                f"Unknown retrieved video_chunk_id {reference!r}",
                "Call retrieve to obtain a valid video_chunk_id, then retry.",
            )
        return VideoChunkSearchHit.model_validate(self.get(collection, reference))

    def retrieval_item(self, video_chunk_id: str) -> VideoChunkSearchHit:
        return self._hit("retrieval", video_chunk_id)

    def retrieval_item_ids(self) -> tuple[str, ...]:
        return tuple(reference for reference, _value in self.items("retrieval"))

    def retrieval_snapshot(self, video_chunk_id: str) -> VideoChunkSearchHit:
        return self._hit("evidence", video_chunk_id)

    @staticmethod
    def _upsert_draft(draft: dict, reference: str, fields: dict) -> tuple[str, int]:
        evidence_hit = draft.get("evidence", {}).get(reference)
        if evidence_hit is None:
            raise UnknownToolItemError(
                "Draft Item must reference retrieved Evidence",
                "Call retrieve and use one of its video_chunk_id values, then retry.",
            )
        items = draft.setdefault("draft", {})
        existing = items.get(reference)
        value = {**(existing or {}), "video_chunk_id": reference, **fields}
        evidence = VideoChunkSearchHit.model_validate(evidence_hit).evidence
        start, end = value.get("start_seconds"), value.get("end_seconds")
        if type(start) is int and type(end) is int and not (
            evidence.start_seconds <= start < end <= evidence.end_seconds
        ):
            raise InvalidDraftRangeError(
                video_chunk_id=reference, start_seconds=start, end_seconds=end,
                evidence_start_seconds=evidence.start_seconds,
                evidence_end_seconds=evidence.end_seconds,
            )
        item = DraftItem.model_validate(value)
        value = item.model_dump(mode="json")
        if existing == value:
            return "already_present", len(items)
        items[reference] = value
        return ("inserted" if existing is None else "updated"), len(items)

    def upsert_draft_item(self, *, video_chunk_id: str, start_seconds: int,
                          end_seconds: int, claim: str) -> tuple[str, int]:
        return self.mutate(lambda draft: self._upsert_draft(draft, video_chunk_id, {
            "start_seconds": start_seconds, "end_seconds": end_seconds, "claim": claim,
        }))

    def remove_draft_item(self, video_chunk_id: str) -> tuple[bool, int]:
        def remove(draft: dict) -> tuple[bool, int]:
            items = draft.setdefault("draft", {})
            return items.pop(video_chunk_id, None) is not None, len(items)
        return self.mutate(remove)

    def draft_item_count(self) -> int:
        return len(self.items("draft"))

    def draft_items(self, *, approved: bool = False) -> list[DraftItem]:
        collection = "approved" if approved else "draft"
        if approved:
            self._require_approval()
        return [DraftItem.model_validate(value) for _reference, value in self.items(collection)]

    def draft_item(self, video_chunk_id: str, *, approved: bool = False) -> DraftItem:
        collection = "approved" if approved else "draft"
        if approved:
            self._require_approval()
        if video_chunk_id not in dict(self.items(collection)):
            label = "approved Draft" if approved else "Draft"
            raise UnknownToolItemError(
                f"Unknown {label} video_chunk_id {video_chunk_id!r}",
                "Read the Draft catalog and use one of its video_chunk_id values.",
            )
        return DraftItem.model_validate(self.get(collection, video_chunk_id))

    def _require_approval(self) -> None:
        if not self.items("approval"):
            raise AgentModelInvocationError("Draft has not been approved")

    def approve_draft(self, *, expected_revision: int | None = None) -> None:
        with self._lock:
            if expected_revision is not None and self.revision != expected_revision:
                raise AgentToolFeedbackError(
                    "CANVAS_CONFLICT", "Draft changed during approval.",
                    "Repeat the Evaluator audit for the current Canvas revision.",
                )

            def approve(draft: dict) -> None:
                if draft.get("approval"):
                    raise AgentModelInvocationError("Draft has already been approved")
                draft["approved"] = deepcopy(draft.get("draft", {}))
                draft["approved_evidence"] = {
                    reference: deepcopy(draft["evidence"][reference])
                    for reference in draft["approved"]
                }
                draft["approval"] = {"snapshot": {"input_revision": self.revision}}

            self.mutate(approve, readonly_collections=("approved", "approved_evidence", "approval"))

    def approved_hit(self, video_chunk_id: str) -> VideoChunkSearchHit:
        self.draft_item(video_chunk_id, approved=True)
        return self._hit("approved_evidence", video_chunk_id)

    def append_markdown(self, markdown: str) -> None:
        block = MarkdownRenderBlock(markdown=markdown)
        with self._lock:
            before = self.revision
            self.mutate(lambda draft: draft.setdefault("render", {}).__setitem__(
                f"markdown:{uuid4().hex}", block.model_dump(mode="json"),
            ))
            if self._markdown_end_revision != before:
                self._markdown_start_revision = before
            self._markdown_end_revision = self.revision

    @staticmethod
    def _upsert_video(draft: dict, item: ChatVideoBlock) -> bool:
        if not draft.get("approval"):
            raise AgentModelInvocationError("Draft has not been approved")
        if item.video_chunk_id not in draft.get("approved", {}):
            raise UnknownToolItemError(
                "Video Item must reference approved Draft Evidence",
                "Read the approved Draft catalog and use one of its video_chunk_id values.",
            )
        blocks = draft.setdefault("render", {})
        reference = f"video:{item.video_chunk_id}"
        inserted = reference not in blocks
        blocks[reference] = item.model_dump(mode="json")
        return inserted

    def upsert_video_item(self, item: ChatVideoBlock) -> bool:
        return self.mutate(lambda draft: self._upsert_video(draft, item))

    def remove_video_item(self, video_chunk_id: str) -> bool:
        return self.mutate(lambda draft: draft.setdefault("render", {}).pop(
            f"video:{video_chunk_id}", None,
        ) is not None)

    def video_item_count(self) -> int:
        return sum(value["type"] == "video" for _reference, value in self.items("render"))

    def render_blocks(self) -> list[MarkdownRenderBlock | ChatVideoBlock]:
        return [
            (ChatVideoBlock if value["type"] == "video" else MarkdownRenderBlock).model_validate(value)
            for _reference, value in self.items("render")
        ]

    def render_draft(self) -> ChatRenderDraft:
        return ChatRenderDraft(blocks=self.render_blocks())

    def _check_collection(self, collection: str, role: str) -> None:
        if collection not in self.read_collections.get(role, ()):
            raise AgentToolFeedbackError(
                "FORBIDDEN_COLLECTION", f"{role!r} cannot read {collection!r}.",
                "Use only the collections registered for the current role.",
            )
        if role == "chat":
            self._require_approval()

    def catalog(self, collection: str, *, role: str) -> list[dict]:
        self._check_collection(collection, role)
        if collection == "render":
            return [{"reference": reference, "type": value["type"],
                     "description": (value.get("claim") or value.get("markdown", ""))[:500]}
                    for reference, value in self.items(collection)]
        if collection == "review":
            return [{"reference": reference, "turn_number": value["turn_number"],
                     "action": value["action"], "description": value["advice"][:500]}
                    for reference, value in self.items(collection)]
        if collection == "history":
            return [{"reference": reference, "turn_number": value["number"],
                     "action": value["review"]["action"],
                     "description": value["review"]["advice"][:500]}
                    for reference, value in self.items(collection)]
        if collection == "frame_inspections":
            return [{"reference": reference, **{
                key: value[key] for key in (
                    "video_chunk_id", "role", "start_ms", "end_ms", "frame_count", "cache_hit",
                )
            }} for reference, value in self.items(collection)]
        if collection == "retrieval":
            cards = []
            for reference, value in self.items(collection):
                snapshot = self.retrieval_snapshot(reference)
                evidence = snapshot.evidence
                cards.append({
                    "reference": reference, "video_chunk_id": reference,
                    "episode_order": evidence.episode_order,
                    "start_seconds": evidence.start_seconds, "end_seconds": evidence.end_seconds,
                    "description": (snapshot.selection_reason.summary if role == "supervisor"
                                    else value["selection_reason"]["summary"])[:500],
                })
            return cards
        return [{"reference": reference, "video_chunk_id": reference,
                 "episode_order": (self.approved_hit(reference) if collection == "approved"
                                   else self.retrieval_snapshot(reference)).evidence.episode_order,
                 "start_seconds": value["start_seconds"], "end_seconds": value["end_seconds"],
                 "description": value["claim"][:500]}
                for reference, value in self.items(collection)]

    def sections(self, collection: str, reference: str, *, role: str) -> dict[str, str]:
        from .context import detail_sections
        self._check_collection(collection, role)
        try:
            if collection == "render":
                return {"render": json.dumps(self.get(collection, reference), ensure_ascii=False)}
            if collection in ("review", "history", "frame_inspections"):
                return {collection: json.dumps(self.get(collection, reference), ensure_ascii=False)}
            if collection == "retrieval":
                return detail_sections(self.retrieval_snapshot(reference))
            approved = collection == "approved"
            item = self.draft_item(reference, approved=approved)
            hit = self.approved_hit(reference) if approved else self.retrieval_snapshot(reference)
            return detail_sections(hit, item)
        except UnknownToolItemError as exc:
            raise AgentToolFeedbackError("UNKNOWN_ITEM", str(exc), exc.retry_hint) from exc

    def write(self, *, role: str, batch_id: str, expected_revision: int,
              operations: list, maximum_result_bytes: int | None = None) -> dict:
        from .tools.canvas import WriteCanvasArguments
        try:
            arguments = WriteCanvasArguments.model_validate({
                "batch_id": batch_id, "expected_revision": expected_revision,
                "operations": [op.model_dump(exclude_unset=True) if hasattr(op, "model_dump") else op
                               for op in operations],
            })
        except ValidationError as exc:
            raise AgentToolFeedbackError("INVALID_ARGUMENTS", "Invalid Canvas operation arguments.",
                                         "Check the write_context_canvas schema and retry.") from exc
        allowed = {"query": {"upsert_draft_item", "remove_draft_item"},
                   "chat": {"upsert_video_item", "remove_video_item"}}
        if any(op.operation not in allowed.get(role, set()) for op in arguments.operations):
            raise AgentToolFeedbackError("FORBIDDEN_OPERATION", f"Write is not allowed for role {role!r}.",
                                         "Use only the write operations available to this role.")

        def apply(draft: dict, operations: list[dict]) -> list[dict]:
            results = []
            for op in operations:
                name, reference = op["operation"], op["video_chunk_id"]
                if name == "upsert_draft_item":
                    status, count = self._upsert_draft(draft, reference, {
                        key: value for key, value in op.items()
                        if key in {"start_seconds", "end_seconds", "claim"}
                    })
                    results.append({"accepted": True, "operation": status,
                                    "video_chunk_id": reference, "draft_item_count": count})
                elif name == "remove_draft_item":
                    items = draft.setdefault("draft", {})
                    results.append({"removed": items.pop(reference, None) is not None,
                                    "video_chunk_id": reference, "draft_item_count": len(items)})
                elif name == "upsert_video_item":
                    existing = draft.get("render", {}).get(f"video:{reference}", {})
                    item = ChatVideoBlock.model_validate({**existing, "video_chunk_id": reference,
                                                          **({"claim": op["claim"]} if "claim" in op else {})})
                    inserted = self._upsert_video(draft, item)
                    results.append({"accepted": True, "operation": "inserted" if inserted else "updated",
                                    "video_chunk_id": reference, "video_item_count": sum(
                                        block["type"] == "video" for block in draft.get("render", {}).values())})
                else:
                    results.append({"removed": draft.setdefault("render", {}).pop(
                        f"video:{reference}", None) is not None, "video_chunk_id": reference,
                                    "video_item_count": sum(block["type"] == "video"
                                                            for block in draft.get("render", {}).values())})
            return results

        try:
            with self._lock:
                # Gateway delivers this turn's text before its video Tool calls.
                # Video operations cannot alter Markdown, so only uninterrupted
                # host text appends commute with the typed Chat batch.
                if (role == "chat" and self._markdown_end_revision == self.revision
                        and self._markdown_start_revision <= expected_revision
                        <= self._markdown_end_revision):
                    expected_revision = self.revision
                return self.atomic_batch(batch_id, expected_revision,
                                         [op.model_dump(exclude_unset=True) for op in arguments.operations],
                                         apply, role=role, maximum_result_bytes=maximum_result_bytes)
        except UnknownToolItemError as exc:
            raise AgentToolFeedbackError("UNKNOWN_ITEM", str(exc), exc.retry_hint) from exc
        except InvalidDraftRangeError as exc:
            raise AgentToolFeedbackError(
                "INVALID_DRAFT_RANGE", "时间区间不合法："
                f"当前提交 start_seconds={exc.start_seconds}, end_seconds={exc.end_seconds}；必须满足 "
                f"{exc.evidence_start_seconds} <= start_seconds < end_seconds <= {exc.evidence_end_seconds}。",
                "请按 canonical Evidence 的时间范围修正后重新调用 write_context_canvas。",
            ) from exc
        except ValidationError as exc:
            raise AgentToolFeedbackError("INVALID_ARGUMENTS", "Canvas item fields are incomplete or invalid.",
                                         "Read the item and provide valid fields for the operation.") from exc
