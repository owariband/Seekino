"""COST-02 2x2 replay: full/course Event enum x low/none reasoning on five first-30 chunks (2026-11-04)."""
from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from decimal import Decimal
from pathlib import Path

import run_first30 as r

REPO = Path('/Users/hypnos/workspace/agent/Seekino/outputs/worktrees/parse-event-profile')
spec = importlib.util.spec_from_file_location('parse_profile_ab', REPO / 'evals/parse_profile_ab.py')
ab = importlib.util.module_from_spec(spec); spec.loader.exec_module(ab)

# Top five by ON_SCREEN_TEXT + ACTION in the 0.5 fps + medium outputs; ties broken by descending name.
CHUNKS = (0, 28, 19, 2, 23)
VARIANTS = (("full", "low"), ("course", "low"), ("full", "none"), ("course", "none"))
REPEATS = 2
OUT = r.RESULTS / "profile-ab"
CAP_CNY = Decimal("3")


async def one(sequence, profile, effort, repeat, spent):
    stem = f"chunk-{sequence:03d}"
    request = r.read_json(r.PREPARED / "understand" / f"{stem}.request.json")
    meta = r.read_json(r.PREPARED / "understand" / f"{stem}.meta.json")
    media_root = r.EVIDENCE / Path(meta["baseline_events_path"]).parent
    variant = ab.build_variant(request, event_profile=profile, reasoning_effort=effort)
    diff = ab.variant_diff_paths(request, variant)
    destination = OUT / f"{profile}-{effort}" / stem / f"rep-{repeat}"
    if spent[0] >= CAP_CNY:
        return {"variant": f"{profile}-{effort}", "chunk": sequence, "repeat": repeat, "status": "skipped_cap"}
    call = await r.run_with_network_retries(variant, media_root, destination, 0)
    record = call["record"]
    usage = record["usage"]
    cost = Decimal(record["estimated_cny"])
    spent[0] += cost
    schema_ok, coverage, event_types = False, 0.0, {}
    try:
        parsed = r.normalize_parse_response(json.loads(call["text"]))
        result = r.validate_result_time_range(
            r.VideoParseResult.model_validate(parsed),
            start_seconds=meta["allowed_visual_range_ms"][0], end_seconds=meta["allowed_visual_range_ms"][1],
            dialogue_bounds=tuple(meta["dialogue_bounds_ms"]),
        ).model_dump(mode="json")
        allowed = set(ab.COURSE_EVENT_TYPES) if profile == "course" else set(ab.EVENT_TYPES)
        event_types = {}
        for event in result["timeline"]:
            event_types[event["event_type"]] = event_types.get(event["event_type"], 0) + 1
        schema_ok = set(event_types) <= allowed
        lo, hi = meta["allowed_visual_range_ms"]
        cues = [c for c in meta["asr"]["transcript"] if c["end_seconds"] > lo and c["start_seconds"] < hi]
        coverage = ab.dialogue_coverage(result, cues)
        r.write_json(destination / "result.json", result, pretty=False)
    except Exception as error:  # schema failures are a measured outcome, not a crash
        r.write_json(destination / "invalid.json", {"error": str(error)[:2000]})
    return {"variant": f"{profile}-{effort}", "chunk": sequence, "repeat": repeat, "cost_cny": str(cost),
            "band": record["price_band"], "reasoning_tokens": usage.get("reasoning_tokens") or 0,
            "output_tokens": usage["output_tokens"], "input_tokens": usage["input_tokens"],
            "elapsed_s": record["elapsed_s"], "schema_ok": schema_ok, "dialogue_coverage": coverage,
            "event_types": event_types, "diff_paths": diff}


async def main():
    spent = [Decimal(0)]
    semaphore = asyncio.Semaphore(int(sys.argv[1]) if len(sys.argv) > 1 else 8)

    async def bound(*args):
        async with semaphore:
            try:
                return await one(*args, spent)
            except Exception as error:
                return {"variant": f"{args[1]}-{args[2]}", "chunk": args[0], "repeat": args[3],
                        "status": "failed", "error": str(error)[:500]}

    jobs = [bound(c, p, e, k) for k in range(REPEATS) for c in CHUNKS for p, e in VARIANTS]
    rows = await asyncio.gather(*jobs)
    OUT.mkdir(parents=True, exist_ok=True)
    r.write_json(OUT / "rows.json", rows)
    ok = [row for row in rows if "cost_cny" in row]
    summary = {"spent_cny": str(spent[0]), "rows": len(rows), "measured": len(ok), "by_variant": ab.summarize(ok)}
    r.write_json(OUT / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    asyncio.run(main())
