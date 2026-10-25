"""Query capability and retrieval benchmarks: explicit suite, offline by default."""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import subprocess
import time
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]


def load_cases(path: Path) -> dict:
    fixture = json.loads(path.read_text(encoding="utf-8"))
    if fixture["schema_version"] != 1 or fixture["time_unit"] != "milliseconds":
        raise ValueError("Unsupported fixture version or time unit")
    seen = {}
    for case in fixture["cases"]:
        if case["id"] in seen or case["kind"] not in {"chat", "retrieval"}:
            raise ValueError(f"Duplicate ID or invalid kind: {case['id']}")
        if case["dataset"] not in fixture["datasets"] or not case["query"].strip():
            raise ValueError(f"Invalid dataset or query: {case['id']}")
        parent = case.get("after")
        if parent and (parent not in seen or case["kind"] != "chat"
                       or seen[parent]["kind"] != "chat"
                       or seen[parent]["dataset"] != case["dataset"]):
            raise ValueError(f"Conversation predecessor must appear first: {case['id']}")
        for fact in case["reference_facts"]:
            if fact not in fixture["facts"]:
                raise ValueError(f"Unknown reference fact: {fact}")
        for group in case["checks"].get("required_fact_groups", []):
            if not group or not set(group) <= set(case["reference_facts"]):
                raise ValueError(f"Invalid required fact group: {case['id']}")
        if case["kind"] == "retrieval":
            if case["mode"] not in {"event", "chunk"} or case["parameters"]["top_k"] <= 0:
                raise ValueError(f"Invalid retrieval parameters: {case['id']}")
        seen[case["id"]] = case
    if not seen:
        raise ValueError("Empty benchmark")
    for fact in fixture["facts"].values():
        if fact["start_ms"] < 0 or fact["end_ms"] <= fact["start_ms"]:
            raise ValueError("Invalid evidence interval")
        if fact["dataset"] not in fixture["datasets"]:
            raise ValueError("Invalid evidence dataset")
    return fixture


def select_cases(fixture: dict, only: list[str] | None, kind: str | None = None) -> list[dict]:
    by_id = {case["id"]: case for case in fixture["cases"]}
    wanted = set(only or [key for key, case in by_id.items() if not kind or case["kind"] == kind])
    if not wanted or wanted - by_id.keys():
        raise ValueError(f"Unknown or empty selection: {sorted(wanted - by_id.keys())}")
    for key in list(wanted):
        if kind and by_id[key]["kind"] != kind:
            raise ValueError(f"{key} is not a {kind} case")
        parent = by_id[key].get("after")
        while parent:
            wanted.add(parent)
            parent = by_id[parent].get("after")
    return [case for case in fixture["cases"] if case["id"] in wanted]


def chat_request(fixture: dict, case: dict, tenant: str) -> dict:
    # Deliberate allowlist: reference answers and grading facts never reach Chat.
    dataset = fixture["datasets"][case["dataset"]]
    payload = {"tenantId": tenant, "content": case["query"], "seriesIds": [dataset["series_id"]]}
    if case.get("playback"):
        playback = case["playback"]
        payload["extraContext"] = {"playback": {
            "seriesId": dataset["series_id"], "videoId": playback["video_id"],
            "episodeOrder": playback["episode"], "positionSeconds": playback["position_ms"],
        }}
    return payload


def normalize_chat(message: dict) -> dict:
    hits = (message.get("searchResult") or {}).get("hits", [])
    sources = {hit["candidate"]["videoChunkId"]: hit["evidence"] for hit in hits}
    cards = []
    for block in message.get("renderBlocks") or []:
        if block.get("type") != "video":
            continue
        evidence = sources.get(block["videoChunkId"], {})
        cards.append({"video_chunk_id": block["videoChunkId"],
                      "episode": evidence.get("episodeOrder"),
                      "series_id": evidence.get("seriesId"),
                      "content_hash": evidence.get("contentHash"),
                      "start_ms": block.get("startSeconds"), "end_ms": block.get("endSeconds")})
    return {"status": message["status"], "content": message.get("content") or "",
            "cards": cards, "model": message.get("modelName"), "timing": message.get("timing"),
            "error": message.get("lastError"), "message_id": message.get("messageId")}


