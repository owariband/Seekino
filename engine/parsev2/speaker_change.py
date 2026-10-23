"""Temporal acoustic-change candidates; scores are not speaker probabilities.

The Gaussian checkerboard curve follows Foote (2000). Masked median contrast
and the low-support edge branch are project hypotheses, not the paper's formula.
This module consumes frozen embeddings and has no model or storage dependency.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from math import hypot, isfinite

import numpy as np


def detect_speaker_changes(
    windows: Sequence[Mapping], vectors: Sequence, *, start_ms: int, end_ms: int,
    side_windows: int, min_pairs: int, contrast_threshold: float, edge_threshold: float,
    window_mask: Sequence[bool] | None = None,
) -> dict:
    """Return all scores and review ranges, without asserting purity or identity.

    Bounds are absolute half-open milliseconds. The caller freezes the window
    grid and encoder. Complete windows on either side support a scan; straddling
    windows do not. An optional speech mask excludes pairs without compressing
    the timeline or enlarging either neighborhood. Missing evidence stays None.
    """
    for name, value in (("start_ms", start_ms), ("end_ms", end_ms),
                        ("side_windows", side_windows), ("min_pairs", min_pairs)):
        if type(value) is not int:
            raise ValueError(f"{name} must be an integer")
    if not 0 <= start_ms < end_ms or side_windows < 2 or min_pairs < 1:
        raise ValueError("invalid bounds or support parameters")
    if any(isinstance(value, bool) or not isfinite(value) or not 0 <= value <= 2
           for value in (contrast_threshold, edge_threshold)):
        raise ValueError("contrast thresholds must be finite and in [0, 2]")
    previous = start_ms - 1
    mask = [True] * len(windows) if window_mask is None else list(window_mask)
    if len(mask) != len(windows) or any(type(value) is not bool for value in mask):
        raise ValueError("window_mask must contain one bool per window")
    for window in windows:
        left, right = window["start_ms"], window["end_ms"]
        if (type(left) is not int or type(right) is not int
                or not start_ms <= left < right <= end_ms or left <= previous):
            raise ValueError("windows must be ordered, distinct and within the audio bounds")
        if not isinstance(window.get("pcm_sha256"), str) or not window["pcm_sha256"]:
            raise ValueError("each window requires its PCM identity")
        previous = left
    values = np.asarray(vectors, dtype=np.float64)
    if len(windows) == 0 and values.size == 0:
        values = np.empty((0, 0))
    if (values.ndim != 2 or values.shape[0] != len(windows)
            or (len(windows) and values.shape[1] == 0) or not np.isfinite(values).all()):
        raise ValueError("embeddings must be a finite rectangular array matching the windows")
    norms = np.array([hypot(*row) for row in values])
    if np.any(norms == 0) or not np.isfinite(norms).all():
        raise ValueError("embeddings must have a finite, nonzero norm")
    unit = values / norms[:, None] if len(windows) else values
    matrix = np.clip(np.einsum("ik,jk->ij", unit, unit), -1, 1)
    # Only truly disjoint, distinct PCM pairs support the masked summaries.
    valid = np.zeros(matrix.shape, dtype=bool)
    for i, first in enumerate(windows):
        for j in range(i + 1, len(windows)):
            second = windows[j]
            valid[i, j] = valid[j, i] = (
                mask[i] and mask[j]
                and first["end_ms"] <= second["start_ms"]
                and first["pcm_sha256"] != second["pcm_sha256"]
            )

    def summarize(left, right, *, internal=False):
        pairs = [(i, j) for i in left for j in right if valid[i, j] and (not internal or i < j)]
        score = float(np.median([matrix[i, j] for i, j in pairs])) if pairs else None
        return score, len(pairs)

    scans = []
    for cut in [window["start_ms"] for window in windows[1:]]:
        left = [i for i, window in enumerate(windows) if window["end_ms"] <= cut][-side_windows:]
        right = [i for i, window in enumerate(windows) if window["start_ms"] >= cut][:side_windows]
        within_left, left_pairs = summarize(left, left, internal=True)
        within_right, right_pairs = summarize(right, right, internal=True)
        cross, cross_pairs = summarize(left, right)
        contrast = (min(within_left, within_right) - cross
                    if all(score is not None for score in (within_left, within_right, cross)) else None)
        edge_contrast = None
        kind, reason = None, "insufficient_support"
        if min(left_pairs, right_pairs, cross_pairs) >= min_pairs:
            reason = "below_contrast_threshold"
            if contrast >= contrast_threshold:
                kind, reason = "block_change", "both_sides_have_disjoint_pairs"
        elif cross_pairs >= min_pairs and (left or right):
            # A short side cannot prove internal coherence, but must not vanish.
            if left_pairs < min_pairs <= right_pairs:
                edge_contrast = within_right - cross
            elif right_pairs < min_pairs <= left_pairs:
                edge_contrast = within_left - cross
            if edge_contrast is not None:
                reason = "low_support_below_threshold"
                if edge_contrast >= edge_threshold:
                    kind, reason = "low_support", "one_side_lacks_internal_support"
        scans.append({
            "time_ms": cut, "left_indices": left, "right_indices": right,
            "left_count": len(left), "right_count": len(right),
            "left_pairs": left_pairs, "right_pairs": right_pairs, "cross_pairs": cross_pairs,
            "left_similarity": within_left, "right_similarity": within_right,
            "cross_similarity": cross, "contrast": contrast, "edge_contrast": edge_contrast,
            "kind": kind, "reason": reason,
        })

    candidates, run = [], []

    def finish_run():
        if not run:
            return
        supported = [row for row in run if row["kind"] == "block_change"]
        # Persistence is a temporal check, not a count of independent votes.
        kind = "block_change" if len(supported) >= 2 else "low_support"
        peak = max(supported if kind == "block_change" else run, key=lambda row: (
            row["contrast"] if row["kind"] == "block_change" else row["edge_contrast"],
            -row["time_ms"],
        ))
        half_window = round(float(np.median([w["end_ms"] - w["start_ms"] for w in windows])) / 2)
        candidates.append({
            "start_ms": max(start_ms, run[0]["time_ms"] - half_window),
            "end_ms": min(end_ms, run[-1]["time_ms"] + half_window),
            "scan_start_ms": run[0]["time_ms"], "scan_end_ms": run[-1]["time_ms"],
            "peak_ms": peak["time_ms"], "kind": kind, "scan_count": len(run),
            "score": peak["contrast"] if peak["kind"] == "block_change" else peak["edge_contrast"],
            "reason": ("persistent_block_contrast" if kind == "block_change"
                       else "limited_or_isolated_support"),
        })
        run.clear()

    for row in scans:
        if row["kind"] is None:
            finish_run()
        else:
            run.append(row)
    finish_run()

    # Literal Gaussian checkerboard correlation on the full similarity matrix.
    # Unlike the masked branch, this baseline includes diagonals/shared audio.
    # L1 kernel normalization fixes scale; per-clip min/max would force every
    # nonconstant clip to reach 1, making unrelated clips hard to compare.
    positions = np.arange(-side_windows, side_windows, dtype=float) + 0.5
    sigma = side_windows / 2
    signed = np.sign(positions) * np.exp(-positions ** 2 / (2 * sigma ** 2))
    kernel = np.outer(signed, signed)
    kernel /= np.abs(kernel).sum()
    foote = []
    for cut in range(side_windows, len(windows) - side_windows + 1):
        centers = [sum((windows[i]["start_ms"], windows[i]["end_ms"])) / 2
                   for i in (cut - 1, cut)]
        block = matrix[cut - side_windows:cut + side_windows, cut - side_windows:cut + side_windows]
        foote.append({"time_ms": round(sum(centers) / 2),
                      "novelty": float(np.sum(kernel * block))})
    uncovered, cursor = [], start_ms
    for window in windows:
        if window["start_ms"] > cursor:
            uncovered.append({"start_ms": cursor, "end_ms": window["start_ms"]})
        cursor = max(cursor, window["end_ms"])
    if cursor < end_ms:
        uncovered.append({"start_ms": cursor, "end_ms": end_ms})
    return {
        "parameters": {"side_windows": side_windows, "min_pairs": min_pairs,
                       "contrast_threshold": contrast_threshold, "edge_threshold": edge_threshold,
                       "foote_sigma_windows": sigma, "foote_normalization": "kernel_l1",
                       "minimum_block_scan_count": 2},
        "matrix": matrix.tolist(), "scans": scans, "candidates": candidates,
        "foote": foote, "uncovered": uncovered,
        "ignored_window_indices": [i for i, keep in enumerate(mask) if not keep],
    }
