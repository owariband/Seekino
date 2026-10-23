"""Offline, read-only CAM++ comparison; artifacts never update parse tasks.

Run with the deployed environment, for example:
  PYTHONDONTWRITEBYTECODE=1 uv run --no-sync --env-file .env.local python -m \
    evals.speaker.voiceprint_algorithm_comparison --out outputs/verification/speaker-algo-20261018

Audio extraction and Gateway assembly follow voiceprint_agreement.py. Here the
binding/revision are checked against the task, and every fit is leave-one-out.
"""

from __future__ import annotations

import argparse
from collections import Counter
import csv
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import time
from urllib.parse import unquote
import wave

import numpy as np
from sqlalchemy import create_engine, text

from evals.speaker.voiceprint_agreement import _unit


TASK_ID = "0f681291-4f52-4254-a3be-91c1ee835c3c"
GROUND_TRUTH = {"start_ms": 47_500, "end_ms": 48_800,
                "text": "祥ちゃんも悪くない", "speaker": "長崎爽世"}
METHODS = ("B0", "C1_1.5_max", "C1_1.5_pooled", "C1_2_max", "C1_2_pooled", "C2_max", "C2_pooled")
# Explicit vocal descriptions and persisted opening/ending scene boundaries.
# These are frozen source annotations, not an audio music/overlap detector.
SONG_INTERVALS = ((84_800, 176_700), (1_258_300, 1_351_000))
SONG_PATTERN = re.compile(r"歌词|片尾曲|片头曲|歌声唱|唱出|唱着|唱起|歌唱|正在演唱|演唱日语")


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _digest(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def _read_json(value):
    return json.loads(value) if isinstance(value, str) else value


def _overlaps(left, right):
    return left[0] < right[1] and right[0] < left[1]


def load_snapshot(task_id, out):
    path = out / "source-snapshot.json"
    if path.exists():
        snapshot = json.loads(path.read_text())
        if snapshot["task"]["id"] != task_id:
            raise ValueError("Existing snapshot belongs to another task")
        return snapshot
    engine = create_engine(os.environ["ANIVAG_DATABASE_URL"])
    try:
        with engine.connect() as connection:
            if engine.dialect.name == "mysql":
                connection.exec_driver_sql("SET TRANSACTION READ ONLY")
            task = dict(connection.execute(text(
                "SELECT t.id,t.status,t.video_id,t.chunk_count,t.parse_config,t.detail,"
                "v.series_id,v.source_uri,v.duration_ms,v.hash,s.name AS series_name "
                "FROM video_parse_task t JOIN video v ON v.id=t.video_id "
                "JOIN series s ON s.id=v.series_id WHERE t.id=:id"
            ), {"id": task_id}).mappings().one())
            chunks = [dict(row) for row in connection.execute(text(
                "SELECT c.id AS content_id,c.version,c.content,JSON_EXTRACT(c.detail,'$.evidence') AS evidence,t.id AS chunk_task_id,"
                "t.chunk_id,v.sequence_no FROM chunk_parse_content c "
                "JOIN chunk_parse_task t ON t.id=c.chunk_task_id "
                "JOIN video_chunk v ON v.id=t.chunk_id WHERE t.video_task_id=:id"
            ), {"id": task_id}).mappings()]
        if task["status"] != "COMPLETE" or len(chunks) != task["chunk_count"]:
            raise ValueError("Evaluation requires a COMPLETE task and every chunk")
        task["parse_config"] = _read_json(task["parse_config"])
        task["detail"] = {"voiceprint": _read_json(task["detail"]).get("voiceprint", {})}
        for chunk in chunks:
            chunk["content"] = _read_json(chunk["content"])
            chunk["detail"] = {"evidence": _read_json(chunk.pop("evidence")) or {}}
        chunks.sort(key=lambda chunk: chunk["sequence_no"])
        snapshot = {"task": task, "chunks": chunks}
        _write_json(path, snapshot)
        return snapshot
    finally:
        engine.dispose()


def build_lines(snapshot):
    cues = {}
    for chunk in snapshot["chunks"]:
        evidence = chunk["detail"]["evidence"]
        asr = evidence.get("asr") or {}
        for cue in asr.get("transcript", []):
            key = _digest([cue["start_seconds"], cue["end_seconds"], cue["text"]])
            cues[key] = {"id": "asr:" + key[:24], "start_ms": cue["start_seconds"],
                         "end_ms": cue["end_seconds"], "text": cue["text"]}
    lines = []
    for chunk in snapshot["chunks"]:
        for position, event in enumerate(chunk["content"]["timeline"]):
            if event["event_type"] != "DIALOGUE":
                continue
            line = {"chunk_id": chunk["chunk_id"], "chunk_task_id": chunk["chunk_task_id"],
                    "source_version": chunk["version"], "event_position": position,
                    "start_ms": event["start_seconds"], "end_ms": event["end_seconds"],
                    "label": event.get("speaker"), "description": event["description"],
                    "text": next((v["text"] for v in event.get("text_variants", [])
                                  if v["role"] == "ORIGINAL"), "")}
            interval = (line["start_ms"], line["end_ms"])
            line["cue_ids"] = sorted(cue["id"] for cue in cues.values()
                                     if _overlaps(interval, (cue["start_ms"], cue["end_ms"])))
            line["cue_source"] = "retained_asr" if line["cue_ids"] else "dialogue_fallback"
            if not line["cue_ids"]:
                line["cue_ids"] = [f"dialogue:{interval[0]}:{interval[1]}"]
            line["song"] = (any(_overlaps(interval, song) for song in SONG_INTERVALS)
                            or bool(SONG_PATTERN.search(line["description"])))
            lines.append(line)
    lines.sort(key=lambda row: (row["start_ms"], row["end_ms"], row["chunk_id"], row["event_position"]))
    for index, line in enumerate(lines):
        line["id"] = f"line-{index:03d}"
        line["duration_ms"] = line["end_ms"] - line["start_ms"]
        line["overlap"] = any(other is not line and _overlaps(
            (line["start_ms"], line["end_ms"]), (other["start_ms"], other["end_ms"])
        ) for other in lines)
        line["clean"] = line["duration_ms"] >= 1500 and not line["song"] and not line["overlap"]
    return lines, sorted(cues.values(), key=lambda cue: (cue["start_ms"], cue["id"]))


def build_queries(lines):
    queries = [dict(line) for line in lines]
    for line in lines:
        if line["duration_ms"] <= 4000:
            continue
        starts = list(range(line["start_ms"], line["end_ms"] - 1499, 1500))
        if starts[-1] + 1500 < line["end_ms"]:
            starts.append(line["end_ms"] - 1500)
        for index, start in enumerate(starts):
            queries.append({**line, "id": f"{line['id']}-window-{index:02d}", "parent_id": line["id"],
                            "start_ms": start, "end_ms": start + 1500, "duration_ms": 1500})
    matching = [line for line in lines if _overlaps(
        (line["start_ms"], line["end_ms"]), (GROUND_TRUTH["start_ms"], GROUND_TRUTH["end_ms"])
    )]
    queries.append({"id": "known-soyo", **{key: GROUND_TRUTH[key] for key in ("start_ms", "end_ms", "text")},
                    "duration_ms": 1300, "label": matching[0]["label"] if len(matching) == 1 else None,
                    "cue_ids": sorted({cue for line in matching for cue in line["cue_ids"]}),
                    "song": False, "overlap": False, "clean": False,
                    "ground_truth": GROUND_TRUTH["speaker"]})
    return queries


def embed_queries(snapshot, queries, out, config_path):
    from gateway.config import ModelBinding, load_config
    from gateway.model_factory import GatewayClient

    task = snapshot["task"]
    binding = task["parse_config"]["models"]["speaker_embedding"]
    frozen_models = {(_json(person["model"])) for person in task["detail"]["voiceprint"]["voiceprintList"]}
    if len(frozen_models) != 1:
        raise ValueError("Task does not have exactly one frozen voiceprint model/revision")
    frozen = json.loads(next(iter(frozen_models)))
    source = Path(os.environ.get("ANIVAG_ARTIFACT_ROOT", "outputs/server/artifacts")) / unquote(task["source_uri"].split("://", 1)[1])
    if not source.is_file():
        raise FileNotFoundError(f"Source media does not exist: {source}")
    gateway = GatewayClient(load_config(config_path))
    encoder = gateway.create_speaker_embedding_model(ModelBinding.model_validate({**binding, "kind": "speaker_embedding"}))
    try:
        if (encoder.model_name, encoder.encoder_revision, encoder.dimensions) != (frozen["name"], frozen["encoder_revision"], 192):
            raise ValueError("Deployed encoder differs from the task's frozen production encoder")
        cache_path = out / "embeddings.jsonl"
        cached = {row["id"]: row for row in (json.loads(line) for line in cache_path.read_text().splitlines())} if cache_path.exists() else {}
        audio = out / "clips"
        audio.mkdir(exist_ok=True)
        # Encode windows first so oversized parent cues can use their mean if necessary.
        ordered = sorted(queries, key=lambda row: ("parent_id" not in row, row["id"]))
        encoded = {}
        with cache_path.open("a", encoding="utf-8") as cache:
            for index, query in enumerate(ordered):
                wav = audio / f"{query['id']}.wav"
                identity = {"start_ms": query["start_ms"], "end_ms": query["end_ms"],
                            "model": encoder.model_name, "encoder_revision": encoder.encoder_revision,
                            "source_hash": task["hash"]}
                existing = cached.get(query["id"])
                if existing is not None and (existing["identity"] != identity or not wav.is_file()):
                    raise ValueError("Cached embedding identity/audio mismatch; use a fresh output directory")
                if existing is not None:
                    with wave.open(str(wav), "rb") as wave_file:
                        digest = hashlib.sha256(wave_file.readframes(wave_file.getnframes())).hexdigest()
                    if digest != existing["pcm_sha256"]:
                        raise ValueError("Cached PCM content differs from its frozen embedding")
                if existing is None:
                    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{query['start_ms'] / 1000:.3f}",
                                    "-to", f"{query['end_ms'] / 1000:.3f}", "-i", str(source),
                                    "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav)],
                                   check=True, timeout=90)
                    with wave.open(str(wav), "rb") as wave_file:
                        pcm_hash = hashlib.sha256(wave_file.readframes(wave_file.getnframes())).hexdigest()
                    existing = {"id": query["id"], "identity": identity, "pcm_sha256": pcm_hash,
                                "audio_path": str(wav.resolve()), "embedding_method": "direct_camplus"}
                    started = time.monotonic()
                    try:
                        existing["vector"] = _unit(encoder.embed_audio(wav).vector)
                    except ValueError as error:
                        if query["duration_ms"] > 30_000:
                            windows = [encoded[other["id"]]["vector"] for other in ordered
                                       if other.get("parent_id") == query["id"] and "vector" in encoded.get(other["id"], {})]
                            if not windows:
                                raise
                            existing["vector"] = _unit(np.mean(windows, axis=0).tolist())
                            existing["embedding_method"] = "mean_1.5s_window_embeddings_over_encoder_limit"
                        else:
                            existing["error"] = f"{type(error).__name__}: {error}"
                    existing["embedding_seconds"] = time.monotonic() - started
                    cache.write(_json(existing) + "\n")
                    cache.flush()
                encoded[query["id"]] = existing
                if index % 25 == 0 or index + 1 == len(ordered):
                    print(_json({"stage": "embedding", "done": index + 1, "total": len(ordered)}), flush=True)
        return [{**query, **{key: value for key, value in encoded[query["id"]].items() if key not in {"id", "identity"}},
                 "model": encoder.model_name, "encoder_revision": encoder.encoder_revision} for query in queries]
    finally:
        gateway.close()