def normalize_retrieval(result: dict, mode: str) -> dict:
    chunks = result["chunk_result"] if mode == "event" else result
    sources = {hit["candidate"]["video_chunk_id"]: hit["evidence"] for hit in chunks["hits"]}
    matches = []
    records = result["matches"] if mode == "event" else chunks["hits"]
    for position, item in enumerate(records, 1):
        chunk_id = item["video_chunk_id"] if mode == "event" else item["candidate"]["video_chunk_id"]
        evidence = sources[chunk_id]
        record = {"video_chunk_id": chunk_id, "episode": evidence["episode_order"],
                  "series_id": evidence["series_id"], "content_hash": evidence.get("content_hash"),
                  "returned_rank": position,
                  "start_ms": item.get("start_ms") if mode == "event" else evidence["start_seconds"],
                  "end_ms": item.get("end_ms") if mode == "event" else evidence["end_seconds"]}
        if mode == "event":
            record.update(event_id=item["event_id"], speaker=item.get("speaker"),
                          fused_rank=item["fused_rank"],
                          fused_score=item["fused_score"], channel_ranks=item["channel_ranks"],
                          channel_scores=item["channel_scores"],
                          dialogue=[f["raw_text"] for f in item["lexical_fields"] if f["field"] == "dialogue"])
        else:
            record["ranking"] = item["candidate"]
        matches.append(record)
    return {"status": "success", "matches": matches,
            "partial": chunks.get("partial"), "warnings": chunks.get("warnings", [])}


def evaluate(fixture: dict, case: dict, observed: dict) -> dict:
    completed = observed.get("status") == ("COMPLETED" if case["kind"] == "chat" else "success")
    records = observed.get("cards", []) if case["kind"] == "chat" else observed.get("matches", [])
    fact_hits = {}
    for key in case["reference_facts"]:
        fact = fixture["facts"][key]
        series = fixture["datasets"][fact["dataset"]]["series_id"]
        found = []
        for record in records:
            if (record.get("series_id") != series or record.get("episode") != fact["episode"]
                    or type(record.get("start_ms")) is not int or type(record.get("end_ms")) is not int
                    or record["start_ms"] > fact["start_ms"] or record["end_ms"] < fact["end_ms"]):
                continue
            if fact.get("content_hash") and record.get("content_hash") != fact["content_hash"]:
                continue
            if case.get("mode") == "event":
                if fact.get("speaker") and record.get("speaker") not in fact.get("speaker_aliases", [fact["speaker"]]):
                    continue
                if fact.get("quote") and fact["quote"] not in record.get("dialogue", []):
                    continue
            found.append(record.get("fused_rank", record.get("returned_rank", 1)))
        fact_hits[key] = {"found": bool(found), "best_rank": min(found) if found else None}
    failures = []
    checks = case["checks"]
    if not completed:
        failures.append("execution_not_completed")
    if completed and len(records) < checks.get("min_video_cards", 0):
        failures.append("too_few_video_cards")
    for group in checks.get("required_fact_groups", []):
        if not any(fact_hits[key]["found"] for key in group):
            failures.append("missing_known_source:" + "|".join(group))
    for episode, upper in checks.get("max_end_ms_by_episode", {}).items():
        if any(record.get("episode") == int(episode) and
               (type(record.get("end_ms")) is not int or record["end_ms"] > upper) for record in records):
            failures.append("playback_exceeds_time_bound:" + episode)
    if case["kind"] == "chat" and any(
        type(r.get("episode")) is not int or type(r.get("start_ms")) is not int
        or type(r.get("end_ms")) is not int or r["end_ms"] <= r["start_ms"] for r in records
    ):
        failures.append("unresolved_or_invalid_video_card")
    return {"execution_completed": completed, "source_checks_passed": not failures,
            "failures": failures, "reference_matches": fact_hits,
            "quality": "manual_review_required" if completed else "not_assessable",
            "rubric": case["rubric"]}


