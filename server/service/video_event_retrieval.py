"""Event recall, Engine ranking and batch MySQL hydration over the existing scope."""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import nullcontext

from engine.rag.contracts import (
    AgentReply, LexicalMatch, MatchedEvidence, SelectionReason, VideoChunkCandidate,
    VideoChunkRetrievalIdentity, VideoChunkSearchHit, VideoChunkSearchResult,
)
from engine.rag.indexing.events import EventIndexingContract
from engine.rag.retrieval.contracts import (
    EventFilters, EventIndexNotReadyError, EventSearchResult, RetrievalConsistencyError, RetrievalPolicy,
)
from engine.rag.retrieval.events import (
    EVENT_RETRIEVAL_STRATEGY_VERSION, fuse_event_hits, select_event_chunk_hits, verify_event_candidate,
)
from gateway.tracing import LangfuseTracing
from gateway.usage import model_call_context

from ..repository.video_parse import VideoParseConflictError, VideoParseRepository
from ..repository.video import SeriesNotReadyError
from ..storage.elasticsearch_events import ElasticsearchVideoEventStore
from .video_retrieval import QueryEmbedder, RetrievalIndexConsistencyError

class VideoEventRetrievalService:
    """Keep Event ranking and facts typed; expose familiar Chunk Evidence to the Agent."""

    def __init__(
        self, repository: VideoParseRepository, store: ElasticsearchVideoEventStore,
        embedder: QueryEmbedder, *, contract: EventIndexingContract,
        policy: RetrievalPolicy, vision_embedder: QueryEmbedder | None = None,
        exact: bool = False, tracing: LangfuseTracing | None = None,
    ) -> None:
        self._contract = EventIndexingContract.model_validate(contract)
        self._policy = RetrievalPolicy.model_validate(policy)
        if self._policy.sparse_weight:
            raise ValueError("Event retrieval has lexical, semantic and vision channels")
        if (store.semantic_dimensions != contract.semantic_encoder.dimensions
                or store.vision_dimensions != contract.vision_encoder.dimensions):
            raise ValueError("Event query and document dimensions differ")
        if vision_embedder is None and contract.semantic_encoder != contract.vision_encoder:
            raise ValueError("distinct Event encoders require a distinct compatible vision query embedder")
        if not isinstance(exact, bool):
            raise ValueError("exact must be a bool")
        self._repository, self._store, self._embedder = repository, store, embedder
        self._vision_embedder = embedder if vision_embedder is None else vision_embedder
        self._exact, self._tracing = exact, tracing

    def search(
        self, query: str, *, tenant_id: str, series_ids: Sequence[str], top_k: int = 10,
        lexical_weight: float | None = None, text_weight: float | None = None,
        vision_weight: float | None = None, event_types: Sequence[str] | None = None,
        speaker: str | None = None, video_chunk_ids: Sequence[str] | None = None,
    ) -> EventSearchResult:
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must be nonblank")
        if not isinstance(tenant_id, str) or not tenant_id.strip() or tenant_id != tenant_id.strip():
            raise ValueError("tenant_id must be nonblank and unpadded")
        if type(top_k) is not int or top_k <= 0:
            raise ValueError("top_k must be a positive integer")
        for name, value in (("event_types", event_types), ("video_chunk_ids", video_chunk_ids)):
            if isinstance(value, (str, bytes)):
                raise ValueError(f"{name} must be a sequence, not a string")
        filters = EventFilters(
            event_types=tuple(event_types or ()), speaker=speaker,
            video_chunk_ids=tuple(video_chunk_ids or ()),
        )
        weights = (lexical_weight, text_weight, vision_weight)
        if any(value is not None for value in weights) and any(value is None for value in weights):
            raise ValueError("all three Event channel weights must be supplied together")
        policy = RetrievalPolicy.model_validate(self._policy.model_copy(update={
            "top_k": top_k, "candidate_budget": max(top_k, self._policy.candidate_budget),
            **({"lexical_weight": lexical_weight, "semantic_weight": text_weight,
                "vision_weight": vision_weight} if lexical_weight is not None else {}),
        }))
        query = query.strip()
        try:
            selected = self._repository.resolve_retrieval_scope(series_ids, tenant_id=tenant_id)
        except (VideoParseConflictError, SeriesNotReadyError) as error:
            raise EventIndexNotReadyError(str(error)) from error
        # Absence/coverage is checked before any model call; it is not an empty match.
        self._store.bind_contract(self._contract)
        try:
            sources = self._repository.validate_event_coverage(
                selected, index_name=self._store.index_name, contract_id=self._contract.contract_id,
                video_chunk_ids=filters.video_chunk_ids or None,
            )
        except VideoParseConflictError as error:
            raise EventIndexNotReadyError(str(error)) from error
        source_set = set(sources)
        context = {
            "tenant_id": tenant_id, "series_ids": selected.series_ids,
            "video_ids": selected.searched_video_ids,
            "video_parse_task_ids": tuple(selected.parse_tasks_by_video_id.values()),
            "stage": "event_retrieval", "event_index": self._store.index_name,
        }
        tracing = self._tracing.scope(context) if self._tracing is not None else nullcontext()
        raw = []
        effective = {key: policy.weights[key] for key in ("lexical", "semantic", "vision")}
        with model_call_context(context), tracing:
            if sources and policy.lexical_weight:
                raw.extend(self._store.search_lexical(
                    query, sources=sources, filters=filters, limit=policy.channel_top_k,
                ))
            vectors = {}
            for channel, encoder, embedder in (
                ("semantic", self._contract.semantic_encoder, self._embedder),
                ("vision", self._contract.vision_encoder, self._vision_embedder),
            ):
                if not sources or not effective[channel]:
                    continue
                # A shared query is reusable only when the full declared encoder matches.
                cache_key = (id(embedder), encoder)
                vector = vectors.get(cache_key)
                if vector is None:
                    result = embedder.embed_query(query)
                    if result.modality != "text" or result.model != encoder.model:
                        raise RetrievalIndexConsistencyError("Event query returned a different model or modality")
                    vector = result.vector
                    vectors[cache_key] = vector
                raw.extend(self._store.search_vector(
                    vector, channel=channel, sources=sources, filters=filters,
                    limit=policy.channel_top_k, exact=self._exact,
                ))
        for hit in raw:
            if (hit.parse_task_id, hit.video_chunk_id, hit.version) not in source_set:
                raise RetrievalIndexConsistencyError("Event candidate escaped its authorized SQL source")
        fused = fuse_event_hits(raw, channel_weights=effective, rrf_k=policy.rrf_k)
        budgeted = fused[:policy.candidate_budget]
        task_videos = {task: video for video, task in selected.parse_tasks_by_video_id.items()}
        identities = {}
        for hit in budgeted:
            video_id = task_videos[hit.parse_task_id]
            identity = VideoChunkRetrievalIdentity(
                tenant_id=tenant_id, series_id=selected.series_by_video_id[video_id], video_id=video_id,
                parse_task_id=hit.parse_task_id, content_version=hit.version,
                index_generation=selected.index_generation,
            )
            previous = identities.setdefault(hit.video_chunk_id, identity)
            if previous != identity:
                raise RetrievalIndexConsistencyError("Event candidates disagree about one parent Chunk")
        try:
            hydrated = self._repository.hydrate_retrieval_candidates(list(identities.items())) if identities else ()
        except VideoParseConflictError as error:
            raise RetrievalIndexConsistencyError(str(error)) from error
        by_chunk = {item.facts.video_chunk_id: item for item in hydrated}
        verified, rejected = [], []
        for hit in budgeted:
            try:
                parent = by_chunk.get(hit.video_chunk_id)
                if parent is None:
                    raise RetrievalConsistencyError("Event parent was absent during batch hydration")
                verified.append(verify_event_candidate(hit, parent.facts, contract=self._contract))
            except RetrievalConsistencyError as error:
                rejected.append({"event_id": hit.event_id, "reason": str(error)})
        matches = select_event_chunk_hits(verified, top_k=top_k)
        grouped = {}
        for match in matches:
            grouped.setdefault(match.video_chunk_id, []).append(match)
        hits = []
        for rank, (chunk_id, events) in enumerate(grouped.items(), 1):
            channels = {}
            for event in events:
                for channel, channel_rank in event.channel_ranks.items():
                    if channel not in channels or channel_rank < channels[channel][0]:
                        channels[channel] = (channel_rank, event.channel_scores[channel])
            ranking = {}
            for channel, prefix in (("lexical", "lexical"), ("semantic", "text"), ("vision", "vision")):
                if channel in channels:
                    ranking[f"{prefix}_rank"], ranking[f"{prefix}_score"] = channels[channel]
            proofs, lexical = [], []
            facts = by_chunk[chunk_id].facts
            for event in events:
                fact = facts.result.timeline[event.timeline_index]
                proofs.append(MatchedEvidence(
                    kind="TRANSCRIPT" if event.event_type == "DIALOGUE" else "VISUAL_EVENT",
                    text=fact.description, start_seconds=event.start_ms, end_seconds=event.end_ms,
                ))
                for field in event.matched_fields:
                    lexical.append(LexicalMatch(
                        **field.model_dump(), speaker=event.speaker if field.field == "dialogue" else None,
                        start_seconds=event.start_ms, end_seconds=event.end_ms,
                        timeline_index=event.timeline_index,
                    ))
            hits.append(VideoChunkSearchHit(
                candidate=VideoChunkCandidate(
                    video_chunk_id=chunk_id, fused_rank=rank, fused_score=events[0].fused_score, **ranking,
                ),
                selection_reason=SelectionReason(
                    summary="；".join(proof.text for proof in proofs), matched_evidence=proofs,
                    lexical_matches=lexical, limitations=[],
                ), evidence=by_chunk[chunk_id].evidence,
            ))
        warnings = list(selected.warnings)
        if rejected:
            warnings.append("部分 Event 候选与当前正式内容不一致，已剔除；可重试或使用 Chunk 检索。")
        chunk_result = VideoChunkSearchResult(
            query=query, series_ids=list(selected.series_ids), searched_video_ids=list(selected.searched_video_ids),
            partial=selected.partial or bool(rejected), warnings=warnings, hits=hits,
            agent_reply=AgentReply(message=f"找到 {len(hits)} 个相关视频片段。", result_count=len(hits)),
        )
        return EventSearchResult(chunk_result=chunk_result, matches=matches, trace={
            "strategy_version": EVENT_RETRIEVAL_STRATEGY_VERSION,
            "query": query, "filters": filters.model_dump(mode="json"),
            "index_name": self._store.index_name, "contract_id": self._contract.contract_id,
            "sources": [list(source) for source in sources], "top_k": top_k,
            "channel_top_k": policy.channel_top_k, "candidate_budget": policy.candidate_budget,
            "channel_weights": effective,
            "raw_candidates": [hit.model_dump(mode="json") for hit in raw],
            "fused_candidates": [hit.model_dump(mode="json") for hit in fused],
            "budgeted_event_ids": [hit.event_id for hit in budgeted],
            "budget_excluded_event_ids": [hit.event_id for hit in fused[policy.candidate_budget:]],
            "rejected": rejected, "selected_event_ids": [hit.event_id for hit in matches],
            "returned_chunk_ids": list(grouped),
        })
