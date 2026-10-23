"""Read-only Event vision-channel smoke search; explicitly run only when authorized.

From the repository root:
uv run --env-file .env.local python engine/experiment/composition-models/20261018-event-backfill/smoke_event_search.py --episode 1 --output engine/experiment/composition-models/20261018-event-backfill/search-ep01.json

The real search calls the current text query embedding API. It never queues tasks,
initializes an index, or writes the database. Importing this file makes no requests.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

from sqlalchemy import select
from gateway.config import load_config
from gateway.model_factory import GatewayClient
from scripts.local_services import configure, config_digest
from server.caller.elasticsearch import open_elasticsearch_client, build_event_elasticsearch_storage
from server.caller.mysql import open_database_storage
from server.cli import _event_contract
from server.common.retrieval import build_event_retrieval_service
from server.config.parse_profile import resolve_embedding_config
from server.config.settings import resolve_parse_max_height
from server.model.video import Series, Video, VideoChunk
from server.repository.video_parse import VideoParseRepository

SERIES = "836fa9f0-2bf4-50fb-9c64-75526fa1dea9"
TENANT = "local-default"
DEFAULT_QUERIES = ("俯视房间，人物很小，坐在沙发上", "舞台上乐队演奏")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--episode", type=int, help="Restrict coverage and search to this episode's canonical Chunks")
    parser.add_argument("--query", action="append", help="Repeat for multiple text queries; replaces the two defaults")
    parser.add_argument("--top-k", type=int, default=5, help="Maximum distinct Chunk groups")
    parser.add_argument("--exact", action="store_true", help="Use exact Event vector scoring instead of configured ANN")
    args = parser.parse_args()
    if args.top_k < 1 or args.episode is not None and not 1 <= args.episode <= 13:
        parser.error("top-k must be positive and episode must be in 1..13")
    queries = args.query or DEFAULT_QUERIES
    if any(not query.strip() for query in queries):
        parser.error("queries must be nonblank")
    logging.disable(logging.CRITICAL)
    settings = configure()
    gateway = load_config()
    embedding = resolve_embedding_config(gateway, dimensions=settings.text_vector_dimensions)
    contract = _event_contract(gateway, embedding, max_height=resolve_parse_max_height())
    report = {"observed_at": datetime.now(timezone.utc).isoformat(), "series_id": SERIES,
              "episode_filter": args.episode, "config_digest": config_digest(),
              "contract_id": contract.contract_id, "channel": "vision", "query_modality": "text",
              "channel_weights": {"lexical": 0.0, "semantic": 0.0, "vision": 1.0},
              "top_k_unit": "distinct_chunk_groups", "exact": args.exact,
              "limitation": "Retrieval candidates require frame inspection; scores do not prove composition similarity.",
              "results": []}
    client = GatewayClient(gateway)
    try:
        with open_database_storage(settings.database_url, environment=settings.environment,
                                   pool_recycle_seconds=3600) as (_, sessions), open_elasticsearch_client(settings.elasticsearch) as es:
            repository = VideoParseRepository(sessions)
            store = build_event_elasticsearch_storage(es, settings.elasticsearch, environment=settings.environment,
                semantic_dimensions=settings.text_vector_dimensions, vision_dimensions=settings.vision_vector_dimensions)
            report["event_index"] = store.index_name
            chunk_ids = None
            if args.episode is not None:
                with sessions() as session:
                    chunk_ids = list(session.scalars(select(VideoChunk.id).join(Video, Video.id == VideoChunk.video_id)
                        .join(Series, Series.id == Video.series_id).where(Series.tenant_id == TENANT,
                            Video.series_id == SERIES, Video.episode_order == args.episode)
                        .order_by(VideoChunk.sequence_no)))
                if not chunk_ids:
                    raise ValueError("requested episode has no canonical Chunks")
            report["restricted_chunk_count"] = len(chunk_ids) if chunk_ids is not None else None
            timeout = min(value for value in (
                settings.agent_request_timeout_seconds, settings.agent.execution_timeout_seconds,
            ) if value is not None)
            service = build_event_retrieval_service(repository, store, gateway_config=gateway,
                embedding_config=embedding, retrieval_config=settings.retrieval, contract=contract,
                request_timeout_seconds=timeout, exact=args.exact, gateway_client=client)
            for query in queries:
                started = time.perf_counter()
                entry = {"query": query.strip(), "channel": "vision"}
                try:
                    result = service.search(query, tenant_id=TENANT, series_ids=[SERIES], top_k=args.top_k,
                        lexical_weight=0.0, text_weight=0.0, vision_weight=1.0, video_chunk_ids=chunk_ids)
                    trace = result.trace
                    assert result.matches, "Event vision query must return a verified match"
                    assert not result.chunk_result.partial, "Event result must have complete coverage"
                    assert not trace["rejected"], "Event candidates must pass source verification"
                    assert all(set(match.channel_ranks) == set(match.channel_scores) == {"vision"}
                               and match.start_ms is not None and match.end_ms is not None
                               and 0 <= match.start_ms < match.end_ms for match in result.matches), \
                        "Every selected Event must have only vision scores and a valid source interval"
                    raw = trace["raw_candidates"]
                    assert all(hit["channel"] == "vision" for hit in raw)
                    hits = {hit.candidate.video_chunk_id: hit for hit in result.chunk_result.hits}
                    matches = []
                    for match in result.matches:
                        evidence = hits[match.video_chunk_id].evidence
                        event = evidence.all_time_events[match.timeline_index]
                        matches.append({"event_id": match.event_id, "parse_task_id": match.parse_task_id,
                            "video_chunk_id": match.video_chunk_id, "content_version": match.version,
                            "timeline_index": match.timeline_index, "video_id": evidence.video_id,
                            "episode": evidence.episode_order, "start_ms": match.start_ms, "end_ms": match.end_ms,
                            "event_type": match.event_type, "description": event["description"],
                            "vision_rank": match.channel_ranks.get("vision"),
                            "vision_score": match.channel_scores.get("vision"), "fused_score": match.fused_score})
                    entry.update(status="ok", partial=result.chunk_result.partial,
                        warnings=result.chunk_result.warnings, matches=matches,
                        trace_summary={"strategy_version": trace["strategy_version"],
                            "source_count": len(trace["sources"]), "channel_weights": trace["channel_weights"],
                            "raw_candidates_by_channel": dict(Counter(hit["channel"] for hit in raw)),
                            "fused_candidates": len(trace["fused_candidates"]),
                            "budgeted_candidates": len(trace["budgeted_event_ids"]),
                            "budget_excluded_count": len(trace["budget_excluded_event_ids"]),
                            "rejected_count": len(trace["rejected"]),
                            "selected_event_ids": trace["selected_event_ids"],
                            "returned_chunk_ids": trace["returned_chunk_ids"]})
                except Exception as error:
                    entry.update(status="error", error_type=type(error).__name__,
                                 message="Event search failed or coverage is not ready; no raw exception payload saved")
                entry["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 2)
                report["results"].append(entry)
    finally:
        client.close()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"output": str(args.output), "queries": len(report["results"]),
        "successful": sum(item["status"] == "ok" for item in report["results"])}, ensure_ascii=False))
    return 0 if all(item["status"] == "ok" for item in report["results"]) else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(json.dumps({"status": "error", "error_type": type(error).__name__,
                          "message": "Event smoke setup failed; raw exception omitted to protect configuration"}))
        raise SystemExit(2)