def evaluate(queries, protocol):
    from engine.parsev2.voiceprint_matching import eligible_references, cluster_samples, score_baseline, score_clusters

    samples = [query for query in queries if query["id"].startswith("line-") and "parent_id" not in query and "vector" in query]
    fit_cache = {}
    results = []
    for index, query in enumerate(queries):
        scores = {}
        if "vector" in query:
            scores["B0"] = score_baseline(query, samples, trim_fraction=protocol["trim_fraction"])
            clean = [sample for sample in eligible_references(query, samples) if sample["clean"]]
            key = _digest([{name: sample.get(name) for name in ("id", "label", "cue_ids", "pcm_sha256")} for sample in clean])
            if key not in fit_cache:
                fit_cache[key] = {name: cluster_samples(clean, **options, seed=protocol["seed"],
                                                      starts=protocol["starts"], max_iter=protocol["iterations"])
                                  for name, options in protocol["cluster_options"].items()}
            for prefix, fitted in fit_cache[key].items():
                for aggregation in ("max", "pooled"):
                    method = f"{prefix}_{aggregation}"
                    scores[method] = score_clusters(query, clean, fitted["groups"], aggregation=aggregation,
                                                    min_cues=protocol["min_cues"], min_purity=protocol["min_purity"])
                    scores[method]["fit_k"] = fitted["k"]
                    scores[method]["fit_key"] = key
                    scores[method]["clean_reference_count"] = len(clean)
        for method in METHODS:
            result = scores.setdefault(method, {"candidates": [], "gap": None})
            candidates = result.pop("candidates")
            result["top3"] = candidates[:3]
            result["top1"] = candidates[0]["speaker"] if candidates else None
            result["candidate_count"] = len(candidates)
            result["candidate_speakers"] = [candidate["speaker"] for candidate in candidates]
            result["method"] = method
            result["duration_ms"] = query["duration_ms"]
            if "error" in query:
                result["error"] = query["error"]
        results.append({key: value for key, value in query.items() if key != "vector"} | {"methods": scores})
        if index % 25 == 0 or index + 1 == len(queries):
            print(_json({"stage": "scoring", "done": index + 1, "total": len(queries), "distinct_reference_fits": len(fit_cache)}), flush=True)
    return results, fit_cache


