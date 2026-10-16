from __future__ import annotations

from collections.abc import Sequence
from contextlib import nullcontext
from hashlib import sha256
import json
import logging
from typing import Protocol

from engine.rag import EncoderContract, VIDEO_CLIP_PREPROCESS_REVISION
from engine.rag.encoding import SparseEncoderContract, accept_sparse_result
from engine.rag.indexing.pipeline import ChunkIndexingContract
from engine.rag.nlp.query import accept_query_vector
from engine.rag.query_pipeline import plan_retrieval_query
from engine.rag.retrieval.contracts import (
    RetrievalConsistencyError, RetrievalPolicy, RetrievalScope, SelectedVideoTask,
    QueryEncoderBinding, VerifiedRetrievalCandidate,
)
from engine.rag.retrieval.pipeline import build_recall_requests, fuse_retrieval_candidates
from engine.rag.retrieval.rerank import plan_candidate_rerank, finalize_candidate_rerank
from engine.rag.vision.inputs import VideoClipInputContract
from engine.rag.contracts import LexicalMatch
from gateway.embedding import EmbeddingResult
from gateway.model_sources import ModelReference
from gateway.tracing import LangfuseTracing
from gateway.usage import ModelCallControlError, model_call_context

from ..config.retrieval import RetrievalConfig
from ..constants.parse import MAX_INDEX_GENERATION
from ..repository.video_parse import (
    VideoParseConflictError,
    VideoParseRepository,
)
from ..schema import (
    AgentReply,
    MatchedEvidence,
    SelectionReason,
    VideoChunkCandidate,
    VideoChunkSearchHit,
    VideoChunkSearchResult,
)
from ..storage.elasticsearch import (
    ElasticsearchDocumentConsistencyError,
    ElasticsearchVideoChunkStore,
)
from .retrieval_rerank import StructuredTextReranker

_DEFAULT_RETRIEVAL_CONFIG = RetrievalConfig()
_logger = logging.getLogger(__name__)


class QueryEmbedder(Protocol):
    def embed_query(self, query: str) -> EmbeddingResult: ...


class RetrievalIndexConsistencyError(RuntimeError):
    """Retrieved candidates do not match the current MySQL index facts."""


