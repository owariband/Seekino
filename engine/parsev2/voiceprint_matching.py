"""Pure, deterministic speaker-matching baselines for offline evaluation.

Inputs use integer milliseconds and frozen encoder embeddings. Similarities and
gaps are cosine scores, not calibrated probabilities. No task or storage state
is read or changed here.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Collection, Mapping, Sequence
from math import floor, isfinite
from typing import Any
import warnings

import numpy as np


Sample = Mapping[str, Any]


def eligible_references(
    query: Sample, samples: Sequence[Sample], *, excluded_reference_ids: Collection[str] = (),
) -> list[dict[str, Any]]:
    """Exclude target leakage, then retain one deterministic row per PCM.

    Conflicting names on identical PCM cannot supply an identity vote, even if
    one conflicting row is excluded because its source overlaps the query.
    Half-open intervals that merely touch do not overlap. Excluded IDs cannot
    contribute vectors, but their original names still participate in PCM
    conflict detection.
    """
    query_cues = set(query.get("cue_ids", ()))
    by_pcm: dict[str, list[Sample]] = defaultdict(list)
    for sample in sorted(samples, key=lambda row: str(row["id"])):
        if (sample.get("model"), sample.get("encoder_revision")) != (
            query.get("model"), query.get("encoder_revision")
        ):
            continue
        by_pcm[str(sample["pcm_sha256"])].append(sample)

    references = []
    for pcm, duplicates in by_pcm.items():
        if pcm == query["pcm_sha256"]:
            continue
        allowed = [sample for sample in duplicates if not (
            sample["id"] == query["id"]
            or sample["id"] in excluded_reference_ids
            or query_cues.intersection(sample.get("cue_ids", ()))
            or (query["start_ms"] < sample["end_ms"]
                and sample["start_ms"] < query["end_ms"])
        )]
        if not allowed:
            continue
        reference = dict(allowed[0])
        names = {sample.get("label") for sample in duplicates if sample.get("label")}
        if len(names) > 1:
            reference["label"] = None
            reference["identity_conflict"] = True
        references.append(reference)
    return sorted(references, key=lambda row: str(row["id"]))


def _unit_vector(sample: Sample) -> np.ndarray:
    vector = np.asarray(sample["vector"], dtype=np.float64)
    if vector.ndim != 1 or not len(vector) or not np.isfinite(vector).all():
        raise ValueError("voice vectors must be finite one-dimensional arrays")
    norm = np.linalg.norm(vector)
    if not isfinite(norm) or norm == 0:
        raise ValueError("voice vectors must be nonzero")
    return vector / norm


def _centroid(samples: Sequence[Sample]) -> np.ndarray | None:
    vector = np.stack([_unit_vector(sample) for sample in samples]).mean(axis=0)
    norm = np.linalg.norm(vector)
    return vector / norm if norm > 1e-12 else None


def _ranked_result(query: Sample, candidates: list[dict[str, Any]], method: str) -> dict[str, Any]:
    candidates.sort(key=lambda candidate: (-candidate["similarity"], candidate["speaker"]))
    return {
        "method": method,
        "duration_ms": query["end_ms"] - query["start_ms"],
        "candidates": candidates,
        "gap": candidates[0]["similarity"] - candidates[1]["similarity"]
        if len(candidates) >= 2 else None,
    }


def score_baseline(
    query: Sample, samples: Sequence[Sample], *, trim_fraction: float = 0.2,
    excluded_reference_ids: Collection[str] = (),
) -> dict[str, Any]:
    """B0: leave-one-out, per-label normalized centroid after one trim step.

    Distinct PCM rows sharing a non-target cue remain separate centroid inputs;
    independent cue voting is a cluster-naming rule, not part of this baseline.
    """
    if not 0 <= trim_fraction < 1:
        raise ValueError("trim_fraction must be in [0, 1)")
    by_speaker: dict[str, list[Sample]] = defaultdict(list)
    for sample in eligible_references(query, samples, excluded_reference_ids=excluded_reference_ids):
        if sample.get("label"):
            by_speaker[sample["label"]].append(sample)
    target = _unit_vector(query)
    candidates = []
    for speaker, references in sorted(by_speaker.items()):
        center = _centroid(references)
        if center is None:
            continue
        ordered = sorted(references, key=lambda row: (
            -float(_unit_vector(row) @ center), str(row["id"]),
        ))
        kept = ordered[:len(ordered) - floor(trim_fraction * len(ordered))]
        center = _centroid(kept)
        if center is not None:
            candidates.append({
                "speaker": speaker,
                "similarity": float(np.clip(target @ center, -1, 1)),
                "reference_count": len(kept),
                "reference_consistency": float(np.clip(np.mean([
                    np.sum(_unit_vector(row) * center) for row in kept
                ]), -1, 1)),
                "reference_ids": sorted(row["id"] for row in kept),
                "independent_cue_count": len({cue for row in kept for cue in row.get("cue_ids", ())}),
            })
    return _ranked_result(query, candidates, "B0")


def score_voiceprint_line(
    query: Sample, samples: Sequence[Sample], *, model: str,
    encoder_revision: str, dimensions: int, trim_fraction: float,
    min_duration_ms: int, label_min_rank: int | None = None, label_min_margin: float | None = None,
    label_min_duration_ms: int | None = None, label_min_ref_count: int | None = None,
    label_min_consistency: float | None = None,
    excluded_reference_ids: Collection[str] = (),
) -> dict[str, Any]:
    """Apply the frozen production embedding contract to the offline B0 scorer.

    Invalid reference embeddings cannot contribute evidence. Invalid target
    embeddings are contract errors; a too-short target needs no embedding and
    instead retains an explicit, unscored result.
    """
    if not model or not encoder_revision:
        raise ValueError("a frozen model and encoder_revision are required")
    if isinstance(dimensions, bool) or not isinstance(dimensions, int) or dimensions < 1:
        raise ValueError("dimensions must be a positive integer")
    if (isinstance(min_duration_ms, bool) or not isinstance(min_duration_ms, int)
            or min_duration_ms < 1):
        raise ValueError("min_duration_ms must be a positive integer")
    if not 0 <= trim_fraction < 1:
        raise ValueError("trim_fraction must be in [0, 1)")
    duration_ms = query["end_ms"] - query["start_ms"]
    if duration_ms <= 0:
        raise ValueError("target audio interval must have a positive duration")
    result = {
        "method": "B0",
        "model": model, "encoder_revision": encoder_revision,
        "parameters": {"dimensions": dimensions, "trim_fraction": trim_fraction,
                       "min_duration_ms": min_duration_ms},
        "duration_ms": duration_ms, "candidates": [], "gap": None,
        "candidate_count": 0, "reason": "audio_too_short",
        "excluded_reference_counts": {},
        "label_score": None, "label_rank": None, "label_count": None,
        "label_margin": None, "label_ref_count": None, "label_ref_consistency": None,
    }
    label_policy = {
        "label_min_rank": label_min_rank, "label_min_margin": label_min_margin,
        "label_min_duration_ms": label_min_duration_ms, "label_min_ref_count": label_min_ref_count,
        "label_min_consistency": label_min_consistency,
    }
    supplied = [value is not None for value in label_policy.values()]
    if any(supplied) and not all(supplied):
        raise ValueError("label policy must provide all five thresholds or none")
    result["label_doubtful"] = is_label_doubtful(result, **label_policy) if all(supplied) else False
    if duration_ms < min_duration_ms:
        return result
    issue = _embedding_issue(query, model=model, encoder_revision=encoder_revision,
                             dimensions=dimensions)
    if issue is not None:
        raise ValueError(f"target voice embedding violates frozen contract: {issue}")
    references = []
    excluded: Counter[str] = Counter()
    for sample in samples:
        if not sample.get("label"):
            excluded["unnamed_reference"] += 1
            continue
        if sample["end_ms"] - sample["start_ms"] < min_duration_ms:
            excluded["audio_too_short"] += 1
            continue
        issue = _embedding_issue(sample, model=model, encoder_revision=encoder_revision,
                                 dimensions=dimensions)
        if issue is not None:
            excluded[issue] += 1
            continue
        references.append(sample)
    scored = score_baseline(query, references, trim_fraction=trim_fraction,
                            excluded_reference_ids=excluded_reference_ids)
    candidates = scored["candidates"][:3]
    if query.get("label") is not None and candidates:
        for rank, candidate in enumerate(scored["candidates"], 1):
            if candidate["speaker"] == query["label"]:
                result.update(
                    label_score=candidate["similarity"], label_rank=rank,
                    label_count=len(scored["candidates"]),
                    label_margin=scored["candidates"][0]["similarity"] - candidate["similarity"],
                    label_ref_count=candidate["reference_count"],
                    label_ref_consistency=candidate["reference_consistency"],
                )
                break
        if all(supplied):
            result["label_doubtful"] = is_label_doubtful(result, **label_policy)
    return {
        **result, "candidates": candidates, "gap": scored["gap"],
        "candidate_count": len(scored["candidates"]),
        "reason": "compared" if candidates else "no_eligible_reference",
        "excluded_reference_counts": dict(sorted(excluded.items())),
    }


def is_label_doubtful(
    result: Mapping[str, Any], *, label_min_rank: int, label_min_margin: float,
    label_min_duration_ms: int, label_min_ref_count: int, label_min_consistency: float,
) -> bool:
    """Flag a label contradicted by a sufficiently coherent reference pool.

    These injected cosine criteria do not change candidates, bands or defaults.
    Reference count means kept PCM rows, not independent ASR cues.
    """
    for name, value in (("label_min_rank", label_min_rank),
                        ("label_min_duration_ms", label_min_duration_ms),
                        ("label_min_ref_count", label_min_ref_count)):
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"{name} must be a positive integer")
    if not isfinite(label_min_margin) or not 0 <= label_min_margin <= 2:
        raise ValueError("label_min_margin must be in [0, 2]")
    if not isfinite(label_min_consistency) or not 0 <= label_min_consistency <= 1:
        raise ValueError("label_min_consistency must be in [0, 1]")
    return all(result.get(key) is not None and result[key] >= minimum for key, minimum in (
        ("label_rank", label_min_rank), ("label_margin", label_min_margin),
        ("duration_ms", label_min_duration_ms), ("label_ref_count", label_min_ref_count),
        ("label_ref_consistency", label_min_consistency),
    ))


def score_voiceprint_batch(
    samples: Sequence[Sample], *, model: str, encoder_revision: str, dimensions: int,
    trim_fraction: float, min_duration_ms: int, label_min_rank: int, label_min_margin: float,
    label_min_duration_ms: int, label_min_ref_count: int, label_min_consistency: float,
) -> dict[str, Any]:
    """Score exactly twice; scores preserve input order and samples stay frozen.

    Pass-one doubtful IDs are excluded from reference vectors in pass two, even
    if their final flags clear. Original rows remain available for conflicting
    PCM-name abstention. No recursive convergence or relabeling is performed.
    """
    label_policy = {
        "label_min_rank": label_min_rank, "label_min_margin": label_min_margin,
        "label_min_duration_ms": label_min_duration_ms, "label_min_ref_count": label_min_ref_count,
        "label_min_consistency": label_min_consistency,
    }
    is_label_doubtful({}, **label_policy)
    if len({sample["id"] for sample in samples}) != len(samples):
        raise ValueError("voiceprint batch sample IDs must be unique")
    settings = {"model": model, "encoder_revision": encoder_revision, "dimensions": dimensions,
                "trim_fraction": trim_fraction, "min_duration_ms": min_duration_ms, **label_policy}
    first = [score_voiceprint_line(sample, samples, **settings) for sample in samples]
    excluded = {sample["id"] for sample, result in zip(samples, first) if result["label_doubtful"]}
    final = [score_voiceprint_line(sample, samples, excluded_reference_ids=excluded, **settings)
             for sample in samples]
    flagged = {sample["id"] for sample, result in zip(samples, final) if result["label_doubtful"]}
    return {"scores": final, "hygiene": {
        "passes": 2, "pass1_flag_count": len(excluded), "pass2_flag_count": len(flagged),
        "changed_flag_count": len(excluded ^ flagged),
        "newly_flagged_ids": sorted(flagged - excluded), "cleared_flag_ids": sorted(excluded - flagged),
        "excluded_reference_ids": sorted(excluded),
    }}


def _embedding_issue(
    sample: Sample, *, model: str, encoder_revision: str, dimensions: int,
) -> str | None:
    if (sample.get("model"), sample.get("encoder_revision")) != (model, encoder_revision):
        return "incompatible_encoder"
    try:
        vector = np.asarray(sample.get("vector"), dtype=np.float64)
    except (TypeError, ValueError):
        return "invalid_embedding"
    if vector.ndim != 1 or len(vector) != dimensions or not np.isfinite(vector).all():
        return "invalid_embedding"
    if not np.isclose(np.linalg.norm(vector), 1.0, rtol=0, atol=1e-5):
        return "non_normalized_embedding"
    return None


def classify_voiceprint_result(
    result: Mapping[str, Any], *, current_label: str | None, song: bool,
    overlap: bool, accept_gap: float, accept_duration_ms: int,
    accept_min_cues: int, conflict_gap: float, no_voice_duration_ms: int,
) -> dict[str, Any]:
    """Classify voice evidence without applying any speaker change.

    ACCEPT disagreements remain eligible defaults and also set conflict=True.
    A high-gap disagreement that fails an ACCEPT condition remains a conflict
    for Refine review; it is never eligible for automatic prefill.
    """
    if (not isfinite(accept_gap) or not isfinite(conflict_gap)
            or not 0 <= conflict_gap <= accept_gap <= 2):
        raise ValueError("voice gap thresholds must satisfy 0 <= conflict <= accept <= 2")
    for name, value in (("accept_duration_ms", accept_duration_ms),
                        ("accept_min_cues", accept_min_cues),
                        ("no_voice_duration_ms", no_voice_duration_ms)):
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"{name} must be a positive integer")
    candidates = result.get("candidates") or []
    duration_ms = result["duration_ms"]
    gap = result.get("gap")
    decision = {"band": "NO_VOICE", "conflict": False, "review_reason": None}
    if duration_ms < no_voice_duration_ms:
        return {**decision, "review_reason":
                (result.get("reason") if not candidates else None)
                or "duration_below_no_voice_minimum"}
    if not candidates:
        return {**decision, "review_reason": result.get("reason") or "no_candidates"}
    if gap is None:
        return {**decision, "review_reason": "insufficient_candidates"}
    if not isfinite(gap) or not 0 <= gap <= 2:
        raise ValueError("candidate gap must be a finite cosine difference in [0, 2]")
    if gap < conflict_gap:
        return {**decision, "review_reason": "gap_below_no_voice_minimum"}
    top1 = candidates[0]
    disagreement = top1["speaker"] != current_label
    if gap < accept_gap:
        return {"band": "CONFLICT" if disagreement else None,
                "conflict": disagreement,
                "review_reason": "voice_label_disagreement" if disagreement
                else "agreement_below_accept_gap"}
    reason = (
        "song" if song else
        "overlapping_dialogue" if overlap else
        "duration_below_accept_minimum" if duration_ms < accept_duration_ms else
        "insufficient_independent_reference_cues"
        if top1.get("independent_cue_count", 0) < accept_min_cues else None
    )
    if reason is not None:
        return {"band": "CONFLICT" if disagreement else None,
                "conflict": disagreement, "review_reason": reason}
    return {"band": "ACCEPT", "conflict": disagreement,
            "review_reason": "voice_label_disagreement" if disagreement else None}


def cluster_samples(
    samples: Sequence[Sample], *, method: str, k: int | None = None,
    threshold: float = 0.35, seed: int = 20261018, starts: int = 10,
    max_iter: int = 100,
) -> dict[str, Any]:
    """Fit unit-vector K-means or average-linkage cosine clustering.

    The caller supplies clean, query-excluded rows. Cluster IDs follow the first
    sample ID, so arbitrary library label numbers do not affect output or ties.
    """
    from scipy.cluster.hierarchy import fcluster, linkage
    from scipy.cluster.vq import kmeans2
    from scipy.spatial.distance import pdist

    if method not in {"kmeans", "agglomerative"}:
        raise ValueError("method must be kmeans or agglomerative")
    if method == "kmeans" and (isinstance(k, bool) or not isinstance(k, int) or k < 1):
        raise ValueError("k must be a positive integer")
    if (isinstance(starts, bool) or not isinstance(starts, int) or starts < 1
            or isinstance(max_iter, bool) or not isinstance(max_iter, int) or max_iter < 1):
        raise ValueError("starts and max_iter must be positive integers")
    if not isfinite(threshold) or not 0 <= threshold <= 2:
        raise ValueError("cosine distance threshold must be in [0, 2]")
    ordered = sorted(samples, key=lambda sample: str(sample["id"]))
    if len({sample["id"] for sample in ordered}) != len(ordered):
        raise ValueError("sample IDs must be unique")
    result: dict[str, Any] = {
        "method": method, "requested_k": k if method == "kmeans" else None,
        "threshold": threshold if method == "agglomerative" else None,
        "groups": [], "k": 0,
    }
    if not ordered:
        return result
    values = np.stack([_unit_vector(sample) for sample in ordered])
    if len(ordered) == 1:
        labels = np.zeros(1, dtype=int)
    elif method == "agglomerative":
        distances = np.clip(pdist(values, metric="cosine"), 0, 2)
        labels = fcluster(linkage(distances, method="average"), threshold, criterion="distance")
    else:
        effective_k = min(k, len(np.unique(values, axis=0)))
        best = None
        for start in range(starts):
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                centers, _ = kmeans2(
                    values, effective_k, iter=max_iter, minit="++", missing="warn",
                    rng=np.random.default_rng(seed + start),
                )
            distances = ((values[:, None, :] - centers[None, :, :]) ** 2).sum(axis=2)
            labels = distances.argmin(axis=1)
            sse = float(distances[np.arange(len(values)), labels].sum())
            if not isfinite(sse):
                raise ValueError("K-means produced nonfinite centers")
            canonical = sorted(
                (tuple(np.flatnonzero(labels == label)) for label in set(labels)),
                key=lambda indices: indices[0],
            )
            key = (sse, canonical)
            if best is None or key < best[0]:
                best = (key, labels.copy(), seed + start)
        labels = best[1]
        result.update(sse=best[0][0], seed=best[2], starts=starts, max_iter=max_iter)
    members = sorted(
        ([sample["id"] for index, sample in enumerate(ordered) if labels[index] == label]
         for label in set(labels)),
        key=lambda ids: str(ids[0]),
    )
    result["groups"] = [
        {"cluster_id": index, "sample_ids": ids} for index, ids in enumerate(members)
    ]
    result["k"] = len(members)
    return result


def score_clusters(
    query: Sample, samples: Sequence[Sample], groups: Sequence[Mapping[str, Any]], *,
    aggregation: str = "max", min_cues: int = 3, min_purity: float = 0.6,
) -> dict[str, Any]:
    """Name groups by independent cues, then rank distinct characters.

    A conflicting cue abstains, and null/abstaining cues remain in the purity
    denominator. Target exclusion precedes both identity voting and centroids.
    """
    if aggregation not in {"max", "pooled"}:
        raise ValueError("aggregation must be max or pooled")
    if isinstance(min_cues, bool) or not isinstance(min_cues, int) or min_cues < 1:
        raise ValueError("min_cues must be a positive integer")
    if not 0 <= min_purity <= 1:
        raise ValueError("min_purity must be in [0, 1]")
    references = {sample["id"]: sample for sample in eligible_references(query, samples)}
    cue_labels: dict[str, set[str | None]] = defaultdict(set)
    for sample in references.values():
        for cue in sample.get("cue_ids", ()):
            cue_labels[cue].add(sample.get("label") or None)
    target = _unit_vector(query)
    by_speaker: dict[str, list[tuple[Any, list[Sample], np.ndarray]]] = defaultdict(list)
    diagnostics = []
    seen_ids = set()
    for group in groups:
        member_ids = set(group["sample_ids"])
        if seen_ids.intersection(member_ids):
            raise ValueError("a sample cannot belong to multiple clusters")
        seen_ids.update(member_ids)
        members = [references[sample_id] for sample_id in sorted(member_ids) if sample_id in references]
        cues = {cue for sample in members for cue in sample.get("cue_ids", ())}
        votes = Counter(next(iter(cue_labels[cue])) for cue in cues
                        if len(cue_labels[cue]) == 1 and None not in cue_labels[cue])
        support = max(votes.values(), default=0)
        winners = sorted(name for name, count in votes.items() if count == support)
        purity = support / len(cues) if cues else 0.0
        speaker = winners[0] if len(winners) == 1 and support >= min_cues and purity >= min_purity else None
        diagnostics.append({
            "cluster_id": group["cluster_id"], "speaker": speaker,
            "sample_ids": [sample["id"] for sample in members],
            "cue_count": len(cues), "label_support": dict(sorted(votes.items())),
            "support_count": support, "purity": purity,
        })
        center = _centroid(members) if members else None
        if speaker is not None and center is not None:
            by_speaker[speaker].append((group["cluster_id"], members, center))
    candidates = []
    for speaker, named_groups in sorted(by_speaker.items()):
        if aggregation == "max":
            cluster_id, members, center = min(named_groups, key=lambda group: (
                -float(target @ group[2]), str(group[0]),
            ))
            cluster_ids = [cluster_id]
        else:
            members = [sample for _, rows, _ in named_groups for sample in rows]
            center = _centroid(members)
            cluster_ids = [cluster_id for cluster_id, _, _ in named_groups]
        if center is not None:
            candidates.append({
                "speaker": speaker,
                "similarity": float(np.clip(target @ center, -1, 1)),
                "reference_count": len(members),
                "reference_ids": sorted(sample["id"] for sample in members),
                "independent_cue_count": len({cue for sample in members for cue in sample.get("cue_ids", ())}),
                "cluster_ids": cluster_ids,
            })
    result = _ranked_result(query, candidates, f"cluster_{aggregation}")
    result["clusters"] = diagnostics
    return result
