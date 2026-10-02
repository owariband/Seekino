from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from engine.retrieval import EncoderContract, compute_encoder_id
from engine.retrieval.video_retrieval import (
    RankedTranscriptMatch,
    RankedVideoChunkMatch,
    VersionedRankedVideoChunkMatch,
    fuse_video_chunk_rankings,
    fuse_versioned_video_chunk_rankings,
    normalize_hybrid_retrieval_weights,
    rank_transcript_chunks,
)
from engine.retrieval.contracts import LexicalMatch, VideoChunkRetrievalIdentity
from gateway.embedding import EmbeddingResult

from ..config.retrieval import RetrievalConfig
from ..model.index_sync import IndexSyncOperation, IndexSyncStatus
from ..model.dto.videoDTO import HydratedVideoChunkState
from ..repository.video import VideoChunkNotFoundError, VideoRepository
from ..repository.video_parse import (
    VideoParseConflictError,
    VideoParseRepository,
)
from ..schema import (
    AgentReply,
    MatchedEvidence,
    SelectionReason,
    VideoChunkCandidate,
    VideoChunkEvidence,
    VideoChunkSearchHit,
    VideoChunkSearchResult,
)
from ..storage.qdrant import (
    QdrantPointConsistencyError,
    QdrantVideoChunkStore,
    VideoChunkIndexMatch as QdrantVideoChunkIndexMatch,
)
from ..storage.elasticsearch import (
    ElasticsearchDocumentConsistencyError,
    ElasticsearchVideoChunkStore,
    VideoChunkIndexMatch as ElasticsearchVideoChunkIndexMatch,
)

_DEFAULT_RETRIEVAL_CONFIG = RetrievalConfig()


class QueryEmbedder(Protocol):
    def embed_query(self, query: str) -> EmbeddingResult: ...


class RetrievalIndexConsistencyError(RuntimeError):
    """Retrieved candidates do not match the current MySQL index facts."""