class VideoParseRetrievalService:
    """Execute Engine recall, verify current facts, then score and select top_k."""

    def __init__(
        self, repository: VideoParseRepository, store: ElasticsearchVideoChunkStore,
        embedder: QueryEmbedder, *, semantic_encoder: EncoderContract,
        expected_index_generation: int,
        expected_embedding_source: str | None = None,
        channel_top_k: int = _DEFAULT_RETRIEVAL_CONFIG.channel_top_k,
        rrf_k: int = _DEFAULT_RETRIEVAL_CONFIG.rrf_k,
        lexical_weight: float = _DEFAULT_RETRIEVAL_CONFIG.lexical_weight,
        text_weight: float = _DEFAULT_RETRIEVAL_CONFIG.text_weight,
        vision_weight: float = _DEFAULT_RETRIEVAL_CONFIG.vision_weight,
        candidate_budget: int | None = _DEFAULT_RETRIEVAL_CONFIG.candidate_budget,
        sparse_weight: float = 0.0,
        reranker: StructuredTextReranker | None = None,
        exact: bool = False, tracing: LangfuseTracing | None = None,
    ) -> None:
        if (store.semantic_dimensions != store.vision_dimensions
                or store.semantic_dimensions != semantic_encoder.dimensions):
            raise ValueError("query encoder and both store dimensions must match")
        if (isinstance(expected_index_generation, bool)
                or not isinstance(expected_index_generation, int)
                or not 1 <= expected_index_generation <= MAX_INDEX_GENERATION):
            raise ValueError("expected_index_generation must fit the Elasticsearch version contract")
        if not isinstance(exact, bool):
            raise ValueError("exact must be a bool")
        if expected_embedding_source is not None and (
                not isinstance(expected_embedding_source, str) or not expected_embedding_source.strip()):
            raise ValueError("expected_embedding_source must be nonblank when supplied")
        self._policy = RetrievalPolicy(
            lexical_weight=lexical_weight, semantic_weight=text_weight,
            vision_weight=vision_weight, sparse_weight=sparse_weight,
            channel_top_k=channel_top_k,
            candidate_budget=channel_top_k if candidate_budget is None else candidate_budget,
            top_k=1, rrf_k=rrf_k,
        )
        self._repository, self._store, self._embedder = repository, store, embedder
        self._semantic_encoder = semantic_encoder
        self._candidate_budget = candidate_budget
        self._expected_index_generation = expected_index_generation
        self._expected_embedding_source = expected_embedding_source
        self._reranker, self._tracing, self._exact = reranker, tracing, exact

    def search(
        self, query: str, *, tenant_id: str, series_ids: Sequence[str],
        top_k: int = 10, lexical_weight: float | None = None,
        transcript_weight: float | None = None, text_weight: float | None = None,
        vision_weight: float | None = None,
    ) -> VideoChunkSearchResult:
        if (not isinstance(tenant_id, str) or not tenant_id
                or tenant_id != tenant_id.strip()):
            raise ValueError("tenant_id must be a non-empty string")
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must not be empty")
        if isinstance(top_k, bool) or not isinstance(top_k, int) or top_k <= 0:
            raise ValueError("top_k must be a positive integer")
        if transcript_weight is not None:
            raise ValueError("Video Parse V2 retrieval uses lexical_weight, not transcript_weight")
        overrides = (lexical_weight, text_weight, vision_weight)
        if any(value is not None for value in overrides) and not all(
                value is not None for value in overrides):
            raise ValueError("lexical_weight, text_weight and vision_weight must be provided together")
        policy = RetrievalPolicy.model_validate(self._policy.model_copy(update={
            "top_k": top_k,
            "candidate_budget": max(top_k, self._policy.candidate_budget),
            **({"lexical_weight": lexical_weight, "semantic_weight": text_weight,
                "vision_weight": vision_weight} if lexical_weight is not None else {}),
        }))
        if self._candidate_budget is None:
            policy = policy.model_copy(update={
                "candidate_budget": max(
                    top_k,
                    policy.channel_top_k * sum(weight > 0 for weight in policy.weights.values()),
                ),
            })
        query = query.strip()
        try:
            selected = self._repository.resolve_retrieval_scope(
                series_ids, tenant_id=tenant_id,
                **({"require_sparse": True} if policy.sparse_weight else {}),
            )
        except VideoParseConflictError as error:
            raise RetrievalIndexConsistencyError(str(error)) from error
        if selected.index_generation != self._expected_index_generation:
            raise RetrievalIndexConsistencyError("selected V2 task index generation differs from the active Elasticsearch generation")
        embedding = selected.parse_config.models.embedding
        if (policy.semantic_weight or policy.vision_weight or policy.sparse_weight) and (
                self._expected_embedding_source is not None
                and embedding.source != self._expected_embedding_source):
            raise RetrievalIndexConsistencyError("selected V2 task embedding source differs from the active query source")
        try:
            reference = ModelReference.resolve(
                embedding.model, encoder_model=embedding.encoder_model,
                default_encoder_model=self._semantic_encoder.model,
            )
        except ValueError as error:
            raise RetrievalIndexConsistencyError(str(error)) from error
        if (policy.semantic_weight or policy.vision_weight or policy.sparse_weight) and (
                reference.model != self._semantic_encoder.model
                or embedding.dimensions != self._semantic_encoder.dimensions):
            raise RetrievalIndexConsistencyError("selected V2 task embedding model or dimensions differ from the active query encoder contract")
        contract = ChunkIndexingContract(
            retrieval_contract_id="parse-v2-retrieval-v1",
            semantic_encoder=self._semantic_encoder, vision_encoder=self._semantic_encoder,
            visual_input_contract=VideoClipInputContract(
                fps=embedding.video_fps, max_height=selected.parse_config.visual_input.max_height,
                mime_type="video/mp4", preprocess_revision=VIDEO_CLIP_PREPROCESS_REVISION,
            ), index_generation=selected.index_generation,
            sparse_encoder=SparseEncoderContract(
                provider=self._semantic_encoder.provider, model=reference.model,
                encoder_revision=self._semantic_encoder.encoder_revision,
            ) if policy.sparse_weight else None,
        )
        scope = RetrievalScope(tenant_id=tenant_id, tasks=tuple(
            SelectedVideoTask(series_id=selected.series_by_video_id[video_id],
                video_id=video_id, parse_task_id=task_id, status="COMPLETE")
            for video_id, task_id in selected.parse_tasks_by_video_id.items()
        ))
        bindings = tuple(QueryEncoderBinding(
            channel=channel, document_encoder_id=getattr(contract, f"{channel}_encoder_id"),
            query_encoder=self._semantic_encoder,
            compatibility_provenance="Ark multimodal vectorization: shared text/image/video embedding space",
        ) for channel in ("semantic", "vision") if policy.weights[channel])
        plan = plan_retrieval_query(query, scope=scope, index_contract=contract,
                                    policy=policy, encoder_bindings=bindings)
        context = {
            "tenant_id": tenant_id, "series_ids": selected.series_ids,
            "video_ids": selected.searched_video_ids,
            "video_parse_task_ids": tuple(selected.parse_tasks_by_video_id.values()),
            "index_generation": selected.index_generation, "stage": "retrieval",
        }
        tracing = self._tracing.scope(context) if self._tracing is not None else nullcontext()
        try:
            with model_call_context(context), tracing:
                vectors = []
                for request in plan.encoding_requests:
                    result = self._embedder.embed_query(request.text)
                    if result.modality != "text":
                        raise RetrievalConsistencyError("query embedder returned a non-text embedding")
                    vectors.append(accept_query_vector(request, model=result.model, vector=result.vector))
                sparse = None
                if plan.sparse_request is not None:
                    result = self._embedder.embed_sparse(plan.sparse_request.text)
                    sparse = accept_sparse_result(plan.sparse_request, model=result.model, weights=result.weights)
            requests = build_recall_requests(plan, vectors=vectors, sparse=sparse)
            raw_hits = tuple(hit for request in requests for hit in self._store.recall(request, exact=self._exact))
            candidates = fuse_retrieval_candidates(plan, raw_hits)
            hydrated = self._repository.hydrate_retrieval_candidates([
                (item.ranking.video_chunk_id, item.ranking.retrieval_identity) for item in candidates
            ]) if candidates else ()
            rerank_plan = plan_candidate_rerank(
                plan, candidates, [item.facts for item in hydrated],
                scorer=self._reranker.contract if self._reranker is not None else None,
                contexts={item.facts.video_chunk_id: item.contexts for item in hydrated},
            )
        except (VideoParseConflictError, ElasticsearchDocumentConsistencyError, ValueError) as error:
            raise RetrievalIndexConsistencyError(str(error)) from error
        scores = None
        if rerank_plan.request is not None:
            scoring_context = {**context, "stage": "retrieval_rerank"}
            tracing = self._tracing.scope(scoring_context) if self._tracing is not None else nullcontext()
            try:
                with model_call_context(scoring_context), tracing:
                    scores = self._reranker.score(rerank_plan.request)
            except ModelCallControlError:
                raise
            except Exception as error:
                logging.getLogger(__name__).warning(
                    "Retrieval scorer failed; using RRF: candidates=%d error=%s",
                    len(rerank_plan.request.items), type(error).__name__,
                )
        verified = finalize_candidate_rerank(rerank_plan, scores)
        displays = {item.facts.video_chunk_id: item.evidence for item in hydrated}
        hits = []
        for item in verified.accepted:
            ranking = item.candidate.ranking
            candidate = VideoChunkCandidate.model_validate(
                ranking.model_dump(include=set(VideoChunkCandidate.model_fields)),
            )
            if item.candidate.lexical_matches:
                _logger.info(
                    "event=video_retrieval_lexical_matches query_sha256=%s "
                    "video_chunk_id=%s lexical_rank=%s lexical_score=%s "
                    "retrieval_identity=%s matched_field_count=%s matched_fields=%s",
                    sha256(query.encode("utf-8")).hexdigest()[:16],
                    ranking.video_chunk_id, candidate.lexical_rank, candidate.lexical_score,
                    json.dumps(ranking.retrieval_identity.model_dump(mode="json"),
                               ensure_ascii=False, separators=(",", ":")),
                    len(item.candidate.lexical_matches),
                    json.dumps([
                        {
                            **source.model_dump(mode="json", exclude={"raw_text"}),
                            "text_preview": source.raw_text[:256],
                            "text_chars": len(source.raw_text),
                            "text_sha256": sha256(source.raw_text.encode("utf-8")).hexdigest()[:16],
                        }
                        for source in item.candidate.lexical_matches
                    ], ensure_ascii=False, separators=(",", ":")),
                )
            hits.append(VideoChunkSearchHit(
                candidate=candidate, selection_reason=_v2_selection_reason(item),
                evidence=displays[ranking.video_chunk_id],
            ))
        return VideoChunkSearchResult(
            query=query, series_ids=list(selected.series_ids),
            searched_video_ids=list(selected.searched_video_ids), partial=selected.partial,
            warnings=list(selected.warnings), agent_reply=_agent_reply(hits), hits=hits,
        )


def _v2_selection_reason(verified: VerifiedRetrievalCandidate) -> SelectionReason:
    """Display only Engine-verified source evidence and its stated granularity."""

    matched = []
    for proof in verified.evidence:
        source = proof.source
        matched.append(MatchedEvidence(
            kind=("TRANSCRIPT" if source is not None and source.field == "dialogue"
                  else "VISUAL_EVENT" if source is not None and source.field in {"event_description", "screen_text"}
                  else "CONTEXT"),
            text=source.raw_text if source is not None else "匹配结果定位到此 Chunk。",
            start_seconds=proof.start_seconds, end_seconds=proof.end_seconds,
        ))
    lexical = [LexicalMatch.model_validate(item) for item in verified.candidate.lexical_matches]
    limitations = []
    if any(proof.granularity == "chunk" for proof in verified.evidence):
        limitations.append("此结果只定位到 Chunk，尚未定位到具体事件或帧。")
    if any(source.field in {"questions", "keywords"} for source in lexical):
        limitations.append("questions / keywords 只用于召回线索，不作为事件证据。")
    return SelectionReason(
        summary="返回范围和证据已与当前 Chunk 的正式内容核对。",
        matched_evidence=matched, limitations=limitations, lexical_matches=lexical,
    )


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
