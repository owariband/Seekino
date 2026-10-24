from __future__ import annotations

import importlib
import json
import subprocess
import sys
from pathlib import Path

import pytest


_PUBLIC_MODULES = [
    ("engine.rag.contracts", [
        "VideoChunkRetrievalIdentity", "LexicalMatch", "VideoChunkSearchResult",
    ]),
    ("engine.rag.encoding", [
        "EmbeddingReuseTarget", "assess_embedding_reuse", "embedding_receipt",
        "EncoderContract", "EmbeddingArtifact", "SparseEncoderContract", "compute_encoder_id",
    ]),
    ("engine.rag.vision.inputs", [
        "OrderedFramesInputContract", "VideoClipManifest", "plan_ordered_frame_timestamps",
    ]),
    ("engine.rag.indexing.pipeline", [
        "ChunkIndexingContract", "VideoChunkIndexDocument", "plan_chunk_index", "finalize_chunk_index",
    ]),
    ("engine.rag.indexing.projection", [
        "LexicalField", "project_chunk_index", "compute_index_projection_revision",
    ]),
    ("engine.rag.query_pipeline", ["plan_retrieval_query"]),
    ("engine.rag.nlp.query", [
        "accept_query_vector", "reuse_query_vector",
    ]),
    ("engine.rag.retrieval.contracts", [
        "RetrievalQueryPlan", "RawChunkHit", "ChunkRetrievalFacts", "RetrievalEvidenceResult",
        "EvidenceRequest",
    ]),
    ("engine.rag.retrieval.pipeline", [
        "build_recall_requests", "fuse_retrieval_candidates",
    ]),
    ("engine.rag.retrieval.evidence", [
        "build_evidence_requests", "verify_candidate_evidence", "verify_retrieval_evidence",
    ]),
    ("engine.rag.retrieval.rerank", [
        "RerankScorerContract", "RerankItem", "RerankRequest", "RerankPlan", "RerankResult",
        "plan_candidate_rerank", "accept_rerank_result", "finalize_candidate_rerank",
    ]),
    ("engine.rag.retrieval.fusion", [
        "VersionedRankedVideoChunkMatch", "fuse_versioned_video_chunk_rankings",
    ]),
]


@pytest.mark.parametrize("rag_first", [False, True], ids=["module-first", "rag-first"])
def test_public_modules_import_without_v1_or_legacy_in_a_fresh_process(rag_first: bool) -> None:
    script = """
import importlib
import importlib.util
import json
import sys

modules = json.loads(sys.argv[1])
rag_first = json.loads(sys.argv[2])
if rag_first:
    importlib.import_module("engine.rag")
for module_path, names in modules:
    module = importlib.import_module(module_path)
    for name in names:
        assert hasattr(module, name), (module_path, name)
importlib.import_module("engine.rag")
assert not any(name == "engine.parsev1" or name.startswith("engine.parsev1.") for name in sys.modules)
assert importlib.util.find_spec("engine.retrieval") is None
"""
    result = subprocess.run(
        [sys.executable, "-c", script, json.dumps(_PUBLIC_MODULES), json.dumps(rag_first)],
        cwd=Path(__file__).parents[2], capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_current_top_level_exports_remain_available() -> None:
    canonical = importlib.import_module("engine.rag")
    required = {
        "ChunkIndexSource", "ChunkIndexingContract", "EncoderContract", "EmbeddingArtifact",
        "EmbeddingRequest", "VideoChunkIndexDocument", "OrderedFramesInputContract",
        "VideoClipInputContract", "VideoClipManifest", "VIDEO_CLIP_PREPROCESS_REVISION",
        "LexicalField", "ChunkIndexProjection", "accept_embedding_result", "compute_encoder_id",
        "compute_source_revision", "compute_index_projection_revision", "embedding_receipt",
        "finalize_chunk_index", "plan_chunk_index", "project_chunk_index",
        "validate_embedding_artifact", "plan_retrieval_query", "verify_retrieval_evidence",
        "EvidenceRequest", "build_evidence_requests",
        "verify_candidate_evidence", "RerankScorerContract", "RerankItem", "RerankRequest",
        "RerankPlan", "RerankResult", "plan_candidate_rerank", "accept_rerank_result",
        "finalize_candidate_rerank",
    }
    assert required <= set(canonical.__all__)
    for name in canonical.__all__:
        assert hasattr(canonical, name)
    for module_path, names in _PUBLIC_MODULES:
        module = importlib.import_module(module_path)
        for name in names:
            if name in canonical.__all__:
                assert getattr(canonical, name) is getattr(module, name)


def test_chunk_embedding_cache_helpers_are_removed_from_public_api() -> None:
    removed = {
        "reuse_embedding_artifact", "reuse_sparse_embedding_artifact", "sparse_embedding_receipt",
    }
    for module_path in ("engine.rag", "engine.rag.encoding"):
        module = importlib.import_module(module_path)
        assert removed.isdisjoint(module.__all__)
        for name in removed:
            assert not hasattr(module, name)


def test_projection_version_patch_changes_canonical_fingerprint(monkeypatch) -> None:
    canonical = importlib.import_module("engine.rag.indexing.projection")
    projection = canonical.ChunkIndexProjection(lexical_fields=[], semantic_text="")
    before = canonical.compute_index_projection_revision(projection)
    monkeypatch.setattr(canonical, "INDEX_PROJECTION_VERSION", "rag-api-test-version")
    after = canonical.compute_index_projection_revision(projection)
    assert after != before


def test_persisted_identity_uses_content_version_and_generation_and_search_json_round_trips() -> None:
    canonical = importlib.import_module("engine.rag.contracts")
    payload = {
        "tenantId": "workspace", "seriesId": "series", "videoId": "video",
        "parseTaskId": "task", "contentVersion": 2, "indexGeneration": 1,
    }
    identity = canonical.VideoChunkRetrievalIdentity.model_validate(payload)
    assert identity.model_dump(mode="json", by_alias=True) == payload
    encoded = identity.model_dump_json(by_alias=True)
    assert canonical.VideoChunkRetrievalIdentity.model_validate_json(encoded) == identity

    search_payload = {
        "seriesIds": ["series"], "agentReply": {"message": "没有命中", "resultCount": 0},
        "hits": [], "searchedVideoIds": ["video"], "partial": False, "warnings": [],
        "query": "我没事",
    }
    result = canonical.VideoChunkSearchResult.model_validate(search_payload)
    assert result.model_dump(mode="json", by_alias=True) == search_payload
    assert canonical.VideoChunkSearchResult.model_validate_json(
        result.model_dump_json(by_alias=True),
    ) == result