def run_chat(client, fixture: dict, cases: list[dict], tenant: str):
    conversations, completed = {}, set()
    for case in cases:
        parent = case.get("after")
        if parent and parent not in completed:
            yield case, {"status": "BLOCKED", "error": "predecessor_not_completed"}, None
            continue
        started = time.perf_counter()
        try:
            if parent:
                cid = conversations[parent]
            else:
                response = client.post("/api/agent/conversations/create",
                    headers={"Idempotency-Key": str(uuid4())},
                    json={"tenantId": tenant, "title": "[query-capabilities] " + case["id"]})
                response.raise_for_status()
                cid = response.json()["conversationId"]
            conversations[case["id"]] = cid
            response = client.post("/api/agent/messages/send", params={"conversationId": cid},
                headers={"Idempotency-Key": str(uuid4()), "Accept": "application/json"},
                json=chat_request(fixture, case, tenant))
            response.raise_for_status()
            observed = normalize_chat(response.json()["assistantMessage"])
            observed["conversation_id"] = cid
            if observed["status"] == "COMPLETED":
                completed.add(case["id"])
        except Exception as error:
            # Preserve completed cases; do not silently retry a potentially accepted request.
            observed = {"status": "ERROR", "error_type": type(error).__name__, "error": str(error)}
        yield case, observed, time.perf_counter() - started


@contextmanager
def retrieval_services():
    # Reuse current production factories without application startup or parse workers.
    from gateway.model_factory import GatewayClient
    from server.bootstrap import prepare_service_configuration
    from server.caller.base import init
    from server.common.retrieval import build_event_retrieval_service, build_retrieval_service
    from server.config import ServerSettings
    from server.config.parse_profile import resolve_event_indexing_contract
    from server.service.video_parse_persistence import VideoParsePersistenceService

    settings = ServerSettings.from_environment()
    gateway, defaults = prepare_service_configuration(settings)
    with ExitStack() as cleanup:
        resources = init(settings, semantic_dimensions=defaults.embedding.dimensions,
                         vision_dimensions=defaults.embedding.dimensions)
        cleanup.callback(resources.close)
        client = GatewayClient(gateway)
        cleanup.callback(client.close)
        persistence = VideoParsePersistenceService(resources.session_factory)
        common = dict(gateway_config=gateway, embedding_config=defaults.embedding,
                      retrieval_config=settings.retrieval, gateway_client=client, request_timeout_seconds=120)
        chunk, encoder = build_retrieval_service(persistence, resources.elasticsearch_store,
            index_generation=settings.elasticsearch.index_generation,
            max_output_tokens=settings.agent.max_output_tokens, **common)
        contract = resolve_event_indexing_contract(encoder, max_height=defaults.window.analysis_max_height)
        event = build_event_retrieval_service(persistence, resources.elasticsearch_event_store,
                                            contract=contract, **common)
        yield {"chunk": chunk, "event": event}, {
            "chunk_index": resources.elasticsearch_store.index_name,
            "event_index": resources.elasticsearch_event_store.index_name,
            "event_contract_id": contract.contract_id,
            "retrieval": settings.retrieval.model_dump(mode="json") if hasattr(settings.retrieval, "model_dump")
            else dataclasses.asdict(settings.retrieval),
        }