class VideoRetrievalService:
    """Run scoped hybrid retrieval and hydrate formal video evidence."""

    def __init__(
        self,
        repository: VideoRepository,
        store: QdrantVideoChunkStore,
        embedder: QueryEmbedder,
        *,
        channel_top_k: int = _DEFAULT_RETRIEVAL_CONFIG.channel_top_k,
        rrf_k: int = _DEFAULT_RETRIEVAL_CONFIG.rrf_k,
        text_weight: float = _DEFAULT_RETRIEVAL_CONFIG.text_weight,
        vision_weight: float = _DEFAULT_RETRIEVAL_CONFIG.vision_weight,
        exact: bool = False,
    ) -> None:
        if store.text_dimensions != store.vision_dimensions:
            raise ValueError(
                "text and vision dimensions must match for shared query embedding"
            )
        if not isinstance(exact, bool):
            raise ValueError("exact must be a bool")
        # The pure Engine function owns validation of the RRF configuration.
        fuse_video_chunk_rankings(
            [],
            [],
            transcript_weight=0.0,
            text_weight=text_weight,
            vision_weight=vision_weight,
            channel_top_k=channel_top_k,
            rrf_k=rrf_k,
        )
        self._repository = repository
        self._store = store
        self._embedder = embedder
        self._channel_top_k = channel_top_k
        self._rrf_k = rrf_k
        self._text_weight = text_weight
        self._vision_weight = vision_weight
        self._exact = exact

    def search(
        self,
        query: str,
        *,
        workspace_id: str,
        series_ids: Sequence[str],
        top_k: int = 10,
        transcript_weight: float | None = None,
        text_weight: float | None = None,
        vision_weight: float | None = None,
    ) -> VideoChunkSearchResult:
        if (
            not isinstance(workspace_id, str)
            or not workspace_id
            or workspace_id != workspace_id.strip()
        ):
            raise ValueError("workspace_id must be a non-empty string")
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must not be empty")
        if isinstance(top_k, bool) or not isinstance(top_k, int) or top_k <= 0:
            raise ValueError("top_k must be a positive integer")
        if (text_weight is None) != (vision_weight is None):
            raise ValueError("text_weight and vision_weight must be provided together")
        if transcript_weight is not None and text_weight is None:
            raise ValueError(
                "text_weight and vision_weight are required with transcript_weight"
            )

        requested_transcript_weight = (
            0.0 if transcript_weight is None else transcript_weight
        )
        requested_text_weight = self._text_weight if text_weight is None else text_weight
        requested_vision_weight = (
            self._vision_weight if vision_weight is None else vision_weight
        )
        (
            effective_transcript_weight,
            effective_text_weight,
            effective_vision_weight,
        ) = normalize_hybrid_retrieval_weights(
            requested_transcript_weight,
            requested_text_weight,
            requested_vision_weight,
        )

        normalized_query = query.strip()
        scope = self._repository.require_ready_series(
            series_ids,
            workspace_id=workspace_id,
        )
        searched_video_ids = self._repository.list_indexed_video_ids(
            scope,
            workspace_id=workspace_id,
        )
        if not searched_video_ids:
            raise RetrievalIndexConsistencyError(
                "a READY Series scope has no fully indexed video"
            )
        transcript_matches = (
            rank_transcript_chunks(
                normalized_query,
                self._repository.list_searchable_transcripts(
                    scope,
                    workspace_id=workspace_id,
                    video_ids=searched_video_ids,
                ),
                limit=self._channel_top_k,
            )
            if effective_transcript_weight > 0
            else []
        )

        text_matches: list[QdrantVideoChunkIndexMatch] = []
        vision_matches: list[QdrantVideoChunkIndexMatch] = []
        if effective_text_weight > 0 or effective_vision_weight > 0:
            embedding = self._embedder.embed_query(normalized_query)
            if embedding.modality != "text":
                raise RuntimeError("query embedder returned a non-text embedding")

            try:
                text_matches = (
                    self._store.search_text(
                        embedding.vector,
                        series_ids=scope,
                        video_ids=searched_video_ids,
                        limit=self._channel_top_k,
                        exact=self._exact,
                    )
                    if effective_text_weight > 0
                    else []
                )
                vision_matches = (
                    self._store.search_vision(
                        embedding.vector,
                        series_ids=scope,
                        video_ids=searched_video_ids,
                        limit=self._channel_top_k,
                        exact=self._exact,
                    )
                    if effective_vision_weight > 0
                    else []
                )
            except QdrantPointConsistencyError as exc:
                raise RetrievalIndexConsistencyError(str(exc)) from exc

        if (
            (effective_text_weight > 0 and not text_matches)
            or (effective_vision_weight > 0 and not vision_matches)
        ):
            raise RetrievalIndexConsistencyError(
                "a READY Series scope returned no candidate in an enabled Qdrant channel"
            )
        index_state = _merge_index_state(text_matches, vision_matches)
        fused = fuse_video_chunk_rankings(
            _engine_rankings(text_matches),
            _engine_rankings(vision_matches),
            transcript_rankings=transcript_matches,
            transcript_weight=effective_transcript_weight,
            text_weight=effective_text_weight,
            vision_weight=effective_vision_weight,
            channel_top_k=self._channel_top_k,
            rrf_k=self._rrf_k,
        )[:top_k]
        if not fused:
            return VideoChunkSearchResult(
                query=normalized_query,
                series_ids=scope,
                searched_video_ids=searched_video_ids,
                agent_reply=_agent_reply([]),
                hits=[],
            )

        candidate_ids = [item.video_chunk_id for item in fused]
        try:
            hydrated = self._repository.hydrate_video_chunk_states(candidate_ids)
        except VideoChunkNotFoundError as exc:
            raise RetrievalIndexConsistencyError(
                "retrieval returned VideoChunk IDs missing from MySQL"
            ) from exc
        if len(hydrated) != len(fused):
            raise RetrievalIndexConsistencyError(
                "MySQL hydrate returned a different number of VideoChunks"
            )

        hits: list[VideoChunkSearchHit] = []
        transcript_matches_by_id = {
            match.video_chunk_id: match for match in transcript_matches
        }
        for ranking, state in zip(fused, hydrated, strict=True):
            match = index_state.get(ranking.video_chunk_id)
            _validate_hydrated_state(match, state, scope)
            candidate = VideoChunkCandidate.model_validate(ranking.model_dump())
            hits.append(
                VideoChunkSearchHit(
                    candidate=candidate,
                    selection_reason=_selection_reason(
                        candidate,
                        state.evidence,
                        transcript_match=transcript_matches_by_id.get(
                            ranking.video_chunk_id
                        ),
                        transcript_enabled=effective_transcript_weight > 0,
                        text_enabled=effective_text_weight > 0,
                        vision_enabled=effective_vision_weight > 0,
                    ),
                    evidence=state.evidence,
                )
            )
        return VideoChunkSearchResult(
            query=normalized_query,
            series_ids=scope,
            searched_video_ids=searched_video_ids,
            agent_reply=_agent_reply(hits),
            hits=hits,
        )


