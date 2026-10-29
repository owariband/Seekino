"""Local ASR-text indexing pilot; this does not execute or complete video parsing.

Run with ``python -m evals.local_text_embedding --config ... --dataset ...
--output ... --es-url http://127.0.0.1:9200``. Each dataset query expects its
containing sample. Source paths and millisecond ranges come from the supplied
transcript dataset; OTHER events below are explicit manual projections.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Annotated, Any
from urllib.parse import urlsplit
from uuid import UUID, uuid4, uuid5

from elasticsearch import Elasticsearch
from pydantic import BaseModel, ConfigDict, Field, model_validator

from engine.parsev2.schemas import VideoParseResult
from engine.rag.encoding import EncoderContract, accept_embedding_result
from engine.rag.indexing.events import (
    EventIndexingContract, finalize_event_index, plan_event_index,
)
from engine.rag.indexing.pipeline import (
    ChunkIndexSource, ChunkIndexingContract, finalize_chunk_index, plan_chunk_index,
)
from engine.rag.nlp.query import accept_query_vector
from engine.rag.query_pipeline import plan_retrieval_query
from engine.rag.retrieval.contracts import (
    ChunkRetrievalFacts, EventFilters, QueryEncoderBinding, RetrievalPolicy,
    RetrievalScope, SelectedVideoTask,
)
from engine.rag.retrieval.evidence import verify_retrieval_evidence
from engine.rag.retrieval.events import fuse_event_hits, verify_event_candidate
from engine.rag.retrieval.pipeline import build_recall_requests, fuse_retrieval_candidates
from gateway.config import load_config
from gateway.model_factory import GatewayClient
from server.storage.elasticsearch import ElasticsearchVideoChunkStore
from server.storage.elasticsearch_events import ElasticsearchVideoEventStore

NonBlank = Annotated[str, Field(min_length=1, pattern=r"\S")]


class Sample(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: NonBlank
    source_path: NonBlank
    start_ms: int = Field(ge=0, strict=True)
    end_ms: int = Field(gt=0, strict=True)
    text: NonBlank
    queries: tuple[NonBlank, ...] = Field(min_length=1)
    asr_seconds: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    audio_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    source_bytes: int | None = Field(default=None, gt=0, strict=True)

    @model_validator(mode="after")
    def validate_bounds(self) -> Sample:
        if self.end_ms <= self.start_ms:
            raise ValueError("sample end_ms must be greater than start_ms")
        return self


class Dataset(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    samples: tuple[Sample, ...] = Field(min_length=2)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_ids(self) -> Dataset:
        if len({sample.id for sample in self.samples}) != len(self.samples):
            raise ValueError("sample IDs must be unique")
        return self


def load_dataset(path: Path) -> Dataset:
    dataset = Dataset.model_validate_json(path.read_text(encoding="utf-8"))
    resolved = []
    for sample in dataset.samples:
        source = Path(sample.source_path).expanduser()
        source = (path.parent / source if not source.is_absolute() else source).resolve()
        if not source.is_file():
            raise ValueError(f"sample {sample.id} source file does not exist: {source}")
        if sample.source_bytes is not None and source.stat().st_size != sample.source_bytes:
            raise ValueError(f"sample {sample.id} source file size differs from the ASR provenance")
        resolved.append(sample.model_copy(update={"source_path": str(source)}))
    return dataset.model_copy(update={"samples": tuple(resolved)})


def require_loopback_url(value: str) -> str:
    parsed = urlsplit(value)
    if (parsed.scheme not in {"http", "https"} or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise ValueError("this pilot only accepts credential-free loopback service URLs")
    return value


@dataclass
class MeasuredEmbedder:
    model: Any
    calls: list[dict[str, Any]] = field(default_factory=list)

    def encode(self, operation: str, text: str):
        if operation not in {"text", "query"}:
            raise ValueError("this pilot forbids visual and generative model calls")
        started = perf_counter()
        record = {"operation": operation, "input_chars": len(text),
                  "input_sha256": hashlib.sha256(text.encode()).hexdigest()}
        try:
            result = getattr(self.model, f"embed_{operation}")(text)
            record.update(status="completed", dimensions=len(result.vector), model=result.model)
            return result
        except Exception as error:
            record.update(status="failed", error_type=type(error).__name__)
            raise
        finally:
            record["elapsed_ms"] = round((perf_counter() - started) * 1000, 3)
            self.calls.append(record)


def build_documents(dataset: Dataset, namespace: UUID, encoder: EncoderContract, measured: MeasuredEmbedder):
    """Project supplied text, preserving source times without claiming VLM output."""
    contract = ChunkIndexingContract(
        retrieval_contract_id="local-asr-text-pilot-v1", semantic_encoder=encoder,
        vision_encoder=None, visual_input_contract=None,
    )
    event_contract = EventIndexingContract(semantic_encoder=encoder, vision_encoder=None, capture=None)
    series_id = str(uuid5(namespace, "series"))
    chunks, events, facts, sources, tasks = [], [], {}, {}, {}
    for sequence, sample in enumerate(dataset.samples):
        video_id = str(uuid5(namespace, "video:" + sample.source_path))
        task_id = str(uuid5(namespace, "task:" + sample.source_path))
        chunk_id = str(uuid5(namespace, "chunk:" + sample.id))
        result = VideoParseResult.model_validate({"persons": [], "timeline": [{
            "event_type": "OTHER", "start_seconds": sample.start_ms, "end_seconds": sample.end_ms,
            "description": sample.text, "participants": [], "visible_persons": [],
        }]})
        source = ChunkIndexSource(
            tenant_id="local-text-pilot-test", series_id=series_id, video_id=video_id,
            video_chunk_id=chunk_id, parse_task_id=task_id, content_version=1,
            chunk_start_seconds=sample.start_ms, chunk_end_seconds=sample.end_ms,
            result=result, visual_input=None,
        )
        plan = plan_chunk_index(source, contract)
        artifacts = []
        for request in plan.encoding_requests:
            response = measured.encode("text", request.semantic_text)
            artifacts.append(accept_embedding_result(request, model=response.model,
                                                     modality=response.modality, vector=response.vector))
        document = finalize_chunk_index(plan, artifacts)
        chunks.append(document)
        for event_plan in plan_event_index(task_id, chunk_id, 1, result,
                                            sample.start_ms, sample.end_ms, event_contract):
            semantic = []
            for item in event_plan.semantic_requests:
                response = measured.encode("text", item.request.semantic_text)
                semantic.append(accept_embedding_result(item.request, model=response.model,
                                                        modality=response.modality, vector=response.vector))
            events.append(finalize_event_index(event_plan, semantic, None, ()))
        facts[chunk_id] = ChunkRetrievalFacts(
            video_chunk_id=chunk_id, retrieval_identity=document.retrieval_identity,
            sequence_no=sequence, start_seconds=sample.start_ms, end_seconds=sample.end_ms, result=result,
        )
        sources[chunk_id] = sample
        # This is an isolated fixture scope, not a persisted VideoParseTask status.
        tasks[video_id] = SelectedVideoTask(series_id=series_id, video_id=video_id,
                                           parse_task_id=task_id, status="COMPLETE")
    scope = RetrievalScope(tenant_id="local-text-pilot-test", tasks=tuple(tasks.values()))
    return contract, event_contract, chunks, events, facts, sources, scope


def source_result(sample: Sample, *, start_ms: int, end_ms: int) -> dict[str, Any]:
    if (start_ms, end_ms) != (sample.start_ms, sample.end_ms):
        raise ValueError("retrieved source times differ from the supplied transcript segment")
    return {"sample_id": sample.id, "source_path": sample.source_path,
            "start_ms": start_ms, "end_ms": end_ms, "source_verified": True}


def query_trial(dataset, measured, chunks, events, contract, event_contract, facts, sources, scope, top_k):
    results = []
    channel_top_k = max(top_k, min(20, len(sources)))
    policy = RetrievalPolicy(lexical_weight=1., semantic_weight=1., vision_weight=0.,
                             channel_top_k=channel_top_k, candidate_budget=channel_top_k * 2,
                             top_k=top_k, rrf_k=60)
    binding = QueryEncoderBinding(channel="semantic", document_encoder_id=contract.semantic_encoder_id,
                                  query_encoder=contract.semantic_encoder,
                                  compatibility_provenance="same frozen local text encoder and query instruction")
    event_scope = [(fact.retrieval_identity.parse_task_id, key, fact.retrieval_identity.content_version)
                   for key, fact in facts.items()]
    for sample in dataset.samples:
        for query in sample.queries:
            started = perf_counter()
            query_plan = plan_retrieval_query(query, scope=scope, index_contract=contract,
                                              policy=policy, encoder_bindings=(binding,))
            response = measured.encode("query", query)
            vectors = [accept_query_vector(request, model=response.model, vector=response.vector)
                       for request in query_plan.encoding_requests]
            query_encode_ms = measured.calls[-1]["elapsed_ms"]
            search_started = perf_counter()
            raw = [hit for request in build_recall_requests(query_plan, vectors=vectors)
                   for hit in chunks.recall(request, exact=True)]
            candidates = fuse_retrieval_candidates(query_plan, raw)
            verified = verify_retrieval_evidence(query_plan, candidates,
                [facts[item.ranking.video_chunk_id] for item in candidates])
            if verified.rejected:
                raise ValueError("source verification rejected a Chunk candidate")
            chunk_hits = []
            for item in verified.accepted:
                candidate = item.candidate
                chunk_hits.append(source_result(sources[candidate.ranking.video_chunk_id],
                    start_ms=candidate.start_seconds, end_ms=candidate.end_seconds))
            chunk_search_ms = (perf_counter() - search_started) * 1000
            search_started = perf_counter()
            event_raw = [*events.search_lexical(query, sources=event_scope, filters=EventFilters(), limit=channel_top_k),
                         *events.search_vector(response.vector, channel="semantic", sources=event_scope,
                                               filters=EventFilters(), limit=channel_top_k, exact=True)]
            event_hits = []
            for hit in fuse_event_hits(event_raw, channel_weights={"lexical": 1., "semantic": 1., "vision": 0.})[:top_k]:
                hit = verify_event_candidate(hit, facts[hit.video_chunk_id], contract=event_contract)
                event_hits.append(source_result(sources[hit.video_chunk_id], start_ms=hit.start_ms, end_ms=hit.end_ms))
            event_search_ms = (perf_counter() - search_started) * 1000
            row = {"query": query, "expected_sample_id": sample.id, "query_embedding_ms": query_encode_ms,
                   "chunk_search_and_verify_ms": round(chunk_search_ms, 3),
                   "event_search_and_verify_ms": round(event_search_ms, 3),
                   "elapsed_ms": round((perf_counter() - started) * 1000, 3),
                   "chunk_top_k": chunk_hits, "event_top_k": event_hits}
            for kind, hits in (("chunk", chunk_hits), ("event", event_hits)):
                row[f"{kind}_hit_at_1"] = bool(hits and hits[0]["sample_id"] == sample.id)
                row[f"{kind}_hit_at_k"] = any(hit["sample_id"] == sample.id for hit in hits)
            results.append(row)
    return results


def run(config_path: Path, dataset_path: Path, output: Path, es_url: str, *, keep_indexes=False, top_k=3):
    started = perf_counter()
    run_id = uuid4()
    names = {kind: f"seekino_local_text_test_{run_id.hex}_{kind}" for kind in ("chunks", "events")}
    report = {"run_id": str(run_id), "started_at": datetime.now(UTC).isoformat(), "status": "running",
              "scope": "supplied ASR text -> manual OTHER events -> real local embedding -> isolated ES -> source verification",
              "database_task_completion_verified": False, "full_video_parse_verified": False,
              "ground_truth": "Each query expects its containing supplied sample; small development set, not held-out evaluation.",
              "indexes": names, "stages_ms": {}, "image_embedding_calls": 0, "video_embedding_calls": 0,
              "generative_model_calls": 0, "exact_vector_search": True, "top_k": top_k}
    es = gateway = measured = None
    created = []
    try:
        if not 1 <= top_k <= 100:
            raise ValueError("top_k must be between 1 and 100")
        require_loopback_url(es_url)
        dataset = load_dataset(dataset_path)
        report.update(dataset=str(dataset_path.resolve()), dataset_sha256=hashlib.sha256(dataset_path.read_bytes()).hexdigest(),
                      dataset_metadata=dataset.metadata, sample_count=len(dataset.samples))
        config = load_config(config_path)
        binding = config.resolve_embedding_binding(environ={})
        source = config.source(binding.source)
        if (source.adapter != "ollama_embedding" or binding.input_mode != "text" or binding.dimensions != 1024
                or binding.model != "qwen3-embedding:0.6b"):
            raise ValueError("pilot requires local qwen3-embedding:0.6b, text input mode and 1024 dimensions")
        require_loopback_url(source.base_url)
        gateway = GatewayClient(config, environ={})
        info = gateway.describe_embedding(binding)
        encoder = EncoderContract(provider=info.provider_name, model=info.encoder_model,
                                  encoder_revision=info.encoder_revision, dimensions=info.dimensions)
        report["encoder"] = encoder.claims()
        report["embedding_binding"] = binding.model_dump(mode="json", exclude_none=True)
        measured = MeasuredEmbedder(gateway.create_embedding_model(binding))
        stage = perf_counter()
        contract, event_contract, chunk_docs, event_docs, facts, sources, scope = build_documents(dataset, run_id, encoder, measured)
        report["stages_ms"]["projection_and_document_embedding"] = round((perf_counter() - stage) * 1000, 3)
        report["samples"] = [{"sample_id": sample.id, "source_path": sample.source_path,
                              "start_ms": sample.start_ms, "end_ms": sample.end_ms,
                              "asr_seconds": sample.asr_seconds, "audio_sha256": sample.audio_sha256,
                              "source_bytes": sample.source_bytes,
                              "text_sha256": hashlib.sha256(sample.text.encode()).hexdigest()} for sample in dataset.samples]
        es = Elasticsearch(es_url, request_timeout=60, max_retries=0)
        if not es.info()["version"]["number"].startswith("8.19."):
            raise ValueError("pilot requires Elasticsearch 8.19")
        chunks = ElasticsearchVideoChunkStore(es, index_name=names["chunks"], environment="test",
                                              semantic_dimensions=1024, vision_dimensions=1024, vision_enabled=False)
        events = ElasticsearchVideoEventStore(es, index_name=names["events"], environment="test",
                                              semantic_dimensions=1024, vision_dimensions=1024)
        stage = perf_counter()
        for name, mapping in ((names["chunks"], chunks._mapping()), (names["events"], events._mapping(event_contract))):
            if es.indices.exists(index=name):
                raise ValueError("refusing to use an existing experiment index")
            es.indices.create(index=name, mappings=mapping)
            created.append(name)
        chunks.ensure_index()
        events.bind_contract(event_contract)
        report["stages_ms"]["create_and_validate_indexes"] = round((perf_counter() - stage) * 1000, 3)
        stage = perf_counter()
        chunks.bulk_upsert(chunk_docs, refresh="wait_for")
        events.bulk_upsert(event_docs, refresh="wait_for")
        if not chunks.verify_ready(chunk_docs) or not events.verify_ready(event_docs):
            raise ValueError("published documents are not search-visible")
        report["stages_ms"]["publish_and_verify_ready"] = round((perf_counter() - stage) * 1000, 3)
        before = len(measured.calls)
        if not all(chunks.inspect_exact(chunk_docs).values()):
            raise ValueError("Chunk readback differs")
        if events.inspect([doc.event_id for doc in event_docs]) != {doc.event_id: doc for doc in event_docs}:
            raise ValueError("Event readback differs")
        report["readback_extra_embedding_calls"] = len(measured.calls) - before
        report["readiness"] = {"chunk_count": len(chunk_docs), "event_count": len(event_docs), "verified": True}
        stage = perf_counter()
        report["queries"] = query_trial(dataset, measured, chunks, events, contract, event_contract,
                                          facts, sources, scope, top_k)
        report["stages_ms"]["queries"] = round((perf_counter() - stage) * 1000, 3)
        total = len(report["queries"])
        report["metrics"] = {"query_count": total, **{
            f"{kind}_hit_at_{rank}": sum(row[f"{kind}_hit_at_{rank}"] for row in report["queries"]) / total
            for kind in ("chunk", "event") for rank in ("1", "k")}}
        report["status"] = "completed"
    except Exception as error:
        report.update(status="failed", error={"type": type(error).__name__, "message": str(error)})
        raise
    finally:
        cleanup_errors = []
        if es is not None:
            retain = keep_indexes and report["status"] == "completed"
            for name in created:
                if not retain:
                    try:
                        es.indices.delete(index=name)
                    except Exception as error:
                        cleanup_errors.append({"index": name, "error_type": type(error).__name__, "message": str(error)})
            report["indexes_retained"] = list(created) if retain else [item["index"] for item in cleanup_errors]
            report["cleanup_errors"] = cleanup_errors
            es.close()
        if measured is not None:
            report["embedding_calls"] = measured.calls
            report["embedding_call_count"] = len(measured.calls)
        if gateway is not None:
            gateway.close()
        report["elapsed_ms"] = round((perf_counter() - started) * 1000, 3)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--es-url", default="http://127.0.0.1:9200")
    parser.add_argument("--top-k", type=int, default=3)
    parser.add_argument("--keep-indexes", action="store_true")
    args = parser.parse_args()
    report = run(args.config, args.dataset, args.output, args.es_url,
                 keep_indexes=args.keep_indexes, top_k=args.top_k)
    print(json.dumps({"status": report["status"], "metrics": report["metrics"],
                      "output": str(args.output.resolve())}, ensure_ascii=False))


if __name__ == "__main__":
    main()
