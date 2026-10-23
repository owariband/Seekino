"""Synthetic vector tests establish score semantics, not real acoustic accuracy."""

from __future__ import annotations

from copy import deepcopy
from math import hypot, sqrt

import pytest

from engine.parsev2.speaker_change import detect_speaker_changes


SETTINGS = {
    "side_windows": 6, "min_pairs": 3,
    "contrast_threshold": 0.2, "edge_threshold": 0.2,
}


def timeline(spans, duration_ms, *, offset_ms=0, window_ms=400, hop_ms=100, dimensions=2):
    """Mix unit axes by a window's time coverage; this is not an encoder model."""
    windows, vectors = [], []
    for start in range(0, duration_ms - window_ms + 1, hop_ms):
        windows.append({"start_ms": offset_ms + start, "end_ms": offset_ms + start + window_ms,
                        "pcm_sha256": f"distinct-pcm-{start}"})
        vector = [0.0] * dimensions
        for left, right, axis in spans:
            vector[axis] += max(0, min(start + window_ms, right) - max(start, left))
        norm = hypot(*vector)
        vectors.append([value / norm for value in vector])
    return windows, vectors


def detect(windows, vectors, *, start_ms=0, end_ms, **changes):
    return detect_speaker_changes(windows, vectors, start_ms=start_ms, end_ms=end_ms,
                                  **{**SETTINGS, **changes})


def test_identical_vectors_from_distinct_pcm_have_no_change_or_novelty() -> None:
    windows, vectors = timeline([(0, 4000, 0)], 4000)
    result = detect(windows, vectors, end_ms=4000)
    assert len({window["pcm_sha256"] for window in windows}) == len(windows)
    assert result["candidates"] == []
    assert all(row["kind"] is None for row in result["scans"])
    assert any(row["left_pairs"] >= 3 and row["right_pairs"] >= 3 for row in result["scans"])
    assert all(row["contrast"] in (None, 0) for row in result["scans"])
    assert result["foote"]
    assert all(row["novelty"] == pytest.approx(0, abs=1e-12) for row in result["foote"])


@pytest.mark.parametrize("spans,duration,boundaries,window_ms,hop_ms", [
    ([(0, 1000, 0), (1000, 2000, 1)], 2000, [1000], 400, 100),
    ([(0, 1000, 0), (1000, 2000, 1), (2000, 3000, 0)], 3000, [1000, 2000], 400, 100),
    ([(0, 4000, 0), (4000, 8000, 1)], 8000, [4000], 1000, 400),
    ([(0, 4000, 0), (4000, 8000, 1), (8000, 12000, 0)], 12000, [4000, 8000], 1000, 400),
])
def test_mixed_windows_recover_sequential_block_changes(spans, duration, boundaries, window_ms, hop_ms) -> None:
    windows, vectors = timeline(spans, duration, window_ms=window_ms, hop_ms=hop_ms)
    assert any(vector[0] > 0 and vector[1] > 0 for vector in vectors)
    result = detect(windows, vectors, end_ms=duration)
    candidates = result["candidates"]
    assert len(candidates) == len(boundaries)
    for candidate, boundary in zip(candidates, boundaries, strict=True):
        assert candidate["kind"] == "block_change"
        assert candidate["start_ms"] <= boundary <= candidate["end_ms"]
        assert abs(candidate["peak_ms"] - boundary) <= window_ms
        assert candidate["start_ms"] <= candidate["peak_ms"] <= candidate["end_ms"]
        assert candidate["scan_count"] >= 2
    assert all(left["end_ms"] <= right["start_ms"] for left, right in zip(candidates, candidates[1:]))