class VideoParseRetrievalService:
    """Execute V2 ES channels and hydrate only exact current MySQL evidence."""

    def __init__(
        self,
        repository: VideoParseRepository,
        store: ElasticsearchVideoChunkStore,
        embedder: QueryEmbedder,
        *,
        semantic_encoder: EncoderContract,
        expected_index_generation: int,
        channel_top_k: int = _DEFAULT_RETRIEVAL_CONFIG.channel_top_k,
        rrf_k: int = _DEFAULT_RETRIEVAL_CONFIG.rrf_k,
        lexical_weight: float = _DEFAULT_RETRIEVAL_CONFIG.lexical_weight,
        text_weight: float = _DEFAULT_RETRIEVAL_CONFIG.text_weight,
        vision_weight: float = _DEFAULT_RETRIEVAL_CONFIG.vision_weight,
        exact: bool = False,
    ) -> None:
        if store.semantic_dimensions != store.vision_dimensions:
            raise ValueError(
                "semantic and vision dimensions must match for shared query embedding"
            )
        if store.semantic_dimensions != semantic_encoder.dimensions:
            raise ValueError(
                "semantic dimensions differ between the query encoder and store"
            )
        if (
            isinstance(expected_index_generation, bool)
            or not isinstance(expected_index_generation, int)
            or not 1 <= expected_index_generation <= (1 << 31) - 1
        ):
            raise ValueError(
                "expected_index_generation must fit the Elasticsearch version contract"
            )
        if not isinstance(exact, bool):
            raise ValueError("exact must be a bool")
        # Engine owns channel validation, candidate identity and fusion policy.
        fuse_versioned_video_chunk_rankings(
            [],
            [],
            lexical_rankings=[],
            lexical_weight=lexical_weight,
            text_weight=text_weight,
            vision_weight=vision_weight,
            channel_top_k=channel_top_k,
            rrf_k=rrf_k,
        )
        self._repository = repository
        self._store = store
        self._embedder = embedder
        self._semantic_encoder = semantic_encoder
        self._semantic_encoder_id = compute_encoder_id("text", semantic_encoder)
        self._expected_index_generation = expected_index_generation
        self._channel_top_k = channel_top_k
        self._rrf_k = rrf_k
        self._lexical_weight = lexical_weight
        self._text_weight = text_weight
        self._vision_weight = vision_weight
        self._exact = exact

    def search(
        self,
        query: str,
        *,
        workspace_id: str,
        series_ids: Sequence[str],
        top_k: int = 10,
        lexical_weight: float | None = None,
        transcript_weight: float | None = None,
        text_weight: float | None = None,
        vision_weight: float | None = None,
    ) -> VideoChunkSearchResult:
        if (
            not isinstance(workspace_id, str)
            or not workspace_id
            or workspace_id != workspace_id.strip()
        ):
            raise ValueError("workspace_id must be a non-empty string")
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must not be empty")
        if isinstance(top_k, bool) or not isinstance(top_k, int) or top_k <= 0:
            raise ValueError("top_k must be a positive integer")
        if transcript_weight is not None:
            raise ValueError(
                "Video Parse V2 retrieval uses lexical_weight, not transcript_weight"
            )
        overrides = (lexical_weight, text_weight, vision_weight)
        if any(value is not None for value in overrides) and not all(
            value is not None for value in overrides
        ):
            raise ValueError(
                "lexical_weight, text_weight and vision_weight must be provided together"
            )
        requested_lexical_weight = (
            self._lexical_weight if lexical_weight is None else lexical_weight
        )
        requested_text_weight = self._text_weight if text_weight is None else text_weight
        requested_vision_weight = (
            self._vision_weight if vision_weight is None else vision_weight
        )
        # TODO(video-parse-v2/P8): When Engine publishes its query-policy
        # abstraction, call it here for channel selection. Server should only
        # consume that plan and call Engine fusion, never duplicate policy.
        (
            effective_lexical_weight,
            effective_text_weight,
            effective_vision_weight,
        ) = normalize_hybrid_retrieval_weights(
            requested_lexical_weight,
            requested_text_weight,
            requested_vision_weight,
        )

        normalized_query = query.strip()
        try:
            scope = self._repository.resolve_retrieval_scope(
                series_ids,
                workspace_id=workspace_id,
            )
        except VideoParseConflictError as exc:
            raise RetrievalIndexConsistencyError(str(exc)) from exc

        if scope.index_generation != self._expected_index_generation:
            raise RetrievalIndexConsistencyError(
                "selected V2 task index generation differs from the active "
                "Elasticsearch generation"
            )
        vectors_enabled = effective_text_weight > 0 or effective_vision_weight > 0
        if vectors_enabled:
            if scope.semantic_encoder_id != self._semantic_encoder_id:
                raise RetrievalIndexConsistencyError(
                    "selected V2 task semantic encoder identity differs from the "
                    "active query encoder contract"
                )
            if scope.embedding_model != self._semantic_encoder.model:
                raise RetrievalIndexConsistencyError(
                    "selected V2 task embedding model differs from the active "
                    "query encoder contract"
                )

        search_scope = {
            "workspace_id": workspace_id,
            "series_ids": scope.series_ids,
            "parse_tasks_by_video_id": scope.parse_tasks_by_video_id,
            "retrieval_contract_id": scope.retrieval_contract_id,
        }
        try:
            lexical_matches = (
                self._store.search_lexical(
                    normalized_query,
                    **search_scope,
                    limit=self._channel_top_k,
                )
                if effective_lexical_weight > 0
                else []
            )

            semantic_matches: list[ElasticsearchVideoChunkIndexMatch] = []
            vision_matches: list[ElasticsearchVideoChunkIndexMatch] = []
            if vectors_enabled:
                embedding = self._embedder.embed_query(normalized_query)
                if embedding.modality != "text":
                    raise RuntimeError("query embedder returned a non-text embedding")
                if embedding.model != self._semantic_encoder.model:
                    raise RetrievalIndexConsistencyError(
                        "query embedder returned a model outside its semantic "
                        "encoder contract"
                    )
                if embedding.dimension != self._semantic_encoder.dimensions:
                    raise RetrievalIndexConsistencyError(
                        "query embedder returned dimensions outside its semantic "
                        "encoder contract"
                    )
                semantic_matches = (
                    self._store.search_semantic(
                        embedding.vector,
                        **search_scope,
                        encoder_id=scope.semantic_encoder_id,
                        limit=self._channel_top_k,
                        exact=self._exact,
                    )
                    if effective_text_weight > 0
                    else []
                )
                vision_matches = (
                    self._store.search_vision(
                        embedding.vector,
                        **search_scope,
                        encoder_id=scope.vision_encoder_id,
                        limit=self._channel_top_k,
                        exact=self._exact,
                    )
                    if effective_vision_weight > 0
                    else []
                )
        except ElasticsearchDocumentConsistencyError as exc:
            raise RetrievalIndexConsistencyError(str(exc)) from exc

        try:
            fused = fuse_versioned_video_chunk_rankings(
                _versioned_engine_rankings(semantic_matches),
                _versioned_engine_rankings(vision_matches),
                lexical_rankings=_versioned_engine_rankings(lexical_matches),
                lexical_weight=effective_lexical_weight,
                text_weight=effective_text_weight,
                vision_weight=effective_vision_weight,
                channel_top_k=self._channel_top_k,
                rrf_k=self._rrf_k,
            )[:top_k]
        except (TypeError, ValueError) as exc:
            raise RetrievalIndexConsistencyError(str(exc)) from exc
        if not fused:
            return VideoChunkSearchResult(
                query=normalized_query,
                series_ids=list(scope.series_ids),
                searched_video_ids=list(scope.searched_video_ids),
                partial=scope.partial,
                warnings=list(scope.warnings),
                agent_reply=_agent_reply([]),
                hits=[],
            )

        try:
            hydrated = self._repository.hydrate_retrieval_evidence(
                [
                    (item.video_chunk_id, item.retrieval_identity)
                    for item in fused
                ]
            )
        except VideoParseConflictError as exc:
            raise RetrievalIndexConsistencyError(str(exc)) from exc
        if len(hydrated) != len(fused):
            raise RetrievalIndexConsistencyError(
                "MySQL hydrate returned a different number of V2 VideoChunks"
            )

        lexical_by_id = {
            match.video_chunk_id: match for match in lexical_matches
        }
        hits: list[VideoChunkSearchHit] = []
        for ranking, evidence in zip(fused, hydrated, strict=True):
            candidate = VideoChunkCandidate.model_validate(
                ranking.model_dump(exclude={"retrieval_identity"})
            )
            lexical_match = lexical_by_id.get(ranking.video_chunk_id)
            hits.append(VideoChunkSearchHit(
                candidate=candidate,
                selection_reason=_v2_selection_reason(
                    candidate,
                    evidence,
                    lexical_match=lexical_match,
                    lexical_enabled=effective_lexical_weight > 0,
                    text_enabled=effective_text_weight > 0,
                    vision_enabled=effective_vision_weight > 0,
                ),
                evidence=evidence,
            ))
        return VideoChunkSearchResult(
            query=normalized_query,
            series_ids=list(scope.series_ids),
            searched_video_ids=list(scope.searched_video_ids),
            partial=scope.partial,
            warnings=list(scope.warnings),
            agent_reply=_agent_reply(hits),
            hits=hits,
        )


