"""Select 25 story queries and 10 alternates by recorded tag coverage, without relevance labels.

Selection uses the explicitly authorized completed-data exploratory subset and a deterministic coverage
heuristic; it neither compares image similarity nor judges any candidate retrieval.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import html
import json
import math
import os
from pathlib import Path
from urllib.parse import quote

from schema_models import ShotTag

OUT = Path(__file__).resolve().parent
SEED = "mygo-shot-tags-query-coverage-v1-20261019"
MANDATORY_SHOT = "mygo-ep01-s00152"
FEATURE_NAMES = {
    "overhead": "overhead camera angle", "high": "high camera angle", "low": "low camera angle",
    "extreme_close_up": "extreme close-up", "close_up": "close-up",
    "long_tiny_figure": "long/extreme-long shot with a tiny figure",
    "two_person": "two visible people", "group": "three or more visible people",
    "subject_left": "main subject in the left grid column", "subject_right": "main subject in the right grid column",
    "symmetric": "symmetric composition", "diagonal": "diagonal composition",
    "frame_within_frame": "frame-within-frame composition", "large_negative_space": "large negative space",
}


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def rows(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def tie_key(row):
    return hashlib.sha256((SEED + "\n" + row["frame_id"]).encode()).hexdigest()


def coverage_features(row):
    camera, composition, subjects = row["camera"], row["composition"], row["subjects"]
    result = set()
    if camera["vertical_angle"] in {"overhead", "high", "low"}:
        result.add(camera["vertical_angle"])
    if camera["shot_size"] in {"extreme_close_up", "close_up"}:
        result.add(camera["shot_size"])
    count = subjects["person_count"]
    has_people = count == "many" or count > 0
    if (camera["shot_size"] in {"long", "extreme_long"} and subjects["main_subject_scale"] == "tiny"
            and subjects["main_subject_kind"] in {"person", "group"} and has_people):
        result.add("long_tiny_figure")
    if count == 2:
        result.add("two_person")
    if count == "many" or isinstance(count, int) and count >= 3:
        result.add("group")
    if subjects["main_subject_cell"].endswith("L"):
        result.add("subject_left")
    if subjects["main_subject_cell"].endswith("R"):
        result.add("subject_right")
    labels = {composition["primary"], *composition["secondary"]}
    result.update(labels & {"symmetric", "diagonal", "frame_within_frame"})
    if composition["negative_space"]["amount"] == "large":
        result.add("large_negative_space")
    return result


def clarity(row):
    return sum(row[group]["clarity"] == "clear" for group in ("camera", "composition", "subjects"))


def choose(candidates, count, *, initial=(), previous=()):
    """Greedy coverage, then balanced episode/feature coverage, with seeded tie breaking."""
    ordered = sorted(candidates, key=tie_key)
    by_id = {row["shot_id"]: row for row in candidates}
    selected = [by_id[shot_id] for shot_id in initial]
    notes = {row["shot_id"]: ["mandatory_reference"] for row in selected}
    present_features = {feature for row in candidates for feature in coverage_features(row)}
    objectives = {"feature:" + feature for feature in present_features}
    objectives |= {f"episode:{row['episode']}" for row in candidates}
    tokens = {row["shot_id"]: {"feature:" + feature for feature in coverage_features(row)} | {f"episode:{row['episode']}"} for row in candidates}
    frequencies = Counter(token for token_set in tokens.values() for token in token_set)
    excluded = {row["shot_id"] for row in previous}
    while len(selected) < count:
        used = [*previous, *selected]
        shot_ids = excluded | {row["shot_id"] for row in selected}
        covered = set().union(*(tokens.get(row["shot_id"], set()) for row in used)) if used else set()
        episode_counts = Counter(row["episode"] for row in used)
        feature_counts = Counter(feature for row in used for feature in coverage_features(row))
        reuse_counts = Counter(row["reuse_group_id"] for row in used)
        uncovered = objectives - covered

        def score(row):
            newly_covered = tokens[row["shot_id"]] & uncovered
            gain = sum((2 if token.startswith("feature:") else 1) * (1 + 1 / math.sqrt(frequencies[token]))
                       for token in newly_covered)
            balance = sum(1 / (1 + feature_counts[feature]) for feature in coverage_features(row))
            return (gain, -episode_counts[row["episode"]], -reuse_counts[row["reuse_group_id"]], balance, clarity(row))

        available = [row for row in ordered if row["shot_id"] not in shot_ids]
        if not available:
            raise ValueError("Insufficient eligible story shots")
        winner = max(available, key=score)
        additions = sorted(tokens[winner["shot_id"]] & uncovered)
        notes[winner["shot_id"]] = additions or ["balance_episode_and_tag_coverage"]
        selected.append(winner)
    return selected, notes


def summarize_coverage(candidates, selected, alternates):
    result = {}
    for feature, description in FEATURE_NAMES.items():
        eligible = [row for row in candidates if feature in coverage_features(row)]
        result[feature] = dict(description=description, eligible_shots=len(eligible),
                               query_shot_ids=[row["shot_id"] for row in selected if feature in coverage_features(row)],
                               alternate_shot_ids=[row["shot_id"] for row in alternates if feature in coverage_features(row)])
    missing = [key for key, value in result.items() if value["eligible_shots"] and not value["query_shot_ids"]]
    if missing:
        raise ValueError(f"Coverage heuristic left available categories uncovered: {missing}; inspect before changing selection")
    return dict(features=result,
                unavailable_categories=[key for key, value in result.items() if not value["eligible_shots"]],
                episodes=dict(eligible=dict(Counter(row["episode"] for row in candidates)),
                              queries=dict(Counter(row["episode"] for row in selected)),
                              alternates=dict(Counter(row["episode"] for row in alternates))))


def export_row(row, notes, role):
    covered = sorted(coverage_features(row))
    prefix = "Required Episode 1 reference. " if row["shot_id"] == MANDATORY_SHOT else ""
    features = "; ".join(FEATURE_NAMES[feature] for feature in covered) or "additional story framing"
    reason = prefix + ("Query coverage: " if role == "query" else "Alternate coverage: ") + features + "."
    return dict(query_shot_id=row["shot_id"], frame_id=row["frame_id"],
                image_path=os.path.relpath(Path(row["image_path"]), OUT), sha256=row["sha256"],
                episode=row["episode"], start_ms=row["start_ms"], end_ms=row["end_ms"],
                frame_timestamp_ms=row["frame_timestamp_ms"], reuse_group_id=row["reuse_group_id"],
                role=role, reason=reason, selected_for=notes[row["shot_id"]], coverage_features=covered,
                tag_observations={group: row[group] for group in ShotTag.model_fields},
                detector_observation=dict(person_count=row["detector"]["person_count"],
                                          largest_person_area=row["detector"]["largest_person_area"]),
                pcp_observation=dict(top_class=row["pcp"]["top_class"], top_probability=row["pcp"]["top_probability"]),
                vlm_model=row["vlm_model"], prompt_version=row["prompt_version"])


def render(payload):
    sections = []
    for key, title in (("queries", "25 query shots"), ("alternates", "10 alternates")):
        cards = []
        for index, row in enumerate(payload[key], 1):
            tags = row["tag_observations"]
            summary = {"angle": tags["camera"]["vertical_angle"], "shot_size": tags["camera"]["shot_size"],
                       "composition": [tags["composition"]["primary"], *tags["composition"]["secondary"]],
                       "people": tags["subjects"]["person_count"], "cell": tags["subjects"]["main_subject_cell"],
                       "scale": tags["subjects"]["main_subject_scale"], "negative_space": tags["composition"]["negative_space"]}
            cards.append(f'<article><h3>{index}. {html.escape(row["query_shot_id"])}</h3>'
                         f'<img loading="lazy" src="{html.escape(quote(row["image_path"], safe="/"))}" alt="{html.escape(row["frame_id"])}">'
                         f'<p>{html.escape(row["reason"])}</p><small>Episode {row["episode"]}; frame {row["frame_timestamp_ms"]} ms</small>'
                         f'<pre>{html.escape(json.dumps(summary, ensure_ascii=False, indent=2))}</pre>'
                         f'<details><summary>Full recorded tags</summary><pre>{html.escape(json.dumps(tags, ensure_ascii=False, indent=2))}</pre></details></article>')
        sections.append(f'<section><h2>{title}</h2><div class="grid">' + "".join(cards) + '</div></section>')
    coverage_rows = "".join(f'<tr><td>{html.escape(value["description"])}</td><td>{value["eligible_shots"]}</td><td>{len(value["query_shot_ids"])}</td><td>{len(value["alternate_shot_ids"])}</td></tr>' for value in payload["coverage"]["features"].values())
    return '<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Shot query selection</title><style>body{font:15px/1.5 system-ui;background:#111820;color:#e5edf7;max-width:1500px;margin:auto;padding:24px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:20px}article{padding:15px;background:#1e2834;border:1px solid #425066;border-radius:8px}img{width:100%;height:auto}pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px}small{color:#b6c1d0}table{border-collapse:collapse}td,th{padding:7px;border:1px solid #425066}a{color:#9accff}</style><h1>Story query selection</h1><p>25 queries and 10 alternates selected by recorded VLM tag coverage, with the required Episode 1 reference. These tags are model observations; no image-similarity or relevance judgments are assigned.</p><p>Reasons describe tag coverage, not the expected relevance of any retrieval result. The user may swap a query for an alternate before ranking.</p><p><a href="queries.json">Machine-readable selection and provenance</a></p><h2>Coverage</h2><table><tr><th>Requested feature</th><th>Eligible shots</th><th>Queries</th><th>Alternates</th></tr>' + coverage_rows + '</table>' + "".join(sections) + '</html>'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    gate = json.loads((OUT / "pilot/report.json").read_text())["gate"]
    if not gate.get("passed"):
        raise ValueError("Query selection requires a passing pilot gate")
    manifest = json.loads((OUT / "exploration/manifest.json").read_text())
    if manifest.get("scope") != "exploratory_completed_subset":
        raise ValueError("Expected the authorized exploratory subset manifest")
    keyframes = rows(OUT / "keyframe_tags.jsonl")
    shot_aggregates = rows(OUT / "shot_tags.jsonl")
    if len(keyframes) != manifest["keyframes"] or len({row["frame_id"] for row in keyframes}) != len(keyframes):
        raise ValueError("Exploratory keyframe assembly is incomplete or duplicated")
    if {row["frame_id"] for row in keyframes} != {row["frame_id"] for row in rows(OUT / "exploration/frames.jsonl")}:
        raise ValueError("Exploratory assembly does not match the selected frame universe")
    representatives = [row for row in keyframes if row["is_representative"]]
    if len(representatives) != manifest["shots"] or len({row["shot_id"] for row in representatives}) != manifest["shots"]:
        raise ValueError("Exploratory representative coverage does not reconcile")
    if len(shot_aggregates) != manifest["shots"] or {row["shot_id"] for row in shot_aggregates} != {row["shot_id"] for row in representatives}:
        raise ValueError("Shot aggregates do not reconcile")
    candidates = []
    for row in representatives:
        if row["status"] != "valid":
            continue
        ShotTag.model_validate({key: row[key] for key in ShotTag.model_fields}, strict=True)
        if row["frame"]["kind"] == "story" and not row["is_op_ed"] and not row["low_information"]:
            candidates.append(row)
    if MANDATORY_SHOT not in {row["shot_id"] for row in candidates}:
        raise ValueError("Required reference is not an eligible story representative; inspect rather than relabel it")
    selected, query_notes = choose(candidates, 25, initial=[MANDATORY_SHOT])
    alternates, alternate_notes = choose(candidates, 10, previous=selected)
    coverage = summarize_coverage(candidates, selected, alternates)
    all_story_frames = [row for row in keyframes if row["status"] == "valid" and row["frame"]["kind"] == "story"
                        and not row["is_op_ed"] and not row["low_information"]]
    coverage["eligible_all_frame_counts"] = {feature: sum(feature in coverage_features(row) for row in all_story_frames)
                                             for feature in FEATURE_NAMES}
    if any(coverage["eligible_all_frame_counts"][feature] for feature in coverage["unavailable_categories"]):
        raise ValueError("An unavailable representative category exists among extra frames; inspect a documented frame override")
    if set(coverage["episodes"]["queries"]) != set(coverage["episodes"]["eligible"]):
        raise ValueError("Queries do not cover every eligible episode")
    for row in [*selected, *alternates]:
        if digest(Path(row["image_path"])) != row["sha256"]:
            raise ValueError(f"Image SHA mismatch for {row['frame_id']}")
    source_paths = ["keyframe_tags.jsonl", "shot_tags.jsonl", "exploration/manifest.json", "exploration/frames.jsonl", "pilot/report.json", "select_queries.py"]
    payload = dict(created_at=datetime.now(timezone.utc).isoformat(), seed=SEED,
                   selection_version="shot-tags-query-coverage-v1", source_hashes={name: digest(OUT / name) for name in source_paths},
                   scope=dict(kind=manifest["scope"], annotated_keyframes=manifest["keyframes"], annotated_shots=manifest["shots"],
                              limitation=manifest["selection_bias"]),
                   eligibility="schema-valid story representatives, excluding OP/ED and low_information; no tag is manually repaired",
                   method="Reserve mandatory Episode 1 reference; greedily cover all available requested feature categories and every episode, weighting new feature categories twice episode coverage with inverse-square-root rarity. Then balance episode counts and covered features. Prefer new reuse groups and clear groups when earlier score components tie; break ties by SHA-256(seed + newline + frame_id). Select alternates from remaining shots to add backup coverage. No embeddings, similarity results, or relevance labels enter selection.",
                   feature_definitions={**FEATURE_NAMES, "group": "VLM person_count >=3 or literal many", "long_tiny_figure": "shot_size long/extreme_long AND main_subject_scale tiny AND main_subject_kind person/group AND person_count >0/many", "composition_membership": "Requested composition label can be primary or secondary"},
                   eligible_shots=len(candidates), coverage=coverage,
                   queries=[export_row(row, query_notes, "query") for row in selected],
                   alternates=[export_row(row, alternate_notes, "alternate") for row in alternates],
                   counts=dict(queries=25, alternates=10, unique_shots=len({row["shot_id"] for row in [*selected, *alternates]}),
                               unique_reuse_groups=len({row["reuse_group_id"] for row in [*selected, *alternates]})),
                   relevance_judgments_created=0,
                   notes="All available requested categories are covered by representative queries. Extra story frames were also checked before declaring a category unavailable; no frame override was needed. Selection uses the episode-biased completed subset after the user stopped further tagging. It is deterministic coverage sampling, not an independent accuracy benchmark.")
    if payload["counts"]["unique_shots"] != 35:
        raise ValueError("Query and alternate shots overlap")
    (OUT / "queries.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    (OUT / "queries.html").write_text(render(payload))
    print(json.dumps(dict(counts=payload["counts"], eligible_shots=len(candidates), unavailable_categories=coverage["unavailable_categories"])))


if __name__ == "__main__":
    main()