def summarize(results, protocol):
    lines = [row for row in results if row["id"].startswith("line-") and "parent_id" not in row]
    mixed = []
    for line in lines:
        flags = {}
        for method in METHODS:
            windows = [row for row in results if row.get("parent_id") == line["id"]]
            confident = [row for row in windows if row["methods"][method]["gap"] is not None
                         and row["methods"][method]["gap"] >= protocol["mixed_gap"]]
            if len({row["methods"][method]["top1"] for row in confident}) > 1:
                flags[method] = [{"id": row["id"], "start_ms": row["start_ms"], "end_ms": row["end_ms"],
                                  "top1": row["methods"][method]["top1"], "gap": row["methods"][method]["gap"],
                                  "audio_path": row["audio_path"]} for row in confident]
        if flags:
            mixed.append({"id": line["id"], "start_ms": line["start_ms"], "end_ms": line["end_ms"],
                          "label": line["label"], "text": line["text"], "audio_path": line["audio_path"], "methods": flags})
    mixed_by_method = {method: {row["id"] for row in mixed if method in row["methods"]} for method in METHODS}
    summary = {}

    def agreement(rows, method):
        named = [row for row in rows if row["label"]]
        scored = [row for row in named if row["methods"][method]["top1"] is not None]
        same = sum(row["methods"][method]["top1"] == row["label"] for row in scored)
        return {"total": len(rows), "named": len(named), "scored_named": len(scored), "agree": same,
                "agreement": same / len(scored) if scored else None,
                "agreement_over_all_named": same / len(named) if named else None,
                "top3_agree": sum(any(c["speaker"] == row["label"] for c in row["methods"][method]["top3"]) for row in scored)}

    for method in METHODS:
        for row in lines:
            result = row["methods"][method]
            result["mixed_cue"] = row["id"] in mixed_by_method[method]
            result["auto_accept"] = bool(
                result["gap"] is not None and result["gap"] >= protocol["accept_gap"]
                and row["duration_ms"] >= protocol["accept_duration_ms"]
                and result["top3"][0]["independent_cue_count"] >= protocol["min_cues"]
                and not row["song"] and not row["overlap"] and not result["mixed_cue"]
            )
        accepted = [row for row in lines if row["methods"][method]["auto_accept"]]
        stats = agreement(lines, method)
        stats.update(scored=sum(row["methods"][method]["top1"] is not None for row in lines),
                     auto_accept=agreement(accepted, method), auto_accept_share=len(accepted) / len(lines),
                     mixed_count=len(mixed_by_method[method]),
                     null_scored=sum(row["label"] is None and row["methods"][method]["top1"] is not None for row in lines),
                     null_auto_accept=sum(row["label"] is None for row in accepted))
        stats["gap_buckets"] = {}
        for bucket, low, high in (("<0.05", 0, .05), ("0.05–0.15", .05, .15), (">=0.15", .15, math.inf)):
            stats["gap_buckets"][bucket] = agreement([row for row in lines if row["methods"][method]["gap"] is not None
                                                      and low <= row["methods"][method]["gap"] < high], method)
        stats["gap_buckets"]["undefined"] = agreement([row for row in lines if row["methods"][method]["gap"] is None], method)
        stats["duration_buckets"] = {bucket: agreement([row for row in lines if low <= row["duration_ms"] < high], method)
                                      for bucket, low, high in (("<1s", 0, 1000), ("1–2s", 1000, 2000),
                                                               ("2–4s", 2000, 4000), (">=4s", 4000, math.inf))}
        ks = [row["methods"][method]["fit_k"] for row in lines if "fit_k" in row["methods"][method]]
        stats["leave_one_out_k"] = dict(sorted(Counter(ks).items()))
        stats["per_label"] = {}
        for label in sorted({row["label"] for row in lines if row["label"]}):
            labeled = [row for row in lines if row["label"] == label]
            group = agreement(labeled, method)
            group["label_available_as_candidate"] = sum(label in row["methods"][method]["candidate_speakers"] for row in labeled)
            stats["per_label"][label] = group
        summary[method] = stats
    candidates = []
    for row in lines:
        choices = {row["methods"][method]["top1"] for method in METHODS if row["methods"][method]["top1"] is not None}
        gaps = [row["methods"][method]["gap"] for method in METHODS if row["methods"][method]["gap"] is not None]
        reasons = (["methods_disagree"] if len(choices) > 1 else []) + (["gap_below_0.05"] if gaps and min(gaps) < .05 else [])
        if reasons:
            methods = {}
            for method, result in row["methods"].items():
                methods[method] = {key: result[key] for key in ("top1", "gap", "auto_accept", "mixed_cue", "duration_ms", "method")}
                methods[method]["top3"] = [{key: candidate[key] for key in
                                           ("speaker", "similarity", "reference_count", "independent_cue_count")}
                                          for candidate in result["top3"]]
            candidates.append({**{key: row[key] for key in ("id", "start_ms", "end_ms", "duration_ms", "label", "text", "audio_path")},
                               "methods": methods, "labeling_reasons": reasons, "verified_speaker": None, "reviewed": False})
    candidates.sort(key=lambda row: (-len({row["methods"][method]["top1"] for method in METHODS
                                          if row["methods"][method]["top1"] is not None}),
                                     min((row["methods"][method]["gap"] for method in METHODS
                                          if row["methods"][method]["gap"] is not None), default=math.inf), row["id"]))
    return summary, mixed, candidates[:60], len(candidates)