def _versioned_engine_rankings(
    matches: Sequence[ElasticsearchVideoChunkIndexMatch],
) -> list[VersionedRankedVideoChunkMatch]:
    return [
        VersionedRankedVideoChunkMatch(
            rank=match.rank,
            video_chunk_id=match.video_chunk_id,
            score=match.score,
            retrieval_identity=VideoChunkRetrievalIdentity(
                workspace_id=match.workspace_id,
                series_id=match.series_id,
                video_id=match.video_id,
                parse_task_id=match.parse_task_id,
                content_version=match.content_version,
                index_generation=match.index_generation,
                source_revision=match.source_revision,
                projection_revision=match.projection_revision,
                retrieval_contract_id=match.retrieval_contract_id,
                semantic_encoder_id=match.semantic_encoder_id,
                vision_encoder_id=match.vision_encoder_id,
            ),
        )
        for match in matches
    ]


def _engine_rankings(
    matches: Sequence[QdrantVideoChunkIndexMatch],
) -> list[RankedVideoChunkMatch]:
    return [
        RankedVideoChunkMatch(
            rank=match.rank,
            video_chunk_id=match.video_chunk_id,
            score=match.score,
        )
        for match in matches
    ]


def _merge_index_state(
    text_matches: Sequence[QdrantVideoChunkIndexMatch],
    vision_matches: Sequence[QdrantVideoChunkIndexMatch],
) -> dict[str, QdrantVideoChunkIndexMatch]:
    by_id: dict[str, QdrantVideoChunkIndexMatch] = {}
    for match in (*text_matches, *vision_matches):
        current = by_id.get(match.video_chunk_id)
        if current is not None and _point_identity(current) != _point_identity(match):
            raise RetrievalIndexConsistencyError(
                "Qdrant point identity changed between retrieval channels: "
                f"{match.video_chunk_id}"
            )
        by_id[match.video_chunk_id] = match
    return by_id


