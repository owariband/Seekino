"""Read-only live Event/Chunk comparison using production factories and current config."""
from __future__ import annotations

import dataclasses
import argparse
import json
import logging
import sys
import time
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from gateway.model_factory import GatewayClient
from gateway.usage import capture_model_usage
from server.bootstrap import prepare_service_configuration
from server.caller import base as caller
from server.common.retrieval import build_event_retrieval_service, build_retrieval_service
from server.config import ServerSettings
from server.config.parse_profile import resolve_event_indexing_contract
from server.repository.video_parse import VideoParseRepository

OUT = Path(__file__).resolve().parent / "retrieval"
SERIES = "836fa9f0-2bf4-50fb-9c64-75526fa1dea9"
TARGET = "ad1c7419-c11c-5280-9e2e-89456355bfe7"
QUERY = "真是高高在上的，出于哪一集？"
CURRENT = None
TARGET_EVENT = None


def plain(value):
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if dataclasses.is_dataclass(value):
        return {field.name: plain(getattr(value, field.name)) for field in dataclasses.fields(value)}
    if isinstance(value, Mapping):
        return {str(key): plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [plain(item) for item in value]
    return value


class Timed:
    def __init__(self, value, prefix):
        self.value, self.prefix = value, prefix

    def __getattr__(self, name):
        original = getattr(self.value, name)
        if not callable(original):
            return original

        def invoke(*args, **kwargs):
            before = time.perf_counter()
            stage = {"stage": self.prefix + "." + name}
            try:
                result = original(*args, **kwargs)
                if name in {"recall", "search_lexical", "search_vector"}:
                    stage["raw_candidates"] = plain(result)
                if name == "validate_event_coverage":
                    stage["sources"] = plain(result)
                return result
            except Exception as error:
                stage["error"] = {"type": type(error).__name__, "message": str(error)}
                raise
            finally:
                stage["elapsed_seconds"] = time.perf_counter() - before
                if CURRENT is not None:
                    CURRENT["stages"].append(stage)
        return invoke


def execute(label, service, query, **kwargs):
    global CURRENT
    record = {"label": label, "query": query, "parameters": kwargs,
              "captured_at": datetime.now(timezone.utc).isoformat(), "stages": [], "usage": []}
    CURRENT = record
    before = time.perf_counter()
    try:
        with capture_model_usage(lambda call: record["usage"].append(plain(call))):
            result = service.search(query, tenant_id="local-default", series_ids=[SERIES], top_k=10, **kwargs)
        record["status"] = "success"
        record["result"] = plain(result)
        chunks = result.chunk_result if hasattr(result, "chunk_result") else result
        record["target_rank"] = next((i for i, hit in enumerate(chunks.hits, 1)
                                       if hit.candidate.video_chunk_id == TARGET), None)
        if TARGET_EVENT is not None and hasattr(result, "matches"):
            record["target_event_rank"] = next((i for i, hit in enumerate(result.matches, 1)
                                               if hit.event_id == TARGET_EVENT), None)
        record["hit_summary"] = [{"rank": rank, "chunk_id": hit.candidate.video_chunk_id,
                                   "episode": hit.evidence.episode_order,
                                   "start_ms": hit.evidence.start_seconds, "end_ms": hit.evidence.end_seconds,
                                   "ranking": plain(hit.candidate)}
                                  for rank, hit in enumerate(chunks.hits, 1)]
    except Exception as error:
        record["status"] = "error"
        record["error"] = {"type": type(error).__name__, "message": str(error)}
    finally:
        record["elapsed_seconds"] = time.perf_counter() - before
        CURRENT = None
        (OUT / (label + ".json")).write_text(json.dumps(record, ensure_ascii=False, indent=2, default=str) + "\n")
    summary = {key: record[key] for key in ("label", "query", "status", "elapsed_seconds", "target_rank", "target_event_rank", "error") if key in record}
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    return summary


def main():
    global OUT, SERIES, TARGET, QUERY, TARGET_EVENT
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", type=Path)
    parser.add_argument("--actual-query", action="store_true")
    args = parser.parse_args()
    case = json.loads(args.case.read_text()) if args.case is not None else {}
    if case:
        SERIES, TARGET, QUERY = case["series_id"], case["target_chunk_id"], case["quote"]
        TARGET_EVENT = case["target_event_id"]
        OUT = Path(__file__).resolve().parent / case["retrieval_output_subdirectory"]
    OUT.mkdir(exist_ok=True)
    logging.basicConfig(filename=OUT / "runtime.log", level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = ServerSettings.from_environment()
    gateway, defaults = prepare_service_configuration(settings)
    resources = caller.init(settings, semantic_dimensions=defaults.embedding.dimensions,
                             vision_dimensions=defaults.embedding.dimensions)
    client = GatewayClient(gateway)
    try:
        repo = Timed(VideoParseRepository(resources.session_factory), "sql")
        common = dict(gateway_config=gateway, embedding_config=defaults.embedding,
                      retrieval_config=settings.retrieval, request_timeout_seconds=120,
                      gateway_client=client)
        chunk, encoder = build_retrieval_service(repo, Timed(resources.elasticsearch_store, "chunk.es"),
            index_generation=settings.elasticsearch.index_generation, max_output_tokens=settings.agent.max_output_tokens,
            **common)
        contract = resolve_event_indexing_contract(encoder, max_height=defaults.window.analysis_max_height)
        event = build_event_retrieval_service(repo, Timed(resources.elasticsearch_event_store, "event.es"),
                                              contract=contract, **common)
        chunk._embedder = Timed(chunk._embedder, "chunk.embedding")
        event._embedder = event._vision_embedder = Timed(event._embedder, "event.embedding")
        if chunk._reranker is not None:
            chunk._reranker = Timed(chunk._reranker, "chunk.rerank")
        environment = {"captured_at": datetime.now(timezone.utc).isoformat(),
            "series_id": SERIES, "target_chunk_id": TARGET,
            "chunk_index": resources.elasticsearch_store.index_name,
            "event_index": resources.elasticsearch_event_store.index_name,
            "retrieval_config": dataclasses.asdict(settings.retrieval), "event_contract": contract.as_dict(),
            "method": "Sequential in-process calls, same production factories/config, no forced query vector reuse or index writes; per-stage wall time includes service IO."}
        followup = args.actual_query
        (OUT / ("actual-query-environment.json" if followup else "environment.json")).write_text(
            json.dumps(environment, ensure_ascii=False, indent=2) + "\n")
        summaries = []
        if case:
            weights = dict(lexical_weight=0.65, text_weight=0.35, vision_weight=0.0)
            for item in case.get("benchmark_queries", [{"id": "quote", "query": QUERY}]):
                for repetition in range(1, case.get("repetitions", 3) + 1):
                    summaries.append(execute(f"event-{item['id']}-{repetition}", event, item["query"],
                                             event_types=["DIALOGUE"], **weights))
                    summaries.append(execute(f"chunk-{item['id']}-{repetition}", chunk, item["query"], **weights))
        elif followup:
            query = "中文台词“真是高高在上的”或“还真是居高临下”“一副高高在上的样子”，人物讽刺对方态度傲慢、居高临下的对话"
            for repetition in range(1, 4):
                summaries.append(execute(f"chunk-actual-query-{repetition}", chunk, query,
                    lexical_weight=0.55, text_weight=0.45, vision_weight=0.0))
        else:
            for repetition in range(1, 4):
                summaries.append(execute(f"event-original-{repetition}", event, QUERY, event_types=["DIALOGUE"]))
                summaries.append(execute(f"chunk-original-{repetition}", chunk, QUERY))
            summaries.append(execute("event-verified-wording", event, "真是居高临下啊", event_types=["DIALOGUE"]))
            summaries.append(execute("chunk-verified-wording", chunk, "真是居高临下啊"))
        (OUT / ("actual-query-summary.json" if followup else "summary.json")).write_text(
            json.dumps(summaries, ensure_ascii=False, indent=2) + "\n")
    finally:
        client.close()
        resources.close()


if __name__ == "__main__":
    main()