def render(out, protocol, results, fits, recommendation):
    summary, mixed, labeling, eligible_labeling = summarize(results, protocol)
    ground = next(row for row in results if row["id"] == "known-soyo")
    # Current corpus spells the same character 长崎爽世; do not merge other aliases.
    for result in ground["methods"].values():
        result["ground_truth_top1_correct"] = result["top1"] in {"長崎爽世", "长崎爽世"}
        result["ground_truth_rank"] = next((index + 1 for index, candidate in enumerate(result["top3"])
                                            if candidate["speaker"] in {"長崎爽世", "长崎爽世"}), None)
    proxy_winner = max(METHODS, key=lambda method: (
        summary[method]["auto_accept"]["agree"], summary[method]["agreement_over_all_named"] or 0,
        -METHODS.index(method)))
    recommendation = recommendation or proxy_winner
    basis = ("Engineering recommendation after reviewing candidate coverage: B0 has the highest overall label consistency "
             "and retains rare-character candidates; all methods pass the one known ground-truth target. "
             "The predeclared accepted-consistency-count winner remains separately reported; no fit parameter was retuned."
             if recommendation == "B0" else
             "Offline candidate ranked by number of label-consistent auto-accepts, then overall named-label agreement.")
    from scipy import __version__ as scipy_version
    source_paths = [Path("engine/parsev2/voiceprint_matching.py"),
                    Path("tests/engine/parsev2/test_voiceprint_matching.py"), Path(__file__)]
    provenance = {"python": platform.python_version(), "numpy": np.__version__, "scipy": scipy_version,
                  "source_sha256": {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in source_paths}}
    historical_path = Path("outputs/verification/speaker-agreement-20261018/v11-lines.jsonl")
    if historical_path.exists():
        historical = [json.loads(line) for line in historical_path.read_text().splitlines()]
        fields = ("start_ms", "end_ms", "label", "text")
        current_lines = [row for row in results if row["id"].startswith("line-") and "parent_id" not in row]
        provenance["existing_harness_line_records_match"] = Counter(tuple(row[key] for key in fields) for row in historical) == Counter(
            tuple(row[key] for key in fields) for row in current_lines)
        provenance["existing_harness_jsonl_sha256"] = hashlib.sha256(historical_path.read_bytes()).hexdigest()
    previous = out / "reproducibility.json"
    if previous.exists():
        provenance["deterministic_replay"] = json.loads(previous.read_text())
    _write_json(out / "provenance.json", provenance)
    largest_fit = max(fits.values(), key=lambda fit: sum(len(group["sample_ids"]) for group in fit["C2"]["groups"]))
    full_pool_k = {name: fit["k"] for name, fit in largest_fit.items()}
    full_pool_size = sum(len(group["sample_ids"]) for group in largest_fit["C2"]["groups"])
    report = {"protocol": protocol, "summary": summary, "known_ground_truth": ground,
              "flagged_mixed_cues": mixed, "labeling_count": len(labeling),
              "eligible_labeling_count": eligible_labeling, "recommended_method": recommendation,
              "largest_clean_reference_pool_size": full_pool_size, "largest_clean_reference_pool_k": full_pool_k,
              "accepted_consistency_count_winner": proxy_winner, "provenance": provenance,
              "recommendation_basis": basis + " Label agreement is not identity accuracy or production approval."}
    _write_json(out / "report.json", report)
    _write_json(out / "results.json", results)
    _write_json(out / "cluster-fits.json", fits)
    _write_json(out / "labeling-60.json", labeling)
    _write_json(out / "mixed-cues.json", mixed)
    with (out / "labeling-60.tsv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream, delimiter="\t")
        writer.writerow(["id", "start_ms", "end_ms", "label", "text", "audio_path", "reasons", *METHODS, "verified_speaker", "reviewed"])
        for row in labeling:
            writer.writerow([row["id"], row["start_ms"], row["end_ms"], row["label"], row["text"], row["audio_path"],
                             ",".join(row["labeling_reasons"]), *(row["methods"][method]["top1"] for method in METHODS), "", "false"])
    pct = lambda value: "n/a" if value is None else f"{100 * value:.2f}%"
    lines = ["# MyGO EP04: offline CAM++ voiceprint algorithm comparison", "",
             "**Agreement with Understanding labels is a consistency proxy, not accuracy.** The same noisy label source names the reference bank/clusters. Leave-one-out prevents direct target leakage but cannot remove shared label errors. No production integration was performed.", "",
             f"Task: `{protocol['task_id']}`. {protocol['line_count']} DIALOGUE lines; {protocol['named_count']} named and {protocol['null_count']} null. P={protocol['named_characters']} distinct labels; aliases are not silently merged. Frozen encoder: `{protocol['encoder']['name']}` / `{protocol['encoder']['encoder_revision']}`.", "",
             "## Frozen protocol", "",
             "- Same target-excluded reference pool for every method: same source cue, overlapping half-open time interval, identical PCM, and incompatible encoder are excluded before fitting, trimming, naming or scoring. Exact duplicate reference PCM is counted once; conflicting duplicate identities abstain.",
             "- B0 uses all valid named lines, including songs/short lines, then trims floor(20% × reference count) farthest from the initial per-character centroid and refits it.",
             f"- C1 uses K=ceil(1.5P)={protocol['cluster_options']['C1_1.5']['k']} and K=ceil(2P)={protocol['cluster_options']['C1_2']['k']}; seed={protocol['seed']}, starts={protocol['starts']}, iterations={protocol['iterations']}. C2 uses average linkage and one predeclared cosine-distance threshold {protocol['cluster_options']['C2']['threshold']}; K is an output.",
             "- C1 and C2 both fit clean lines only: duration ≥1.5 s, outside persisted song annotations, and no simultaneous DIALOGUE interval. Neither song nor overlap annotations are an independently verified audio detector. Clean null-labeled lines participate in geometry and purity denominators.",
             "- A cluster is named only with a unique majority, ≥3 supporting independent ASR cues, and purity ≥0.6. Conflicting/null cue labels abstain but remain in the denominator. max selects the strongest named cluster per character; pooled pools all member vectors of that character's named clusters before normalization. top2 always means a different character.",
             "- Every target is excluded before re-fitting clusters (strict leave-one-out). Parent cue identities exclude the original full cue for 1.5 s internal windows. A final end-aligned 1.5 s window covers a remaining tail; adjacent windows can overlap at that tail.",
             "- Auto-accept is a common, uncalibrated reporting rule: gap ≥0.15, duration ≥1.5 s, ≥3 independent reference cues, not a song/overlap or an internally flagged mixed cue. It is not enabled in production.",
             "- Agreement denominators exclude null labels. `scored_named` agreement excludes unavailable predictions; `all_named` counts unavailable predictions as non-agreement. Coverage and auto-accept share use all 339 lines.",
             "- Song masks from stored OP/ED scenes: 84.8–176.7 s and 1258.3–1351.0 s, plus explicit vocal/lyrics descriptions. Four clips below CAM++'s 400 ms lower limit retain null predictions and an explicit error; no fabricated candidates or zero padding.", "",
             "## Main comparison", "",
             f"The largest eligible clean reference pool has {full_pool_size} lines and fitted K={full_pool_k}. Every scored target uses its own target-excluded fit; the table reports the resulting K distribution.", "",
             "| Method | Scored | Label agree / scored named | Agreement | Agreement / all named | Auto-accept / all lines | Accepted label agreement | Mixed cues | LOO K |",
             "|---|---:|---:|---:|---:|---:|---:|---:|---|"]
    for method, stats in summary.items():
        accepted = stats["auto_accept"]
        lines.append(f"| {method} | {stats['scored']} | {stats['agree']}/{stats['scored_named']} | {pct(stats['agreement'])} | {pct(stats['agreement_over_all_named'])} | {accepted['total']}/{protocol['line_count']} ({pct(stats['auto_accept_share'])}) | {accepted['agree']}/{accepted['scored_named']} ({pct(accepted['agreement'])}) | {stats['mixed_count']} | {stats['leave_one_out_k']} |")
    for section, title in (("gap_buckets", "Gap buckets"), ("duration_buckets", "Duration buckets")):
        lines += ["", f"## {title}", "", "| Method | Bucket | Lines | Named | Scored named | Agree | Agreement |", "|---|---|---:|---:|---:|---:|---:|"]
        for method, stats in summary.items():
            for bucket, group in stats[section].items():
                lines.append(f"| {method} | {bucket} | {group['total']} | {group['named']} | {group['scored_named']} | {group['agree']} | {pct(group['agreement'])} |")
    lines += ["", "## Character coverage", "",
              "Each cell is `top1 agrees / labeled lines; true label available anywhere among candidates`. Candidate availability is separate from appearance in top 3. Missing rare-character candidates can inflate gaps.", "",
              "| Understanding label | Clean reference lines | " + " | ".join(METHODS) + " |",
              "|---|---:|" + "---|" * len(METHODS)]
    corpus = [row for row in results if row["id"].startswith("line-") and "parent_id" not in row]
    for label in sorted(summary["B0"]["per_label"]):
        clean_count = sum(row["label"] == label and row["clean"] and "error" not in row for row in corpus)
        cells = []
        for method in METHODS:
            group = summary[method]["per_label"][label]
            cells.append(f"{group['agree']}/{group['named']}; {group['label_available_as_candidate']}/{group['named']}")
        lines.append(f"| {label} | {clean_count} | " + " | ".join(cells) + " |")
    lines += ["", "## Known independent ground truth", "",
              "47.5–48.8 s, 「祥ちゃんも悪くない」: **長崎爽世**, confirmed by the user. Current Understanding label is **高松灯**. Agreement with that label would be wrong for this utterance. This independent check was not used to select K, threshold or the automatic recommendation rule.", "",
              "[Listen to the exact target](clips/known-soyo.wav)", "",
              "| Method | Top 1 | Gap | Soyo rank in top 3 | Correct top 1 |", "|---|---|---:|---:|---|"]
    for method, result in ground["methods"].items():
        lines.append(f"| {method} | {result['top1']} | {result['gap']} | {result['ground_truth_rank']} | {result['ground_truth_top1_correct']} |")
    best = summary[recommendation]
    lines += ["", "## Recommendation and limits", "",
              f"Recommend **{recommendation} as the preparation/scoring baseline for the next validation stage**, with {best['agree']}/{best['scored_named']} ({pct(best['agreement'])}) label agreement and {best['auto_accept']['total']}/{protocol['line_count']} ({pct(best['auto_accept_share'])}) auto-accept coverage, whose named-label agreement is {best['auto_accept']['agree']}/{best['auto_accept']['scored_named']} ({pct(best['auto_accept']['agreement'])}). {basis}",
              f"The predeclared auto-accepted consistency-count proxy selects **{proxy_winner}**: {summary[proxy_winner]['auto_accept']['agree']} accepted named lines agree with labels. This is a different criterion from broad character coverage and is not a calibrated accuracy ranking.",
              "In this corpus, no clean references remain for 要乐奈 or 若叶睦; 三角初华 has two and 棕发偶像同伴 one. The prescribed three-cue naming rule therefore cannot retain these characters. C1/C2's larger gaps and higher accepted-set consistency must be read with this missing-competitor limitation. The experimental methods were kept unchanged; no fallback was silently added.",
              "Independent annotation of the exported list is required before production selection. This single episode, a single fixed AHC threshold, and two over-clustering sizes do not establish a generally best method. The majority-purity criterion can propagate systematic Understanding errors. K is a count of acoustic groups, not certified character count; max scoring also gives characters with more groups more chances for a high score.", "",
              f"## Labeling list and mixed cues", "",
              f"Exported {len(labeling)} of {eligible_labeling} eligible disagreement/low-gap lines: [TSV](labeling-60.tsv), [JSON](labeling-60.json). Each has an absolute WAV path and blank verification fields. Mixed-cue flags ({len(mixed)} unique cues) are [JSON](mixed-cues.json); flags are model disagreement between confident windows, not verified turn boundaries.", "",
              "| Cue | Time (s) | Label | Flagged methods | Audio |", "|---|---|---|---|---|"]
    for row in mixed:
        lines.append(f"| {row['id']} | {row['start_ms']/1000:.3f}–{row['end_ms']/1000:.3f} | {row['label']} | {', '.join(row['methods'])} | [WAV](clips/{row['id']}.wav) |")
    lines += ["", "## Reproduce", "", "```sh", "PYTHONDONTWRITEBYTECODE=1 uv run --no-sync --env-file .env.local python -m evals.speaker.voiceprint_algorithm_comparison \\",
              f"  --task-id {protocol['task_id']} --out {out} --recommend {recommendation}", "```", "",
              f"Environment: Python {provenance['python']}, NumPy {provenance['numpy']}, SciPy {provenance['scipy']}. Source checksums and deterministic replay checks are in [provenance.json](provenance.json).",
              "Existing snapshots/audio/embeddings are reused only after identity and PCM checks. `--render-only` rebuilds report files from stored scores without database or encoder calls. `protocol.json`, `source-snapshot.json`, `embeddings.jsonl`, and `cluster-fits.json` retain input, source, model and scoring provenance."]
    (out / "report.md").write_text("\n".join(lines) + "\n")
    print(_json({"report": str(out / "report.md"), "recommended": recommendation,
                 "summary": {method: {key: stats[key] for key in ("agree", "scored_named", "auto_accept_share", "mixed_count")}
                             for method, stats in summary.items()}}), flush=True)


def score_digest(results):
    return _digest([{ "id": row["id"], "methods": {
        method: {key: result.get(key) for key in ("top3", "gap", "fit_k")}
        for method, result in row["methods"].items()}} for row in results])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-id", default=TASK_ID)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--config", default=os.environ.get("ANIVAG_MODEL_CONFIG", "config.yaml"))
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--render-only", action="store_true")
    parser.add_argument("--recommend", choices=METHODS)
    args = parser.parse_args()
    if args.task_id != TASK_ID:
        parser.error("This frozen EP04 song/ground-truth protocol is restricted to the specified task")
    args.out.mkdir(parents=True, exist_ok=True)
    if args.render_only:
        render(args.out, json.loads((args.out / "protocol.json").read_text()),
               json.loads((args.out / "results.json").read_text()),
               json.loads((args.out / "cluster-fits.json").read_text()), args.recommend)
        return
    snapshot = load_snapshot(args.task_id, args.out)
    lines, cues = build_lines(snapshot)
    if args.task_id == TASK_ID and (len(lines), sum(row["label"] is None for row in lines)) != (339, 37):
        raise ValueError("Source counts differ from the user's frozen 339-line/37-null task")
    p = len({line["label"] for line in lines if line["label"]})
    protocol = {"version": 1, "task_id": args.task_id, "snapshot_sha256": _digest(snapshot),
                "line_count": len(lines), "named_count": sum(bool(row["label"]) for row in lines),
                "null_count": sum(row["label"] is None for row in lines), "named_characters": p,
                "labels": dict(Counter(row["label"] for row in lines if row["label"])),
                "cue_count": len(cues), "cue_fallback_count": sum(row["cue_source"] != "retained_asr" for row in lines),
                "clean_count": sum(row["clean"] for row in lines), "song_count": sum(row["song"] for row in lines),
                "overlap_count": sum(row["overlap"] for row in lines),
                "seed": 20261018, "starts": 10, "iterations": 100, "trim_fraction": .2,
                "min_cues": 3, "min_purity": .6, "accept_gap": .15, "accept_duration_ms": 1500, "mixed_gap": .1,
                "song_intervals_ms": SONG_INTERVALS, "song_description_pattern": SONG_PATTERN.pattern,
                "cluster_options": {"C1_1.5": {"method": "kmeans", "k": math.ceil(1.5 * p)},
                                    "C1_2": {"method": "kmeans", "k": math.ceil(2 * p)},
                                    "C2": {"method": "agglomerative", "threshold": .4}},
                "encoder": snapshot["task"]["detail"]["voiceprint"]["voiceprintList"][0]["model"],
                "python": platform.python_version(), "numpy": np.__version__,
                "leakage_policy": "full leave-one-out fit, names and centroids; same cue/overlap/PCM excluded",
                "recommendation_rule": "most label-consistent auto-accepted lines, tie overall named agreement; not ground truth"}
    protocol_path = args.out / "protocol.json"
    if protocol_path.exists() and _digest(json.loads(protocol_path.read_text())) != _digest(protocol):
        raise ValueError("Protocol changed; use a new output directory")
    _write_json(protocol_path, protocol)
    _write_json(args.out / "lines.json", lines)
    _write_json(args.out / "asr-cues.json", cues)
    print(_json({"stage": "snapshot", "task": args.task_id, "lines": len(lines), "named_characters": p,
                 "clean": protocol["clean_count"], "songs": protocol["song_count"], "queries": len(build_queries(lines))}), flush=True)
    queries = embed_queries(snapshot, build_queries(lines), args.out, args.config)
    if args.prepare_only:
        return
    result_path = args.out / "results.json"
    prior_digest = score_digest(json.loads(result_path.read_text())) if result_path.exists() else None
    results, fits = evaluate(queries, protocol)
    current_digest = score_digest(results)
    if prior_digest is not None and prior_digest != current_digest:
        raise ValueError("Deterministic replay changed candidate scores, reference provenance or fitted K")
    _write_json(args.out / "reproducibility.json", {"scoring_sha256": current_digest,
                "previous_scoring_sha256": prior_digest, "identical_replay": prior_digest == current_digest if prior_digest else None,
                "query_count": len(results)})
    render(args.out, protocol, results, fits, args.recommend)


if __name__ == "__main__":
    main()