def _point_identity(
    match: QdrantVideoChunkIndexMatch,
) -> tuple[str, str, str, str, bool]:
    return (
        match.video_chunk_id,
        match.series_id,
        match.video_id,
        match.source_revision,
        match.is_fallback,
    )


def _validate_hydrated_state(
    match: QdrantVideoChunkIndexMatch | None,
    state: HydratedVideoChunkState,
    scope: Sequence[str],
) -> None:
    evidence = state.evidence
    mismatches: list[str] = []
    if evidence.series_id not in scope:
        mismatches.append("seriesId")
    if match is not None:
        if evidence.video_chunk_id != match.video_chunk_id:
            mismatches.append("videoChunkId")
        if evidence.series_id != match.series_id:
            mismatches.append("seriesId")
        if evidence.video_id != match.video_id:
            mismatches.append("videoId")
        if evidence.is_fallback != match.is_fallback:
            mismatches.append("isFallback")
        if state.source_revision != match.source_revision:
            mismatches.append("sourceRevision")
    if state.index_operation != IndexSyncOperation.UPSERT.value:
        mismatches.append("indexOperation")
    if state.index_status != IndexSyncStatus.SYNCED.value:
        mismatches.append("indexStatus")
    if mismatches:
        raise RetrievalIndexConsistencyError(
            f"retrieval candidate {evidence.video_chunk_id} disagrees with MySQL fields: "
            + ", ".join(mismatches)
        )


