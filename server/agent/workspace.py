from __future__ import annotations

from dataclasses import dataclass, field
from threading import RLock

from ..schema import VideoChunkSearchHit, VideoChunkSearchResult
from .state import (
    AgentModelInvocationError, ChatRenderDraft, ChatVideoBlock, DraftItem,
    MarkdownRenderBlock,
)


@dataclass(slots=True)
class AgentWorkspace:
    """Canonical per-Kick state kept outside all model message histories."""

    workspace_id: str
    series_ids: tuple[str, ...]
    _retrieval_items: dict[str, VideoChunkSearchHit] = field(
        default_factory=dict, init=False, repr=False,
    )
    _draft_items: dict[str, DraftItem] = field(default_factory=dict, init=False, repr=False)
    _draft_order: list[str] = field(default_factory=list, init=False, repr=False)
    _approved_draft_items: dict[str, DraftItem] | None = field(
        default=None, init=False, repr=False,
    )
    _approved_draft_order: tuple[str, ...] | None = field(
        default=None, init=False, repr=False,
    )
    _render_blocks: list[MarkdownRenderBlock | ChatVideoBlock] = field(
        default_factory=list, init=False, repr=False,
    )
    _lock: RLock = field(default_factory=RLock, init=False, repr=False)

    def __post_init__(self) -> None:
        if not self.workspace_id.strip():
            raise ValueError("workspace_id must not be empty")
        if not self.series_ids or any(not value.strip() for value in self.series_ids):
            raise ValueError("series_ids must contain nonempty values")
        if len(self.series_ids) != len(set(self.series_ids)):
            raise ValueError("series_ids must not contain duplicates")

    def record_retrieval(self, result: VideoChunkSearchResult) -> None:
        if set(result.series_ids) != set(self.series_ids):
            raise AgentModelInvocationError("Retrieval changed the fixed Series scope")
        incoming: dict[str, VideoChunkSearchHit] = {}
        for hit in result.hits:
            evidence = hit.evidence
            video_chunk_id = hit.candidate.video_chunk_id
            if evidence.video_chunk_id != video_chunk_id:
                raise AgentModelInvocationError(
                    "Retrieved Candidate and Evidence IDs do not match"
                )
            if video_chunk_id in incoming:
                raise AgentModelInvocationError(
                    "Retrieval returned duplicate VideoChunk IDs"
                )
            if (
                evidence.workspace_id != self.workspace_id
                or evidence.series_id not in self.series_ids
            ):
                raise AgentModelInvocationError(
                    "Retrieved Evidence is outside the fixed scope"
                )
            incoming[video_chunk_id] = hit.model_copy(deep=True)
        with self._lock:
            self._retrieval_items.update(incoming)

    def retrieval_item(self, video_chunk_id: str) -> VideoChunkSearchHit:
        with self._lock:
            item = self._retrieval_items.get(video_chunk_id)
            if item is None:
                raise AgentModelInvocationError(
                    f"Unknown retrieved video_chunk_id {video_chunk_id!r}"
                )
            return item.model_copy(deep=True)

    def retrieval_item_ids(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._retrieval_items)

    def upsert_draft_item(self, item: DraftItem) -> bool:
        with self._lock:
            evidence_hit = self._retrieval_items.get(item.video_chunk_id)
            if evidence_hit is None:
                raise AgentModelInvocationError(
                    "Draft Item must reference retrieved Evidence"
                )
            evidence = evidence_hit.evidence
            if not (
                evidence.start_ms
                <= item.start_ms
                < item.end_ms
                <= evidence.end_ms
            ):
                raise AgentModelInvocationError(
                    "Draft Item range must be contained in canonical Evidence"
                )
            inserted = item.video_chunk_id not in self._draft_items
            if inserted:
                self._draft_order.append(item.video_chunk_id)
            self._draft_items[item.video_chunk_id] = item.model_copy(deep=True)
            return inserted

    def remove_draft_item(self, video_chunk_id: str) -> bool:
        with self._lock:
            removed = self._draft_items.pop(video_chunk_id, None) is not None
            if removed:
                self._draft_order.remove(video_chunk_id)
            return removed

    def draft_items(self, *, approved: bool = False) -> list[DraftItem]:
        with self._lock:
            items, order = self._draft_state(approved=approved)
            return [items[value].model_copy(deep=True) for value in order]

    def draft_item(self, video_chunk_id: str, *, approved: bool = False) -> DraftItem:
        with self._lock:
            items, _order = self._draft_state(approved=approved)
            item = items.get(video_chunk_id)
            if item is None:
                label = "approved Draft" if approved else "Draft"
                raise AgentModelInvocationError(
                    f"Unknown {label} video_chunk_id {video_chunk_id!r}"
                )
            return item.model_copy(deep=True)

    def approve_draft(self) -> None:
        with self._lock:
            self._approved_draft_items = {
                key: value.model_copy(deep=True)
                for key, value in self._draft_items.items()
            }
            self._approved_draft_order = tuple(self._draft_order)

    def append_markdown(self, markdown: str) -> None:
        with self._lock:
            self._render_blocks.append(MarkdownRenderBlock(markdown=markdown))

    def upsert_video_item(self, item: ChatVideoBlock) -> bool:
        with self._lock:
            approved, _order = self._draft_state(approved=True)
            if item.video_chunk_id not in approved:
                raise AgentModelInvocationError(
                    "Video Item must reference approved Draft Evidence"
                )
            for index, block in enumerate(self._render_blocks):
                if (
                    isinstance(block, ChatVideoBlock)
                    and block.video_chunk_id == item.video_chunk_id
                ):
                    self._render_blocks[index] = item.model_copy(deep=True)
                    return False
            self._render_blocks.append(item.model_copy(deep=True))
            return True

    def remove_video_item(self, video_chunk_id: str) -> bool:
        with self._lock:
            for index, block in enumerate(self._render_blocks):
                if (
                    isinstance(block, ChatVideoBlock)
                    and block.video_chunk_id == video_chunk_id
                ):
                    self._render_blocks.pop(index)
                    return True
            return False

    def video_item_count(self) -> int:
        with self._lock:
            return sum(isinstance(block, ChatVideoBlock) for block in self._render_blocks)

    def render_blocks(self) -> list[MarkdownRenderBlock | ChatVideoBlock]:
        with self._lock:
            return [block.model_copy(deep=True) for block in self._render_blocks]

    def render_draft(self) -> ChatRenderDraft:
        with self._lock:
            return ChatRenderDraft(
                blocks=[block.model_copy(deep=True) for block in self._render_blocks]
            )

    def _draft_state(
        self,
        *,
        approved: bool,
    ) -> tuple[dict[str, DraftItem], list[str] | tuple[str, ...]]:
        if not approved:
            return self._draft_items, self._draft_order
        if self._approved_draft_items is None or self._approved_draft_order is None:
            raise AgentModelInvocationError("Draft has not been approved")
        return self._approved_draft_items, self._approved_draft_order
