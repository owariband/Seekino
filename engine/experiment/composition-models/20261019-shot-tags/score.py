"""Score USER-exported judgments against saved, blinded-pool rankings.

This script never creates labels. Partial judging is supported; unjudged
positions contribute no observed gain and are explicitly counted in coverage.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
from statistics import mean

ROOT = Path(__file__).resolve().parent


def dcg(values):
    return sum((2 ** value - 1) / math.log2(index + 2) for index, value in enumerate(values))


def evaluate(judgments_path, experiment=ROOT):
    pool = json.loads((experiment / "pool.json").read_text())
    provenance = json.loads((experiment / "method_components.json").read_text())
    if provenance.get("pool_sha256") and hashlib.sha256((experiment / "pool.json").read_bytes()).hexdigest() != provenance["pool_sha256"]:
        raise ValueError("Pool differs from its saved ranking provenance")
    queries = pool["queries"]
    candidates = {}
    aliases = {}
    for query in queries:
        qid = query["query_shot_id"]
        for candidate in query["candidates"]:
            cid = candidate["candidate_shot_id"]
            candidates[qid, cid] = candidate
            for sid in candidate.get("collapsed_shot_ids", [cid]):
                aliases[qid, sid] = cid
            aliases[qid, cid] = cid

    labels = {}
    rows_read = 0
    for line_number, line in enumerate(judgments_path.read_text().splitlines(), 1):
        if not line.strip():
            continue
        row = json.loads(line)
        rows_read += 1
        key = row["query_shot_id"], row["candidate_shot_id"]
        if key not in candidates:
            raise ValueError(f"Line {line_number}: judgment pair is outside the saved pool: {key}")
        values = row.get("labels", row)
        clean = {}
        for field in ("category", "layout"):
            value = values.get(field)
            if value is not None and (type(value) is not int or value not in (0, 1, 2)):
                raise ValueError(f"Line {line_number}: {field} must be 0, 1, 2 or null")
            clean[field] = value
        for field in ("same_location", "duplicate"):
            value = values.get(field)
            if value is not None and type(value) is not bool:
                raise ValueError(f"Line {line_number}: {field} must be boolean or null")
            clean[field] = value
        stamp = row.get("timestamp")
        if not isinstance(stamp, str):
            raise ValueError(f"Line {line_number}: timestamp is required")
        instant = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        if instant.tzinfo is None:
            raise ValueError(f"Line {line_number}: timestamp must include its time zone")
        clean["timestamp"] = stamp
        if key not in labels or instant >= labels[key][0]:
            labels[key] = instant, clean
    labels = {key: value[1] for key, value in labels.items()}

    by_method = defaultdict(list)
    # Current pool ownership excludes stale files left after swapping alternates.
    ranking_files = [experiment / "rankings" / method / (query["query_shot_id"] + ".json")
                     for method in provenance["methods"] for query in queries]
    for file in ranking_files:
        expected_sha = provenance["source_hashes"]["rankings"][str(file.relative_to(experiment))]
        if hashlib.sha256(file.read_bytes()).hexdigest() != expected_sha:
            raise ValueError(f"Ranking changed after the judging pool was built: {file}")
        ranking = json.loads(file.read_text())
        qid = ranking["query_shot_id"]
        # Preserve original cutoffs: removing an alias and filling its position
        # from rank 11 would introduce an unpooled candidate into nDCG@10.
        # A repeated pool cluster gets gain only at its first rank; flags still
        # describe every retrieved position. Recall naturally uses set overlap.
        order = [aliases.get((qid, row["shot_id"]), row["shot_id"]) for row in ranking["rows"]]
        record = {"query_shot_id": qid, "ranked_shots": len(order),
                  "unique_clusters_after_pool_alias_mapping": len(set(order))}
        for field in ("category", "layout"):
            judged = {cid: value[field] for (query, cid), value in labels.items()
                      if query == qid and value[field] is not None}
            top10 = order[:10]
            top5 = order[:5]
            positives = {cid for cid, value in judged.items() if value >= 1}
            ideal = dcg(sorted(judged.values(), reverse=True)[:10])
            n10 = sum(cid in judged for cid in top10)
            n5 = sum(cid in judged for cid in top5)
            positive5 = sum(judged.get(cid, 0) >= 1 for cid in set(top5))
            seen = set()
            gains = []
            for cid in top10:
                gains.append(judged.get(cid, 0) if cid not in seen else 0)
                seen.add(cid)
            record[field] = {
                "judged_pool_candidates": len(judged), "judged_positives": len(positives),
                "judged_at_10": n10, "available_at_10": len(top10),
                "judged_at_5": n5, "available_at_5": len(top5),
                "ndcg_at_10": dcg(gains) / ideal if ideal and n10 else None,
                "p_at_5": positive5 / len(top5) if n5 and top5 else None,
                "judged_only_precision_at_5": positive5 / n5 if n5 else None,
                "pooled_recall_at_50": len(positives & set(order[:50])) / len(positives) if positives else None,
                "fully_judged_top10": bool(top10) and n10 == len(top10),
            }
        flags = [labels.get((qid, cid), {}).get("same_location") for cid in order[:10]]
        known_flags = [flag for flag in flags if flag is not None]
        record["content_leakage"] = {
            "same_location_at_10": sum(known_flags), "judged_at_10": len(known_flags),
            "available_at_10": len(flags),
            "share_top10_observed": sum(known_flags) / len(flags) if known_flags and flags else None,
            "share_among_judged_top10": sum(known_flags) / len(known_flags) if known_flags else None,
        }
        by_method[ranking["method"]].append(record)

    methods = {}
    for method, records in sorted(by_method.items()):
        summary = {"queries": len(records), "per_query": records}
        for field in ("category", "layout"):
            group = [record[field] for record in records]
            summary[field] = {
                "judged_at_10": sum(row["judged_at_10"] for row in group),
                "available_at_10": sum(row["available_at_10"] for row in group),
                "fully_judged_top10_queries": sum(row["fully_judged_top10"] for row in group),
            }
            for metric in ("ndcg_at_10", "p_at_5", "judged_only_precision_at_5", "pooled_recall_at_50"):
                values = [row[metric] for row in group if row[metric] is not None]
                summary[field][metric] = mean(values) if values else None
                summary[field][metric + "_queries_used"] = len(values)
            full = [row["ndcg_at_10"] for row in group if row["fully_judged_top10"] and row["ndcg_at_10"] is not None]
            summary[field]["ndcg_at_10_fully_judged_queries"] = mean(full) if full else None
        leakage = [row["content_leakage"] for row in records]
        known = sum(row["judged_at_10"] for row in leakage)
        positive = sum(row["same_location_at_10"] for row in leakage)
        available = sum(row["available_at_10"] for row in leakage)
        summary["content_leakage"] = {
            "same_location_at_10": positive, "judged_at_10": known, "available_at_10": available,
            "share_top10_observed": positive / available if known and available else None,
            "share_among_judged_top10": positive / known if known else None,
        }
        methods[method] = summary
    return {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "scope": provenance.get("scope"),
        "judgments_file": str(judgments_path.resolve()), "rows_read": rows_read,
        "unique_pairs_read": len(labels),
        "unique_pairs_used": sum(any(row[field] is not None for field in ("category", "layout", "same_location", "duplicate")) for row in labels.values()),
        "pairs_by_field": {field: sum(row[field] is not None for row in labels.values())
                           for field in ("category", "layout", "same_location", "duplicate")},
        "pool_pairs": len(candidates),
        "metric_notes": [
            "Only user-supplied labels are read; null means unjudged, never relevance zero.",
            "For partial nDCG/P@5, unjudged ranking positions contribute zero observed gain. These provisional metrics are not final accuracy estimates; coverage is reported.",
            "nDCG uses gain 2^label-1 and log2(rank+1), with ideal top10 from all judged candidates in the query pool. Queries without judged positives or any judged top10 are omitted from the mean.",
            "P@5 uses unique positive clusters>=1 in the original top5 and a fixed five-position denominator (or available ranking length when shorter); judged-only precision is also reported.",
            "Pooled recall@50 divides retrieved judged positives by all judged positives in this query's pool. It is not corpus-wide recall.",
            "Content leakage reports same-location flags over all top10 slots and over judged top10 slots separately; partial flags make the first an observed lower bound.",
            "Pool aliases share one judgment. Original rank positions and @k cutoffs are preserved; a repeated cluster earns relevance gain only at its first occurrence. Cutoffs are never refilled with unpooled lower-ranked candidates. Location flags count each original position; recall uses unique cluster overlap. Latest timestamp wins when the same pair has multiple exports, including explicit all-null clearing records.",
        ],
        "methods": methods,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("judgments", type=Path, help="User-exported judgments.jsonl; never generated labels")
    parser.add_argument("--output", type=Path, default=ROOT / "scores.json")
    args = parser.parse_args()
    report = evaluate(args.judgments)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"output": str(args.output), "pairs_used": report["unique_pairs_used"],
                      "pool_pairs": report["pool_pairs"],
                      "methods": {name: {key: value for key, value in summary.items() if key != "per_query"}
                                  for name, summary in report["methods"].items()}}, indent=2))


if __name__ == "__main__":
    main()