def run_retrieval(services: dict, fixture: dict, cases: list[dict], tenant: str):
    from gateway.usage import capture_model_usage

    for case in cases:
        usage = []
        started = time.perf_counter()
        try:
            with capture_model_usage(lambda call: usage.append({
                "model": call.model, "usage": dataclasses.asdict(call.usage) if call.usage is not None else None,
            })):
                result = services[case["mode"]].search(case["query"], tenant_id=tenant,
                    series_ids=[fixture["datasets"][case["dataset"]]["series_id"]], **case["parameters"])
            observed = normalize_retrieval(result.model_dump(mode="json"), case["mode"])
            observed["usage"] = usage
        except Exception as error:
            observed = {"status": "ERROR", "error_type": type(error).__name__, "error": str(error)}
        yield case, observed, time.perf_counter() - started


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", required=True, choices=("query_capabilities", "retrieval_quality"))
    parser.add_argument("--cases", type=Path, help="Override the selected suite's fixture, e.g. for another dataset scope")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Default: validate without API, DB or model calls")
    mode.add_argument("--replay", type=Path, help="Score compact historical observations offline")
    mode.add_argument("--live", choices=("chat", "retrieval"), help="Call real services; Chat creates conversations")
    parser.add_argument("--only", nargs="+")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--base-url", default="http://127.0.0.1:9621")
    parser.add_argument("--tenant-id", default="local-default")
    parser.add_argument("--data-version", help="Required for live runs: declared dataset/index revision")
    parser.add_argument("--timeout", type=float, default=1500)
    parser.add_argument("--repeat", type=int, default=1)
    args = parser.parse_args(argv)
    args.cases = args.cases or HERE / args.suite / "cases.json"
    fixture = load_cases(args.cases)
    suite_kind = "chat" if args.suite == "query_capabilities" else "retrieval"
    if any(case["kind"] != suite_kind for case in fixture["cases"]):
        parser.error(f"--suite {args.suite} requires only {suite_kind} cases")
    if args.live and args.live != suite_kind:
        parser.error(f"--suite {args.suite} requires --live {suite_kind}")
    selected = select_cases(fixture, args.only, args.live)
    if args.repeat < 1 or args.timeout <= 0:
        parser.error("repeat and timeout must be positive")
    if not args.live and not args.replay:
        print(json.dumps({"suite": args.suite, "cases": len(selected), "chat": sum(c["kind"] == "chat" for c in selected),
                          "api_called": False, "ids": [c["id"] for c in selected]}, ensure_ascii=False))
        return 0
    if args.live and not args.data_version:
        parser.error("--live requires --data-version; no dataset revision is inferred")
    selected_by_id = {case["id"]: case for case in selected}
    rows = []
    if args.replay:
        historical = [json.loads(line) for line in args.replay.read_text().splitlines() if line.strip()]
        for row in historical:
            if row.get("kind") == "run":
                continue
            if row["case_id"] in selected_by_id:
                rows.append({**row, "evaluation": evaluate(fixture, selected_by_id[row["case_id"]], row["observed"])})
        missing = selected_by_id.keys() - {r["case_id"] for r in rows}
        if missing:
            raise ValueError(f"Replay missing cases: {sorted(missing)}")
        if args.out:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            with args.out.open("x", encoding="utf-8") as output:
                for row in rows:
                    output.write(json.dumps(row, ensure_ascii=False) + "\n")
    else:
        started = datetime.now(timezone.utc)
        out = args.out or ROOT / "outputs/verification" / f"{args.suite}-{started:%Y%m%dT%H%M%S%fZ}.jsonl"
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("x", encoding="utf-8") as output, ExitStack() as cleanup:
            run = {"kind": "run", "suite": args.suite, "started_at": started.isoformat(), "mode": args.live,
                   "cases_sha256": hashlib.sha256(args.cases.read_bytes()).hexdigest(),
                   "data_version_declared": args.data_version, "tenant_id": args.tenant_id,
                   "client_git_head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
                   "client_git_dirty": bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT)),
                   "concurrency": 1, "planned_cases": list(selected_by_id), "repeat": args.repeat}
            if args.live == "chat":
                import httpx
                client = cleanup.enter_context(httpx.Client(base_url=args.base_url, timeout=args.timeout))
                run["base_url"] = args.base_url
            else:
                services, environment = cleanup.enter_context(retrieval_services())
                run["environment"] = environment
            output.write(json.dumps(run, ensure_ascii=False) + "\n")
            output.flush()
            for repeat in range(1, args.repeat + 1):
                observations = (run_chat(client, fixture, selected, args.tenant_id) if args.live == "chat"
                                else run_retrieval(services, fixture, selected, args.tenant_id))
                for case, observed, elapsed in observations:
                    row = {"case_id": case["id"], "repeat": repeat, "wall_seconds": elapsed,
                           "observed": observed, "evaluation": evaluate(fixture, case, observed)}
                    rows.append(row)
                    output.write(json.dumps(row, ensure_ascii=False) + "\n")
                    output.flush()
        print(f"Output: {out}")
    print(json.dumps({"suite": args.suite, "observations": len(rows),
        "execution_completed": sum(r["evaluation"]["execution_completed"] for r in rows),
        "source_checks_passed": sum(r["evaluation"]["source_checks_passed"] for r in rows),
        "manual_review_required": sum(r["evaluation"]["quality"] == "manual_review_required" for r in rows),
        "overall_accuracy": None, "api_called": bool(args.live)}, ensure_ascii=False))
    # Replay contains known historical failures; a successful audit is not a quality pass.
    return int(bool(args.live) and any(not r["evaluation"]["source_checks_passed"] for r in rows))


if __name__ == "__main__":
    raise SystemExit(main())