def _v2_selection_reason(
    candidate: VideoChunkCandidate,
    evidence: VideoChunkEvidence,
    *,
    lexical_match: ElasticsearchVideoChunkIndexMatch | None,
    lexical_enabled: bool,
    text_enabled: bool,
    vision_enabled: bool,
) -> SelectionReason:
    has_lexical = candidate.lexical_rank is not None
    has_text = candidate.text_rank is not None
    has_vision = candidate.vision_rank is not None
    lexical_matches = (
        [LexicalMatch.model_validate(item) for item in lexical_match.matched_fields]
        if lexical_match is not None
        else []
    )
    if has_lexical and not lexical_matches:
        raise RetrievalIndexConsistencyError(
            "fused lexical rank has no matching ES field evidence"
        )

    matched: list[MatchedEvidence] = []
    for item in lexical_matches:
        if item.field in {"questions", "keywords"}:
            continue
        matched.append(MatchedEvidence(
            kind=(
                "TRANSCRIPT"
                if item.field == "dialogue"
                else "VISUAL_EVENT"
                if item.field in {"event_description", "screen_text"}
                else "CONTEXT"
            ),
            text=item.raw_text,
            start_seconds=item.start_seconds,
            end_seconds=item.end_seconds,
        ))
        if len(matched) == 2:
            break

    if has_text:
        semantic_text = evidence.retrieval_text.strip() or evidence.window_summary.strip()
        if semantic_text:
            matched.append(MatchedEvidence(
                kind="CONTEXT",
                text=semantic_text,
                start_seconds=evidence.start_seconds,
                end_seconds=evidence.end_seconds,
            ))
    if has_vision:
        visual = _first_visual_evidence(evidence)
        if visual is not None:
            matched.append(visual)
    if not matched:
        matched.append(MatchedEvidence(
            kind="CONTEXT",
            text=evidence.window_summary,
            start_seconds=evidence.start_seconds,
            end_seconds=evidence.end_seconds,
        ))

    fact_summary = "、".join(
        (
            f"“{item.text[:57].rstrip()}…”"
            if len(item.text) > 60
            else f"“{item.text}”"
        )
        for item in matched[:2]
    )
    channel_labels = [
        label
        for present, label in (
            (has_lexical, "BM25 字面"),
            (has_text, "文本语义"),
            (has_vision, "视觉"),
        )
        if present
    ]
    limitations = [
        message
        for condition, message in (
            (not lexical_enabled, "本次检索策略未启用 BM25 字面通道。"),
            (
                lexical_enabled and not has_lexical,
                "BM25 字面通道已启用，但该片段未进入该通道候选。",
            ),
            (not text_enabled, "本次检索策略未启用文本语义通道。"),
            (
                text_enabled and not has_text,
                "文本语义通道已启用，但该片段未进入该通道候选。",
            ),
            (not vision_enabled, "本次检索策略未启用视觉通道。"),
            (
                vision_enabled and not has_vision,
                "视觉通道已启用，但该片段未进入该通道候选。",
            ),
            (
                any(item.field == "questions" for item in lexical_matches),
                "questions 字段仅用于召回线索，不作为对白或事实证据。",
            ),
        )
        if condition
    ]
    return SelectionReason(
        summary=(
            f"该片段进入{'、'.join(channel_labels)}通道，融合排序第 "
            f"{candidate.fused_rank}；返回证据包含 {fact_summary}，保留它用于核验。"
        ),
        matched_evidence=matched,
        limitations=limitations,
        lexical_matches=lexical_matches,
    )


