"""Validate persisted rankings, pooled aliases and blinded HTML after generation."""
from __future__ import annotations

from datetime import datetime, timezone
import itertools
import json
import math
from pathlib import Path
import re

from build_pool import ShotPairSimilarity
from rank import ROOT, METHODS, competition_ranks, exclusion_reasons, load_embeddings, read_jsonl, rrf_score, save_json, selection_scope, sha


def main():
    run = json.loads((ROOT / "rankings/run.json").read_text())
    assert run["status"] == "complete"
    for key, path in [("queries", ROOT / "queries.json"), ("keyframe_tags", ROOT / "keyframe_tags.jsonl")]:
        assert run["source_hashes"][key] == sha(path)
    manifest = json.loads((ROOT / "input_manifest.json").read_text())
    gallery = Path(manifest["gallery"])
    assert run["source_hashes"]["methods"] == sha(ROOT / "rankings/methods.json")
    assert run["source_hashes"]["runner"] == sha(ROOT / "rank.py")
    for key, name in [("gallery", "gallery.jsonl"), ("embeddings", "embeddings.npy"), ("embedding_rows", "embedding_rows.jsonl")]:
        assert run["source_hashes"][key] == sha(gallery / name)
    tags = read_jsonl(ROOT / "keyframe_tags.jsonl")
    by_frame = {r["frame_id"]: r for r in tags}
    gallery_rows = read_jsonl(gallery / "gallery.jsonl")
    scope = selection_scope(tags, gallery_rows)
    assert run["scope"] == scope
    assert run["source_hashes"]["selection_manifest"] == scope["manifest_sha256"]
    assert run["source_hashes"]["selection_frames"] == scope["frames_sha256"]
    queries = json.loads((ROOT / "queries.json").read_text())["queries"]
    assert len(queries) == run["query_count"]
    pool = json.loads((ROOT / "pool.json").read_text())
    pooled = {q["query_shot_id"]: q for q in pool["queries"]}
    assert len(pooled) == len(pool["queries"]) and set(pooled) == {q["query_shot_id"] for q in queries}
    hidden = json.loads((ROOT / "method_components.json").read_text())
    assert hidden["pool_sha256"] == sha(ROOT / "pool.json")
    assert hidden["source_hashes"]["queries"] == sha(ROOT / "queries.json")
    assert hidden["source_hashes"]["ranking_run"] == sha(ROOT / "rankings/run.json")
    hidden_queries = {q["query_shot_id"]: q for q in hidden["queries"]}
    assert set(hidden_queries) == set(pooled)
    for method in METHODS:
        assert {p.stem for p in (ROOT / "rankings" / method).glob("*.json")} == set(pooled)
    _, embedding_ids, matrix = load_embeddings(gallery)
    pair_similarity = ShotPairSimilarity(gallery_rows, embedding_ids, matrix)
    summary = []
    for query in queries:
        qid = query["query_shot_id"]
        qframe = query.get("frame_id", query.get("representative_frame_id"))
        q = by_frame[qframe]
        raw = read_jsonl(ROOT / "rankings/components" / f"{qid}.jsonl")
        assert len({r["frame_id"] for r in raw}) == len(raw)
        expected_eligible = {r["frame_id"] for r in tags if not exclusion_reasons(q, r, "M0")}
        assert expected_eligible == {r["frame_id"] for r in raw}
        for record in raw:
            frame = by_frame[record["frame_id"]]
            assert record["eligible_M0"] is True
            assert record["eligible_M1_to_M5"] == (not exclusion_reasons(q, frame, "M1"))
        # Reconstruct component ranks from every saved raw score, including ties
        # and unavailable layout, without relying on the persisted rank columns.
        m0 = competition_ranks([r["component_scores"]["doubao"] for r in raw])
        components = {name: competition_ranks([r["component_scores"][name] if r["eligible_M1_to_M5"] else None for r in raw], descending=name != "layout") for name in ["doubao", "tags", "pcp", "layout"]}
        for i, record in enumerate(raw):
            assert record["M0_doubao_rank"] == m0[i]
            assert record["M1_to_M5_component_ranks"] == {name: ranks[i] for name, ranks in components.items()}
        union, method_counts = set(), {}
        for method, used in METHODS.items():
            path = ROOT / "rankings" / method / f"{qid}.json"
            assert hidden["source_hashes"]["rankings"][str(path.relative_to(ROOT))] == sha(path)
            ranking = json.loads(path.read_text())
            rows = ranking["rows"]
            assert len({r["shot_id"] for r in rows}) == len(rows) == ranking["candidate_shots"]
            assert [r["rank"] for r in rows] == list(range(1, len(rows) + 1))
            expected = {}
            for record in raw:
                if method != "M0" and not record["eligible_M1_to_M5"]:
                    continue
                if method in ("M0", "M1"):
                    score = record["component_scores"]["doubao"]
                else:
                    score = rrf_score(record["M1_to_M5_component_ranks"], used)
                key = (-score, record["frame_id"])
                sid = record["shot_id"]
                if sid not in expected or key < expected[sid][0]:
                    expected[sid] = (key, record)
            ordered = sorted(expected, key=lambda sid: (expected[sid][0][0], sid, expected[sid][0][1]))
            assert [r["shot_id"] for r in rows] == ordered
            for row in rows:
                key, evidence = expected[row["shot_id"]]
                assert row["frame_id"] == evidence["frame_id"]
                assert math.isclose(row["score"], -key[0], rel_tol=1e-12, abs_tol=1e-12)
                assert not exclusion_reasons(q, by_frame[row["frame_id"]], method)
                for name in used:
                    rank = evidence["M0_doubao_rank"] if method == "M0" else evidence["M1_to_M5_component_ranks"][name]
                    assert row["component_ranks"][name] == rank
                    assert row["component_scores"][name] == evidence["component_scores"][name]
                assert row["available_components"] == [name for name in used if row["component_ranks"][name] is not None]
            method_counts[method] = len(rows)
            union.update(r["shot_id"] for r in rows[:10])
        candidates = pooled[qid]["candidates"]
        assert len({c["candidate_shot_id"] for c in candidates}) == len(candidates)
        aliases = [sid for c in candidates for sid in c["collapsed_shot_ids"]]
        assert len(aliases) == len(set(aliases)) and set(aliases) == union
        assert hidden_queries[qid]["union_shots_before_collapse"] == len(union)
        assert hidden_queries[qid]["candidates_after_collapse"] == len(candidates)
        for candidate in candidates:
            assert candidate["candidate_shot_id"] in candidate["collapsed_shot_ids"]
            assert by_frame[candidate["frame_id"]]["shot_id"] == candidate["candidate_shot_id"]
            assert (ROOT / candidate["image_path"]).is_file()
            for left, right in itertools.combinations(candidate["collapsed_shot_ids"], 2):
                assert pair_similarity.compare(left, right)["qualifies"]
        summary.append({"query_shot_id": qid, "eligible_M0_frames": len(raw), "ranked_shots": method_counts,
                        "union_shots": len(union), "pooled_candidates": len(candidates)})
    # Validate exact public field allowlist, not just visually hidden DOM nodes.
    html = (ROOT / "judge.html").read_text()
    match = re.search(r'<script id="poolData" type="application/json">(.*?)</script>', html, re.S)
    assert match
    encoded = match.group(1)
    assert not re.search(r'\bM[0-5]\b', encoded)
    public = json.loads(encoded)
    expected_public = {"queries": [{"query_shot_id": q["query_shot_id"], "image_path": q["image_path"],
                                   "candidates": [{"candidate_shot_id": c["candidate_shot_id"], "image_path": c["image_path"]} for c in q["candidates"]]} for q in pool["queries"]]}
    assert public == expected_public
    assert not re.search(r'"(?:method|rank|score|component_ranks|component_scores|labels)"\s*:', encoded)
    sanity = json.loads((ROOT / "rankings/sanity_reference_ranks.json").read_text())
    if "mygo-ep01-s00152" in pooled:
        query_frame = next(q["frame_id"] for q in queries if q["query_shot_id"] == "mygo-ep01-s00152")
        reference = by_frame[query_frame]
        targets = {r["shot_id"] for r in tags if (r["episode"] == 7 and r["start_ms"] <= 248600 < r["end_ms"]) or
                   (r["episode"] == 10 and r["start_ms"] < 935600 and 930300 < r["end_ms"])}
        assert len(targets) == 3, "Expected Ep7 candidate and both Ep10 shots in the known interval"
        sanity_map = {(r["method"], r["candidate_shot_id"]): r for r in sanity}
        assert len(sanity_map) == len(sanity) and set(sanity_map) == set(itertools.product(METHODS, targets))
        for (method, sid), record in sanity_map.items():
            assert record["query_shot_id"] == reference["shot_id"]
            ranking = json.loads((ROOT / "rankings" / method / f"{reference['shot_id']}.json").read_text())
            ranked = next((r for r in ranking["rows"] if r["shot_id"] == sid), None)
            assert record["rank"] == (ranked["rank"] if ranked else None)
            assert record["frame_id"] == (ranked["frame_id"] if ranked else None)
            if ranked is None:
                reasons = {r["frame_id"]: exclusion_reasons(reference, r, method) for r in tags if r["shot_id"] == sid}
                assert record["frame_exclusions"] == reasons and all(reasons.values())
                assert record["exclusion_reasons"] == sorted({reason for values in reasons.values() for reason in values})
    result = {"status": "passed", "created_at": datetime.now(timezone.utc).isoformat(), "query_count": len(summary),
              "candidate_pairs": sum(q["pooled_candidates"] for q in summary), "scope": scope, "queries": summary,
              "hashes": {name: sha(ROOT / name) for name in ["pool.json", "method_components.json", "judge.html", "rankings/run.json", "rankings/sanity_reference_ranks.json"]},
              "checks": ["source and ranking hashes", "exhaustive eligible frame coverage", "component rank/tie reconstruction", "full M0–M5 shot uniqueness and ordering", "best coherent keyframe per shot", "RRF component subsets and missing evidence", "all reuse/temporal/story exclusions", "top-10 pool alias conservation", "all-pairs complete-link cosine/time criteria", "exact blinded HTML data allowlist", "Ep7 and both Ep10 sanity ranks/exclusion reasons"]}
    save_json(ROOT / "rankings/validation.json", result)
    print(json.dumps({k: v for k, v in result.items() if k != "queries"}, indent=2))


if __name__ == "__main__":
    main()