def test_single_outlier_does_not_become_a_persistent_block_change() -> None:
    windows, vectors = timeline([(0, 4000, 0)], 4000)
    vectors[len(vectors) // 2] = [0.0, 1.0]
    result = detect(windows, vectors, end_ms=4000, side_windows=12)
    assert not any(candidate["kind"] == "block_change" for candidate in result["candidates"])


def test_short_changed_tail_is_retained_with_explicit_low_support() -> None:
    windows, vectors = timeline([(0, 1600, 0), (1600, 2200, 1)], 2200)
    result = detect(windows, vectors, end_ms=2200)
    assert len(result["candidates"]) == 1
    candidate = result["candidates"][0]
    assert candidate["kind"] == "low_support"
    assert candidate["start_ms"] <= 1600 <= candidate["end_ms"]
    supporting = [row for row in result["scans"] if row["kind"] == "low_support"]
    assert supporting
    assert any(row["right_pairs"] < SETTINGS["min_pairs"] <= row["left_pairs"]
               and row["cross_pairs"] >= SETTINGS["min_pairs"] for row in supporting)


def test_one_second_windows_retain_short_tail_with_low_support_and_uncovered_remainder() -> None:
    windows, vectors = timeline([(0, 8000, 0), (8000, 9300, 1)], 9300,
                                window_ms=1000, hop_ms=400)
    result = detect(windows, vectors, end_ms=9300)
    assert len(result["candidates"]) == 1
    candidate = result["candidates"][0]
    assert candidate["kind"] == "low_support"
    assert candidate["start_ms"] <= 8000 <= candidate["end_ms"]
    assert any(row["kind"] == "low_support" and row["right_pairs"] < SETTINGS["min_pairs"]
               and row["left_pairs"] >= SETTINGS["min_pairs"]
               and row["cross_pairs"] >= SETTINGS["min_pairs"] for row in result["scans"])
    assert result["uncovered"] == [{"start_ms": 9000, "end_ms": 9300}]


def test_one_isolated_supported_scan_is_not_reported_as_persistent() -> None:
    windows, vectors = timeline([(0, 1600, 0), (1600, 3200, 1)], 3200, hop_ms=400)
    result = detect(windows, vectors, end_ms=3200, side_windows=2, min_pairs=1)
    assert len(result["candidates"]) == 1
    assert result["candidates"][0]["scan_count"] == 1
    assert result["candidates"][0]["kind"] == "low_support"


def test_supported_candidate_peak_cannot_come_from_a_higher_low_support_score() -> None:
    windows = [{"start_ms": index * 400, "end_ms": index * 400 + 1000,
                "pcm_sha256": f"distinct-pcm-{index}"} for index in range(17)]
    vectors = [[1.0, *([0.0] * 6)] for _ in range(12)]
    for index in range(5):
        vector = [0.0] * 7
        vector[1] = sqrt(0.6)
        vector[index + 2] = sqrt(0.4)
        vectors.append(vector)
    # Different B vectors share cosine 0.6; every A-B pair has cosine zero.
    result = detect(windows, vectors, end_ms=7400)
    scans = {row["time_ms"]: row for row in result["scans"]}
    assert scans[4400]["kind"] == scans[4800]["kind"] == "block_change"
    assert scans[4400]["contrast"] == pytest.approx(0.3)
    assert scans[4800]["contrast"] == pytest.approx(0.6)
    assert scans[5200]["kind"] == "low_support"
    assert scans[5200]["right_pairs"] == 1
    assert scans[5200]["edge_contrast"] == pytest.approx(1.0)
    assert len(result["candidates"]) == 1
    candidate = result["candidates"][0]
    assert candidate["kind"] == "block_change"
    assert candidate["peak_ms"] == 4800
    assert candidate["score"] == pytest.approx(0.6)


def test_nonzero_time_offset_moves_all_evidence_without_changing_scores() -> None:
    spans = [(0, 1000, 0), (1000, 2000, 1), (2000, 3000, 0)]
    windows, vectors = timeline(spans, 3000)
    offset = 45_200
    shifted, shifted_vectors = timeline(spans, 3000, offset_ms=offset)
    baseline = detect(windows, vectors, end_ms=3150)
    result = detect(shifted, shifted_vectors, start_ms=offset, end_ms=offset + 3150)
    assert result["matrix"] == baseline["matrix"]
    for collection, fields in (("scans", ["time_ms"]), ("foote", ["time_ms"]),
                               ("candidates", ["start_ms", "end_ms", "peak_ms", "scan_start_ms", "scan_end_ms"]),
                               ("uncovered", ["start_ms", "end_ms"])):
        assert len(result[collection]) == len(baseline[collection])
        for actual, original in zip(result[collection], baseline[collection], strict=True):
            normalized = dict(actual)
            for field in fields:
                normalized[field] -= offset
            assert normalized == original


def test_pair_counts_exclude_self_but_include_touching_distinct_pcm() -> None:
    windows, vectors = timeline([(0, 1600, 0)], 1600, hop_ms=400)
    result = detect(windows, vectors, end_ms=1600, side_windows=2, min_pairs=1)
    middle = next(row for row in result["scans"] if row["time_ms"] == 800)
    assert (middle["left_count"], middle["right_count"]) == (2, 2)
    assert (middle["left_pairs"], middle["right_pairs"], middle["cross_pairs"]) == (1, 1, 4)
    assert middle["left_similarity"] == middle["right_similarity"] == middle["cross_similarity"] == 1


def test_straddling_windows_and_time_overlap_do_not_supply_independent_support() -> None:
    windows, vectors = timeline([(0, 1200, 0)], 1200, hop_ms=100)
    result = detect(windows, vectors, end_ms=1200, side_windows=2, min_pairs=1)
    cut = next(row for row in result["scans"] if row["time_ms"] == 400)
    assert cut["left_indices"] == [0]
    assert cut["right_indices"] == [4, 5]
    assert (cut["left_pairs"], cut["right_pairs"], cut["cross_pairs"]) == (0, 0, 2)
    assert cut["left_similarity"] is cut["right_similarity"] is None
    assert cut["contrast"] is None
    assert all(windows[index]["end_ms"] <= cut["time_ms"] for index in cut["left_indices"])
    assert all(windows[index]["start_ms"] >= cut["time_ms"] for index in cut["right_indices"])


def test_identical_pcm_is_excluded_from_internal_and_cross_pairs() -> None:
    windows, vectors = timeline([(0, 1600, 0)], 1600, hop_ms=400)
    windows[1]["pcm_sha256"] = windows[0]["pcm_sha256"]
    windows[3]["pcm_sha256"] = windows[0]["pcm_sha256"]
    result = detect(windows, vectors, end_ms=1600, side_windows=2, min_pairs=1)
    middle = next(row for row in result["scans"] if row["time_ms"] == 800)
    assert (middle["left_pairs"], middle["right_pairs"], middle["cross_pairs"]) == (0, 1, 2)
    assert middle["left_similarity"] is None


def test_foote_baseline_uses_full_matrix_and_l1_scale_without_pcm_masking() -> None:
    windows, vectors = timeline([(0, 800, 0), (800, 1600, 1)], 1600, hop_ms=400)
    for window in windows:
        window["pcm_sha256"] = "same-pcm"
    result = detect(windows, vectors, end_ms=1600, side_windows=2, min_pairs=1)
    assert result["candidates"] == []
    assert all(row["left_pairs"] == row["right_pairs"] == row["cross_pairs"] == 0 for row in result["scans"])
    assert result["matrix"] == [[1, 1, 0, 0], [1, 1, 0, 0], [0, 0, 1, 1], [0, 0, 1, 1]]
    assert len(result["foote"]) == 1
    assert result["foote"][0]["time_ms"] == 800
    assert result["foote"][0]["novelty"] == pytest.approx(0.5)


def test_uncovered_head_gaps_and_tail_are_reported_in_absolute_milliseconds() -> None:
    windows = [{"start_ms": 200, "end_ms": 600, "pcm_sha256": "a"},
               {"start_ms": 1000, "end_ms": 1400, "pcm_sha256": "b"}]
    result = detect(windows, [[1, 0], [1, 0]], start_ms=100, end_ms=1550)
    assert result["uncovered"] == [{"start_ms": 100, "end_ms": 200},
                                    {"start_ms": 600, "end_ms": 1000},
                                    {"start_ms": 1400, "end_ms": 1550}]


def test_empty_input_retains_the_entire_uncovered_range() -> None:
    result = detect([], [], start_ms=400, end_ms=900)
    assert result["matrix"] == result["scans"] == result["candidates"] == result["foote"] == []
    assert result["uncovered"] == [{"start_ms": 400, "end_ms": 900}]


def test_inputs_are_unchanged_and_repeated_calls_are_deterministic() -> None:
    windows, vectors = timeline([(0, 1000, 0), (1000, 2000, 1)], 2000)
    before = deepcopy((windows, vectors))
    first = detect(windows, vectors, end_ms=2000)
    assert (windows, vectors) == before
    assert detect(windows, vectors, end_ms=2000) == first
    assert (windows, vectors) == before


@pytest.mark.parametrize("changes", [
    {"start_ms": -1}, {"end_ms": 0}, {"start_ms": True}, {"end_ms": 1.5},
    {"side_windows": 1}, {"side_windows": True}, {"min_pairs": 0}, {"min_pairs": 1.5},
    {"contrast_threshold": -0.1}, {"contrast_threshold": float("nan")},
    {"contrast_threshold": float("inf")}, {"contrast_threshold": True},
    {"edge_threshold": 2.1}, {"edge_threshold": float("nan")},
])
def test_invalid_bounds_support_or_thresholds_fail(changes) -> None:
    windows, vectors = timeline([(0, 1000, 0)], 1000)
    with pytest.raises(ValueError):
        detect(windows, vectors, **{"end_ms": 1000, **changes})


@pytest.mark.parametrize("vectors", [
    [], [[1, 0]], [[1], [1, 0]], [[], []], [[0, 0], [1, 0]],
    [[float("nan"), 0], [1, 0]], [[float("inf"), 0], [1, 0]],
    [[[1, 0]], [[1, 0]]],
])
def test_invalid_vector_shape_values_or_norm_fail(vectors) -> None:
    windows, _ = timeline([(0, 800, 0)], 800, hop_ms=400)
    with pytest.raises(ValueError):
        detect(windows, vectors, end_ms=800)


@pytest.mark.parametrize("change", [
    {"start_ms": -1}, {"start_ms": True}, {"start_ms": 0.5},
    {"end_ms": 0}, {"end_ms": 801}, {"pcm_sha256": ""}, {"pcm_sha256": None},
])
def test_invalid_window_bounds_or_identity_fail(change) -> None:
    windows, vectors = timeline([(0, 800, 0)], 800, hop_ms=400)
    windows[0].update(change)
    with pytest.raises(ValueError):
        detect(windows, vectors, end_ms=800)


@pytest.mark.parametrize("duplicate", [False, True])
def test_reversed_or_duplicate_window_starts_fail(duplicate) -> None:
    windows, vectors = timeline([(0, 800, 0)], 800, hop_ms=400)
    if duplicate:
        windows[1]["start_ms"] = windows[0]["start_ms"]
    else:
        windows.reverse()
        vectors.reverse()
    with pytest.raises(ValueError):
        detect(windows, vectors, end_ms=800)


def test_all_true_window_mask_is_identical_to_the_default() -> None:
    windows, vectors = timeline([(0, 4000, 0), (4000, 8000, 1)], 8000,
                                window_ms=1000, hop_ms=400)
    baseline = detect(windows, vectors, end_ms=8000)
    assert baseline["candidates"]
    assert baseline["ignored_window_indices"] == []
    assert detect(windows, vectors, end_ms=8000, window_mask=None) == baseline
    assert detect(windows, vectors, end_ms=8000, window_mask=[True] * len(windows)) == baseline


@pytest.mark.parametrize("mask", [
    [], [True], [True, True, True], [True, 0], [True, 1],
    [True, 1.0], [True, None], [True, "true"],
])
def test_window_mask_requires_one_strict_bool_per_window(mask) -> None:
    windows, vectors = timeline([(0, 800, 0)], 800, hop_ms=400)
    with pytest.raises(ValueError, match="window_mask"):
        detect(windows, vectors, end_ms=800, window_mask=mask)


def test_all_false_mask_removes_support_without_compressing_any_evidence_grid() -> None:
    windows, vectors = timeline([(0, 4000, 0), (4000, 8000, 1)], 8000,
                                window_ms=1000, hop_ms=400)
    mask = [False] * len(windows)
    snapshot = deepcopy((windows, vectors, mask))
    baseline = detect(windows, vectors, end_ms=8000)
    result = detect(windows, vectors, end_ms=8000, window_mask=mask)
    assert result["candidates"] == []
    assert result["ignored_window_indices"] == list(range(len(windows)))
    assert result["matrix"] == baseline["matrix"]
    assert result["foote"] == baseline["foote"]
    assert result["foote"]
    assert result["uncovered"] == baseline["uncovered"]
    assert [row["time_ms"] for row in result["scans"]] == [window["start_ms"] for window in windows[1:]]
    for row, original in zip(result["scans"], baseline["scans"], strict=True):
        for field in ("left_indices", "right_indices", "left_count", "right_count"):
            assert row[field] == original[field]
        assert row["left_pairs"] == row["right_pairs"] == row["cross_pairs"] == 0
        assert row["left_similarity"] is row["right_similarity"] is row["cross_similarity"] is None
        assert row["kind"] is None
    assert (windows, vectors, mask) == snapshot


def test_masked_neighborhood_does_not_backfill_older_windows_for_support() -> None:
    windows, vectors = timeline([(0, 4000, 0), (4000, 8000, 1)], 8000,
                                window_ms=1000, hop_ms=400)
    mask = [index not in range(2, 8) for index in range(len(windows))]
    result = detect(windows, vectors, end_ms=8000, window_mask=mask)
    cut = next(row for row in result["scans"] if row["time_ms"] == 4000)
    assert mask[0] and mask[1]
    assert cut["left_indices"] == [2, 3, 4, 5, 6, 7]
    assert cut["right_indices"] == [10, 11, 12, 13, 14, 15]
    assert cut["left_count"] == cut["right_count"] == SETTINGS["side_windows"]
    assert cut["left_pairs"] == cut["cross_pairs"] == 0
    assert cut["right_pairs"] >= SETTINGS["min_pairs"]
    assert cut["kind"] is None


def test_explicit_silence_mask_suppresses_a_same_speaker_silence_same_speaker_change() -> None:
    # Axis 1 stands for a distinct frozen silence embedding, not another speaker.
    windows, vectors = timeline([(0, 4000, 0), (4000, 8000, 1), (8000, 12000, 0)], 12000,
                                window_ms=1000, hop_ms=400)
    mask = [window["end_ms"] <= 4000 or window["start_ms"] >= 8000 for window in windows]
    raw = detect(windows, vectors, end_ms=12000)
    result = detect(windows, vectors, end_ms=12000, window_mask=mask)
    assert len(raw["candidates"]) == 2
    assert result["candidates"] == []
    assert result["ignored_window_indices"] == [index for index, keep in enumerate(mask) if not keep]
    assert result["foote"] == raw["foote"]


def test_explicit_silence_mask_keeps_available_a_silence_b_change_support() -> None:
    # The manual oracle mask is known here; this does not evaluate a VAD model.
    windows, vectors = timeline([(0, 4000, 0), (4000, 4400, 1), (4400, 8400, 2)], 8400,
                                window_ms=1000, hop_ms=400, dimensions=3)
    mask = [window["end_ms"] <= 4000 or window["start_ms"] >= 4400 for window in windows]
    result = detect(windows, vectors, end_ms=8400, window_mask=mask)
    assert result["ignored_window_indices"]
    assert any(candidate["kind"] == "block_change" for candidate in result["candidates"])
    supported = [row for row in result["scans"] if row["kind"] == "block_change"]
    assert supported
    assert all(min(row["left_pairs"], row["right_pairs"], row["cross_pairs"]) >= SETTINGS["min_pairs"]
               for row in supported)