def _selection_reason(
    candidate: VideoChunkCandidate,
    evidence: VideoChunkEvidence,
    *,
    transcript_match: RankedTranscriptMatch | None,
    transcript_enabled: bool,
    text_enabled: bool,
    vision_enabled: bool,
) -> SelectionReason:
    has_transcript = candidate.transcript_rank is not None
    has_text = candidate.text_rank is not None
    has_vision = candidate.vision_rank is not None
    matched: list[MatchedEvidence] = []

    if has_transcript:
        if transcript_match is None:
            raise RetrievalIndexConsistencyError(
                "fused Transcript rank has no matching ASR evidence"
            )
        matched.append(
            MatchedEvidence(
                kind="TRANSCRIPT",
                text=transcript_match.matched_text,
                start_seconds=transcript_match.start_seconds,
                end_seconds=transcript_match.end_seconds,
            )
        )

    if has_text:
        semantic_text = evidence.video_compact.strip() or evidence.retrieval_text.strip()
        if semantic_text:
            matched.append(
                MatchedEvidence(
                    kind="CONTEXT",
                    text=semantic_text,
                    start_seconds=evidence.start_seconds,
                    end_seconds=evidence.end_seconds,
                )
            )

    if has_vision:
        visual = _first_visual_evidence(evidence)
        if visual is not None:
            matched.append(visual)

    if not matched:
        matched.append(
            MatchedEvidence(
                kind="CONTEXT",
                text=evidence.window_summary.strip() or evidence.retrieval_text.strip(),
                start_seconds=evidence.analysis_window_start_seconds,
                end_seconds=evidence.analysis_window_end_seconds,
            )
        )

    fact_summary = "、".join(
        (
            f"“{item.text[:57].rstrip()}…”"
            if len(item.text) > 60
            else f"“{item.text}”"
        )
        for item in matched[:2]
    )
    channel_labels = [
        label
        for present, label in (
            (has_transcript, "ASR 原话"),
            (has_text, "文本语义"),
            (has_vision, "视觉"),
        )
        if present
    ]
    summary = (
        f"该片段进入{'、'.join(channel_labels)}通道，融合排序第 "
        f"{candidate.fused_rank}；返回证据包含 {fact_summary}，保留它用于核验。"
    )

    limitations = [
        message
        for condition, message in (
            (not transcript_enabled, "本次检索策略未启用 ASR 原话通道。"),
            (
                transcript_enabled and not has_transcript,
                "ASR 原话通道已启用，但该片段未命中规范化后的连续原话。",
            ),
            (not text_enabled, "本次检索策略未启用文本语义通道。"),
            (
                text_enabled and not has_text,
                "文本语义通道已启用，但该片段未进入该通道候选，需要重点核验对白与语义。",
            ),
            (not vision_enabled, "本次检索策略未启用视觉通道。"),
            (
                vision_enabled and not has_vision,
                "视觉通道已启用，但该片段未进入该通道候选，需要重点核验实际画面。",
            ),
            (evidence.is_fallback, "该片段来自降级解析，证据粒度可能较粗。"),
        )
        if condition
    ]
    return SelectionReason(
        summary=summary,
        matched_evidence=matched,
        limitations=limitations,
    )


def _first_visual_evidence(
    evidence: VideoChunkEvidence,
) -> MatchedEvidence | None:
    for event in evidence.all_time_events:
        text = event.get("description")
        start_seconds = event.get("start_seconds")
        end_seconds = event.get("end_seconds")
        if (
            isinstance(text, str)
            and text.strip()
            and isinstance(start_seconds, int)
            and not isinstance(start_seconds, bool)
            and isinstance(end_seconds, int)
            and not isinstance(end_seconds, bool)
            and end_seconds > start_seconds
        ):
            return MatchedEvidence(
                kind="VISUAL_EVENT",
                text=text.strip(),
                start_seconds=start_seconds,
                end_seconds=end_seconds,
            )
    text = evidence.baseline_visual_caption or evidence.all_time_event_text
    if text.strip():
        return MatchedEvidence(
            kind="VISUAL_EVENT",
            text=text.strip(),
            start_seconds=evidence.start_seconds,
            end_seconds=evidence.end_seconds,
        )
    return None


def _agent_reply(hits: Sequence[VideoChunkSearchHit]) -> AgentReply:
    count = len(hits)
    if count == 0:
        return AgentReply(
            message="这次没有找到符合当前检索范围的候选片段。",
            result_count=0,
        )
    return AgentReply(
        message=(
            f"我保留了 {count} 段备选。第一段的选择依据是："
            f"{hits[0].selection_reason.summary}"
        ),
        result_count=count,
    )
