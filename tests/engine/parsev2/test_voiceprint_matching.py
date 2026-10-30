from __future__ import annotations

from copy import deepcopy
from math import cos, radians, sin

import pytest

from engine.parsev2.voiceprint_matching import (
    classify_voiceprint_result,
    cluster_samples,
    eligible_references,
    is_label_doubtful,
    score_baseline,
    score_clusters,
    score_voiceprint_batch,
    score_voiceprint_line,
)


def sample(index: int, *, label: str | None = "A", angle: float = 0, **changes) -> dict:
    row = {
        "id": f"line-{index:03}", "cue_ids": [f"cue-{index:03}"],
        "start_ms": index * 3000, "end_ms": index * 3000 + 1500,
        "pcm_sha256": f"pcm-{index:03}",
        "vector": [cos(radians(angle)), sin(radians(angle))],
        "label": label, "model": "CAM++", "encoder_revision": "frozen",
    }
    row.update(changes)
    return row


def group(cluster_id: int, rows: list[dict]) -> dict:
    return {"cluster_id": cluster_id, "sample_ids": [row["id"] for row in rows]}


@pytest.mark.parametrize("changes", [
    {"id": "line-100"},
    {"cue_ids": ["other-cue", "cue-100"]},
    {"start_ms": 301_499, "end_ms": 302_000},
    {"pcm_sha256": "pcm-100"},
    {"model": "other"},
    {"encoder_revision": "other"},
])
def test_reference_excludes_target_leakage_and_incompatible_models(changes) -> None:
    query = sample(100)
    reference = sample(1, **changes)
    assert eligible_references(query, [query, reference]) == []


def test_touching_intervals_remain_independent_and_inputs_are_unchanged() -> None:
    query = sample(100)
    before = sample(1, start_ms=299_000, end_ms=300_000)
    after = sample(2, start_ms=301_500, end_ms=303_000)
    snapshot = deepcopy([query, before, after])
    assert [row["id"] for row in eligible_references(query, [after, query, before])] == [
        before["id"], after["id"],
    ]
    assert [query, before, after] == snapshot


def test_pcm_is_deduplicated_deterministically_and_conflicting_names_abstain() -> None:
    query = sample(100)
    first = sample(1)
    duplicate = sample(2, pcm_sha256=first["pcm_sha256"])
    assert eligible_references(query, [duplicate, first]) == [first]
    conflict = sample(3, label="B", pcm_sha256=first["pcm_sha256"],
                      start_ms=query["start_ms"], end_ms=query["end_ms"])
    references = eligible_references(query, [duplicate, conflict, first])
    assert len(references) == 1
    assert references[0]["id"] == first["id"]
    assert references[0]["label"] is None
    assert references[0]["identity_conflict"] is True
    assert first["label"] == "A"
    assert score_baseline(query, [first, conflict])["candidates"] == []


def test_baseline_excludes_target_before_trimming_and_refits_centroid() -> None:
    query = sample(100, label="wrong")
    references = [sample(index) for index in range(1, 5)]
    outlier = sample(5, angle=90)
    other = sample(6, label="B", angle=30)
    result = score_baseline(query, [query, *references, outlier, other])
    assert [row["speaker"] for row in result["candidates"]] == ["A", "B"]
    top = result["candidates"][0]
    assert top["similarity"] == pytest.approx(1)
    assert top["reference_count"] == 4
    assert top["independent_cue_count"] == 4
    assert top["reference_ids"] == [row["id"] for row in references]
    assert result["gap"] == pytest.approx(1 - cos(radians(30)))
    assert result["duration_ms"] == 1500


def test_empty_single_candidate_and_ties_have_explicit_deterministic_semantics() -> None:
    query = sample(100)
    assert score_baseline(query, [query])["candidates"] == []
    assert score_baseline(query, [query])["gap"] is None
    assert score_baseline(query, [sample(1)])["gap"] is None
    tied = score_baseline(query, [sample(1, label="B"), sample(2, label="A")])
    assert [row["speaker"] for row in tied["candidates"]] == ["A", "B"]
    assert tied["gap"] == 0


@pytest.mark.parametrize("rows", [
    [sample(1), sample(2)],
    [sample(index, cue_ids=["one-cue"]) for index in range(1, 5)],
    [sample(index, label="A" if index <= 3 else "B") for index in range(1, 7)],
    [sample(index, label="A" if index <= 3 else None) for index in range(1, 7)],
    [sample(index, label=None) for index in range(1, 5)],
])
def test_clusters_without_unique_independent_pure_support_stay_unnamed(rows) -> None:
    result = score_clusters(sample(100), rows, [group(0, rows)])
    assert result["clusters"][0]["speaker"] is None
    assert result["candidates"] == []
    assert result["gap"] is None


def test_null_and_conflicting_cues_count_in_purity_denominator() -> None:
    rows = [sample(index) for index in range(1, 4)]
    rows += [sample(4, label="B", cue_ids=rows[0]["cue_ids"]), sample(5, label=None)]
    result = score_clusters(sample(100), rows, [group(0, rows)], min_cues=2)
    diagnostic = result["clusters"][0]
    assert diagnostic["cue_count"] == 4
    assert diagnostic["label_support"] == {"A": 2}
    assert diagnostic["purity"] == 0.5
    assert diagnostic["speaker"] is None


def test_conflicting_cue_abstains_even_when_rows_land_in_different_groups() -> None:
    rows = [sample(index) for index in range(1, 4)]
    conflict = sample(4, label="B", cue_ids=rows[0]["cue_ids"])
    result = score_clusters(sample(100), rows + [conflict], [group(0, rows), group(1, [conflict])])
    assert result["clusters"][0]["label_support"] == {"A": 2}
    assert result["clusters"][0]["speaker"] is None
    assert result["clusters"][1]["label_support"] == {}


def test_exact_purity_boundary_and_target_removal_changes_cluster_name() -> None:
    query = sample(100)
    rows = [sample(1), sample(2), sample(3)]
    rows += [sample(4, label=None), sample(5, label=None)]
    scored = score_clusters(query, rows, [group(0, rows)])
    assert scored["clusters"][0]["purity"] == 0.6
    assert scored["candidates"][0]["speaker"] == "A"
    rows = [query, sample(1), sample(2), sample(3, label="B"), sample(4, label="B")]
    scored = score_clusters(query, rows, [group(0, rows)])
    assert scored["clusters"][0]["speaker"] is None
    assert scored["clusters"][0]["label_support"] == {"A": 2, "B": 2}
    assert query["id"] not in scored["clusters"][0]["sample_ids"]


def test_multiple_groups_per_character_do_not_occupy_top2_and_pooling_differs() -> None:
    near = [sample(index, angle=0) for index in range(1, 4)]
    distant = [sample(index, angle=100) for index in range(4, 7)]
    other = [sample(index, label="B", angle=30) for index in range(7, 10)]
    rows = near + distant + other
    groups = [group(0, near), group(1, distant), group(2, other)]
    maximum = score_clusters(sample(100), rows, groups, aggregation="max")
    pooled = score_clusters(sample(100), rows, groups, aggregation="pooled")
    assert [row["speaker"] for row in maximum["candidates"]] == ["A", "B"]
    assert maximum["gap"] == pytest.approx(1 - cos(radians(30)))
    assert maximum["candidates"][0]["reference_ids"] == [row["id"] for row in near]
    assert [row["speaker"] for row in pooled["candidates"]] == ["B", "A"]
    assert pooled["candidates"][1]["similarity"] == pytest.approx(cos(radians(50)))
    assert pooled["candidates"][1]["reference_count"] == 6
    assert pooled["candidates"][1]["cluster_ids"] == [0, 1]


def test_cluster_character_ties_are_lexical_and_report_zero_gap() -> None:
    a = [sample(index, label="A") for index in range(1, 4)]
    b = [sample(index, label="B") for index in range(4, 7)]
    result = score_clusters(sample(100), a + b, [group(0, b), group(1, a)])
    assert [candidate["speaker"] for candidate in result["candidates"]] == ["A", "B"]
    assert result["gap"] == 0


@pytest.mark.parametrize("method", ["kmeans", "agglomerative"])
def test_clustering_is_deterministic_and_canonical_under_input_reordering(method) -> None:
    rows = [sample(index, angle=angle) for index, angle in enumerate([0, 3, 6, 90, 93, 96])]
    settings = {"method": method, "k": 2, "threshold": 0.1, "starts": 4, "max_iter": 20}
    result = cluster_samples(rows, **settings)
    assert result == cluster_samples(list(reversed(rows)), **settings)
    assert result["k"] == 2
    assert result["groups"] == [group(0, rows[:3]), group(1, rows[3:])]


@pytest.mark.parametrize("method", ["kmeans", "agglomerative"])
def test_clustering_handles_no_samples_singletons_and_identical_vectors(method) -> None:
    settings = {"method": method, "k": 8, "starts": 2, "max_iter": 10}
    assert cluster_samples([], **settings)["k"] == 0
    first = sample(1)
    assert cluster_samples([first], **settings)["groups"] == [group(0, [first])]
    rows = [sample(1), sample(2), sample(3)]
    result = cluster_samples(rows, **settings)
    assert result["k"] == 1
    assert result["groups"] == [group(0, rows)]
    if method == "kmeans":
        assert result["requested_k"] == 8


@pytest.mark.parametrize("vector", [[0, 0], [float("nan"), 1], [[1, 0]]])
def test_invalid_vectors_fail_without_producing_scores(vector) -> None:
    with pytest.raises(ValueError):
        score_baseline(sample(100, vector=vector), [sample(1)])
    with pytest.raises(ValueError):
        cluster_samples([sample(1, vector=vector)], method="kmeans", k=1)


def production_sample(index: int, **changes) -> dict:
    row = sample(index, **changes)
    row["vector"] = row["vector"] + [0.0] * 190
    row["chunk_id"] = f"chunk-{index:03}"
    row["source_version"] = 1
    return row


def production_score(query: dict, references: list[dict]) -> dict:
    return score_voiceprint_line(
        query, references, model="CAM++", encoder_revision="frozen",
        dimensions=192, trim_fraction=0.2, min_duration_ms=400,
    )


LABEL_POLICY = {
    "label_min_rank": 3, "label_min_margin": 0.15,
    "label_min_duration_ms": 1000, "label_min_ref_count": 3,
    "label_min_consistency": 0.70,
}
LABEL_FIELDS = ("label_score", "label_rank", "label_count", "label_margin",
                "label_ref_count", "label_ref_consistency", "label_ref_higher_count",
                "label_ref_higher_fraction")
TAIL_POLICY = {key: value for key, value in LABEL_POLICY.items() if key != "label_min_consistency"}
TAIL_POLICY["label_min_ref_higher_fraction"] = 0.95


def label_score(query, references, **policy):
    return score_voiceprint_line(
        query, references, model="CAM++", encoder_revision="frozen", dimensions=192,
        trim_fraction=0.2, min_duration_ms=400, **{**LABEL_POLICY, **policy},
    )


def label_batch(rows):
    return score_voiceprint_batch(
        rows, model="CAM++", encoder_revision="frozen", dimensions=192,
        trim_fraction=0.2, min_duration_ms=400, **LABEL_POLICY,
    )


def tail_score(query, references, **policy):
    return score_voiceprint_line(
        query, references, model="CAM++", encoder_revision="frozen", dimensions=192,
        trim_fraction=0.2, min_duration_ms=400, **{**TAIL_POLICY, **policy},
    )


def test_production_score_keeps_rare_characters_top3_and_reference_ids_only() -> None:
    query = production_sample(100)
    references = [production_sample(1, label="A", angle=0),
                  production_sample(2, label="B", angle=30),
                  production_sample(3, label="C", angle=60),
                  production_sample(4, label="D", angle=90)]
    before = deepcopy([query, *references])
    result = production_score(query, [query, *references])
    assert result["method"] == "B0"
    assert result["reason"] == "compared"
    assert result["candidate_count"] == 4
    assert [candidate["speaker"] for candidate in result["candidates"]] == ["A", "B", "C"]
    assert all(candidate["reference_count"] == 1 for candidate in result["candidates"])
    assert result["gap"] == pytest.approx(1 - cos(radians(30)))
    assert [candidate["reference_ids"] for candidate in result["candidates"]] == [
        [reference["id"]] for reference in references[:3]
    ]
    assert all("src" not in candidate and "vector" not in candidate
               for candidate in result["candidates"])
    result["candidates"][0]["reference_ids"].append("mutated-result")
    assert [query, *references] == before


def test_production_score_preserves_all_exclusions_and_single_candidate_gap() -> None:
    query = production_sample(100)
    allowed = production_sample(1, label="rare")
    invalid = [
        query,
        production_sample(2, cue_ids=query["cue_ids"]),
        production_sample(3, start_ms=query["start_ms"], end_ms=query["end_ms"]),
        production_sample(4, pcm_sha256=query["pcm_sha256"]),
        production_sample(5, encoder_revision="old"),
    ]
    duplicate = production_sample(6, label="rare", pcm_sha256=allowed["pcm_sha256"])
    conflict_a = production_sample(7, label="conflict-A")
    conflict_b = production_sample(8, label="conflict-B", pcm_sha256=conflict_a["pcm_sha256"])
    result = production_score(query, [allowed, *invalid, duplicate, conflict_a, conflict_b])
    assert len(result["candidates"]) == 1
    assert result["candidates"][0]["speaker"] == "rare"
    assert result["candidates"][0]["reference_ids"] == [allowed["id"]]
    assert result["gap"] is None
    assert result["excluded_reference_counts"] == {"incompatible_encoder": 1}


def test_under_400ms_target_retains_explicit_unscored_result_without_embedding() -> None:
    query = production_sample(100, end_ms=300_399)
    del query["vector"]
    result = production_score(query, [production_sample(1)])
    assert result["duration_ms"] == 399
    assert result["candidates"] == []
    assert result["gap"] is None
    assert result["reason"] == "audio_too_short"
    boundary = production_score(production_sample(100, end_ms=300_400), [production_sample(1)])
    assert boundary["duration_ms"] == 400
    assert boundary["candidates"][0]["speaker"] == "A"


@pytest.mark.parametrize("changes, reason", [
    ({"model": "WavLM"}, "incompatible_encoder"),
    ({"encoder_revision": "old"}, "incompatible_encoder"),
    ({"vector": [1.0] + [0.0] * 190}, "invalid_embedding"),
    ({"vector": [float("nan")] + [0.0] * 191}, "invalid_embedding"),
    ({"vector": [2.0] + [0.0] * 191}, "non_normalized_embedding"),
    ({"vector": [0.0] * 192}, "non_normalized_embedding"),
])
def test_frozen_embedding_contract_rejects_target_and_excludes_reference(changes, reason) -> None:
    query = production_sample(100)
    bad_query = {**query, **changes}
    with pytest.raises(ValueError, match=reason):
        production_score(bad_query, [production_sample(1)])
    reference = {**production_sample(1), **changes}
    result = production_score(query, [reference])
    assert result["candidates"] == []
    assert result["reason"] == "no_eligible_reference"
    assert result["excluded_reference_counts"] == {reason: 1}


def decision(result: dict, **changes) -> dict:
    options = {
        "current_label": "A", "song": False, "overlap": False,
        "accept_gap": 0.15, "accept_duration_ms": 1500, "accept_min_cues": 3,
        "conflict_gap": 0.05, "no_voice_duration_ms": 1000,
    }
    options.update(changes)
    return classify_voiceprint_result(result, **options)


def evidence(*, speaker="A", gap=0.15, duration_ms=1500, cues=3) -> dict:
    return {"candidates": [{"speaker": speaker, "independent_cue_count": cues}],
            "gap": gap, "duration_ms": duration_ms, "reason": "compared"}


@pytest.mark.parametrize("result, options, expected", [
    (evidence(), {}, ("ACCEPT", False)),
    (evidence(speaker="B"), {}, ("ACCEPT", True)),
    (evidence(), {"current_label": None}, ("ACCEPT", True)),
    (evidence(speaker="B", gap=0.05), {}, ("CONFLICT", True)),
    (evidence(speaker="B", gap=0.149999), {}, ("CONFLICT", True)),
    (evidence(gap=0.05), {}, (None, False)),
    (evidence(speaker="B", gap=0.049999), {}, ("NO_VOICE", False)),
    (evidence(speaker="B", duration_ms=999), {}, ("NO_VOICE", False)),
    (evidence(speaker="B", gap=0.1, duration_ms=1000), {}, ("CONFLICT", True)),
    (evidence(duration_ms=1000), {}, (None, False)),
    (evidence(), {"song": True}, (None, False)),
    (evidence(), {"overlap": True}, (None, False)),
    (evidence(cues=2), {}, (None, False)),
    (evidence(gap=None), {}, ("NO_VOICE", False)),
    ({"candidates": [], "duration_ms": 1500, "gap": None,
      "reason": "no_eligible_reference"}, {}, ("NO_VOICE", False)),
])
def test_decision_bands_keep_accept_disagreements_eligible(result, options, expected) -> None:
    before = deepcopy(result)
    classified = decision(result, **options)
    assert (classified["band"], classified["conflict"]) == expected
    if classified["band"] != "ACCEPT" or classified["conflict"]:
        assert classified["review_reason"]
    assert result == before


@pytest.mark.parametrize("speaker, expected_band", [("A", None), ("B", "CONFLICT")])
@pytest.mark.parametrize("result_changes, options, reason", [
    ({"duration_ms": 1200}, {}, "duration_below_accept_minimum"),
    ({}, {"song": True}, "song"),
    ({}, {"overlap": True}, "overlapping_dialogue"),
    ({"cues": 2}, {}, "insufficient_independent_reference_cues"),
])
def test_high_gap_accept_ineligible_disagreement_stays_in_conflict_review(
    speaker, expected_band, result_changes, options, reason,
) -> None:
    result = evidence(speaker=speaker, gap=0.30, **result_changes)
    before = deepcopy(result)
    classified = decision(result, **options)
    assert classified == {
        "band": expected_band, "conflict": speaker != "A", "review_reason": reason,
    }
    assert result == before


def test_accept_support_counts_independent_cues_after_trimming() -> None:
    query = production_sample(100)
    same_cue = [production_sample(index, cue_ids=["one-cue"]) for index in range(1, 5)]
    trimmed = production_sample(5, angle=90)
    other = production_sample(6, label="B", angle=90)
    result = production_score(query, [*same_cue, trimmed, other])
    assert result["candidates"][0]["reference_count"] == 4
    assert result["candidates"][0]["independent_cue_count"] == 1
    assert trimmed["id"] not in result["candidates"][0]["reference_ids"]
    classified = decision(result)
    assert classified["band"] is None
    assert classified["review_reason"] == "insufficient_independent_reference_cues"


def test_b0_module_import_does_not_load_scipy() -> None:
    import subprocess
    import sys

    script = """
import sys
class RejectScipy:
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'scipy' or fullname.startswith('scipy.'):
            raise RuntimeError('B0 must not import scipy')
sys.meta_path.insert(0, RejectScipy())
from engine.parsev2.voiceprint_matching import score_voiceprint_line
"""
    subprocess.run([sys.executable, "-c", script], check=True, capture_output=True, text=True)


def test_label_diagnostics_use_full_ranking_and_trimmed_reference_consistency() -> None:
    query = production_sample(100, label="D")
    references = [production_sample(index, label=label, angle=angle)
                  for index, (label, angle) in enumerate(
                      [("A", 0)] * 3 + [("B", 20)] * 3 + [("C", 40)] * 3
                      + [("D", 70)] * 3, 1)]
    result = label_score(query, [query, *references])
    assert [row["speaker"] for row in result["candidates"]] == ["A", "B", "C"]
    assert result["label_rank"] == result["label_count"] == 4
    assert result["label_score"] == pytest.approx(cos(radians(70)))
    assert result["label_margin"] == pytest.approx(1 - cos(radians(70)))
    assert result["label_ref_count"] == 3
    assert result["label_ref_consistency"] == pytest.approx(1)
    assert result["label_doubtful"] is True
    # The old API has no hidden policy defaults and retains the same ranking.
    legacy = production_score(query, [query, *references])
    assert legacy["candidates"] == result["candidates"]
    assert legacy["gap"] == result["gap"]
    assert legacy["label_doubtful"] is False

    mixed = [production_sample(index, label="D", angle=angle)
             for index, angle in enumerate([-60, 60, 0, 0, 180], 1)]
    trimmed = label_score(query, [*mixed, production_sample(9, label="A")])
    assert trimmed["label_ref_count"] == 4
    assert trimmed["label_ref_consistency"] == pytest.approx(0.75)


@pytest.mark.parametrize("query, references", [
    (production_sample(100, label=None), [production_sample(1)]),
    (production_sample(100, end_ms=300_399), [production_sample(1)]),
    (production_sample(100, label="missing"), [production_sample(1)]),
    (production_sample(100), []),
])
def test_null_or_unscored_label_has_six_null_diagnostics(query, references) -> None:
    result = label_score(query, references)
    assert all(result[key] is None for key in LABEL_FIELDS)
    assert result["label_doubtful"] is False


def test_label_flag_counts_pcm_rows_not_cues_and_adds_no_song_overlap_gate() -> None:
    query = production_sample(100, label="C", song=True, overlap=True)
    references = [production_sample(index, label=label, angle=angle,
                                   cue_ids=[f"shared-{label}"])
                  for index, (label, angle) in enumerate(
                      [("A", 0)] * 3 + [("B", 20)] * 3 + [("C", 90)] * 3, 1)]
    result = label_score(query, references)
    assert result["label_ref_count"] == 3
    assert result["label_doubtful"] is True
    rare = label_score(query, references[:-1])
    assert rare["label_ref_count"] == 2
    assert rare["label_ref_consistency"] == pytest.approx(1)
    assert rare["label_doubtful"] is False
    assert any(row["speaker"] == "C" for row in rare["candidates"])


@pytest.mark.parametrize("changes", [
    {"label_min_rank": 0}, {"label_min_rank": True},
    {"label_min_duration_ms": 0}, {"label_min_ref_count": 0},
    {"label_min_margin": -0.1}, {"label_min_margin": 2.1},
    {"label_min_margin": float("nan")}, {"label_min_consistency": -0.1},
    {"label_min_consistency": 1.1}, {"label_min_consistency": float("inf")},
])
def test_invalid_label_thresholds_fail_even_for_unscored_targets(changes) -> None:
    with pytest.raises(ValueError):
        label_score(production_sample(100, end_ms=300_399), [], **changes)


def test_label_policy_is_optional_as_a_whole_and_boundaries_are_inclusive() -> None:
    with pytest.raises(ValueError, match="tail threshold"):
        score_voiceprint_line(production_sample(100), [], model="CAM++", encoder_revision="frozen",
                              dimensions=192, trim_fraction=0.2, min_duration_ms=400, label_min_rank=3)
    boundary = {"label_rank": 3, "label_margin": 0.15, "duration_ms": 1000,
                "label_ref_count": 3, "label_ref_consistency": 0.70}
    assert is_label_doubtful(boundary, **LABEL_POLICY)
    for key in boundary:
        assert not is_label_doubtful({**boundary, key: boundary[key] - 0.001}, **LABEL_POLICY)


@pytest.mark.parametrize("metrics", [
    {"label_rank": 7, "label_count": 11, "label_score": 0.48096527056253957,
     "label_margin": 0.28659438643592805, "duration_ms": 1300,
     "label_ref_count": 73, "label_ref_consistency": 0.7649639875350588},
    {"label_rank": 8, "label_count": 11, "label_score": 0.5170295242916154,
     "label_margin": 0.2745646161043229, "duration_ms": 2500,
     "label_ref_count": 4, "label_ref_consistency": 0.7753087188487837},
])
def test_confirmed_ep04_original_labels_satisfy_doubtful_policy(metrics) -> None:
    # Frozen v12 metrics; actual cached-vector integration is the offline CLI.
    assert is_label_doubtful(metrics, **LABEL_POLICY)


def test_two_pass_exclusions_survive_cleared_flags_and_preserve_input_order() -> None:
    rows = [production_sample(index, label="C", angle=angle)
            for index, angle in enumerate([90, 270, 180, 180], 1)]
    rows += [production_sample(index, label=label, angle=angle)
             for index, (label, angle) in enumerate(
                 [("A", 90)] * 3 + [("B", 100)] * 3 + [("D", 270)] * 3 + [("E", 260)] * 3, 5)]
    before = deepcopy(rows)
    first = [label_score(row, rows) for row in rows]
    result = label_batch(rows)
    assert result["pass1_scores"] == first
    assert [row["duration_ms"] for row in result["scores"]] == [row["duration_ms"] for row in first]
    for query, score in zip(rows, result["scores"]):
        expected = score_voiceprint_line(
            query, rows, model="CAM++", encoder_revision="frozen", dimensions=192,
            trim_fraction=0.2, min_duration_ms=400, excluded_reference_ids={rows[0]["id"], rows[1]["id"]},
            **LABEL_POLICY,
        )
        assert score == expected
    assert result["hygiene"] == {
        "passes": 2, "pass1_flag_count": 2, "pass2_flag_count": 0,
        "changed_flag_count": 2, "newly_flagged_ids": [],
        "cleared_flag_ids": [rows[0]["id"], rows[1]["id"]],
        "excluded_reference_ids": [rows[0]["id"], rows[1]["id"]],
    }
    assert all(not set(candidate["reference_ids"]) & set(result["hygiene"]["excluded_reference_ids"])
               for score in result["scores"] for candidate in score["candidates"])
    assert rows == before
    assert label_batch([])["hygiene"]["passes"] == 2
    with pytest.raises(ValueError, match="IDs must be unique"):
        label_batch([rows[0], rows[0]])


def test_pass_two_preserves_original_conflicting_pcm_names_after_exclusion() -> None:
    wrong = production_sample(1, label="C", angle=90)
    duplicate = production_sample(2, label="A", angle=90, pcm_sha256=wrong["pcm_sha256"])
    rows = [wrong, duplicate]
    rows += [production_sample(index, label=label, angle=angle)
             for index, (label, angle) in enumerate(
                 [("A", 90)] * 3 + [("B", 100)] * 3 + [("C", 180)] * 3, 3)]
    eligible = eligible_references(production_sample(100), rows, excluded_reference_ids={wrong["id"]})
    retained_duplicate = next(row for row in eligible if row["id"] == duplicate["id"])
    assert retained_duplicate["label"] is None
    result = label_batch(rows)
    assert wrong["id"] in result["hygiene"]["excluded_reference_ids"]
    assert duplicate["id"] not in result["hygiene"]["excluded_reference_ids"]
    assert all(duplicate["id"] not in candidate["reference_ids"]
               for score in result["scores"] for candidate in score["candidates"])


@pytest.mark.parametrize("label", ["椎名立希", "高松灯"])
def test_empirical_tail_flags_outlier_despite_low_mean_reference_coherence(label) -> None:
    query = production_sample(100, label=label, angle=180)
    references = [production_sample(index, label=label, angle=angle)
                  for index, angle in enumerate([60, 60, -60, -60], 1)]
    references += [production_sample(8, label="A", angle=180), production_sample(9, label="B", angle=160)]
    result = tail_score(query, references)
    assert result["label_ref_consistency"] == pytest.approx(0.5)
    assert result["label_rank"] == 3
    assert result["label_ref_higher_count"] == result["label_ref_count"] == 4
    assert result["label_ref_higher_fraction"] == 1
    assert result["label_doubtful"] is True
    assert label_score(query, references)["label_doubtful"] is False  # frozen v2 mean-floor policy


def test_empirical_tail_accepts_broad_within_label_spread_and_counts_ties_strictly() -> None:
    references = [production_sample(index, label="C", angle=angle)
                  for index, angle in enumerate([60, 60, -60, -60], 1)]
    for target_angle in (50, 60):
        query = production_sample(100, label="C", angle=target_angle)
        nearby = [production_sample(8, label="A", angle=target_angle),
                  production_sample(9, label="B", angle=target_angle - 10)]
        result = tail_score(query, [*references, *nearby])
        assert result["label_rank"] == 3 and result["label_margin"] >= 0.15
        assert result["label_ref_higher_count"] == 0
        assert result["label_ref_higher_fraction"] == 0
        assert result["label_doubtful"] is False


@pytest.mark.parametrize("count, higher, expected", [(3, 3, True), (3, 2, False),
                                                     (20, 19, True), (20, 18, False)])
def test_empirical_tail_fraction_has_inclusive_95_percent_boundary(count, higher, expected) -> None:
    result = {"label_rank": 3, "label_margin": 0.15, "duration_ms": 1000,
              "label_ref_count": count, "label_ref_higher_count": higher,
              "label_ref_higher_fraction": higher / count, "label_ref_consistency": 0.1}
    assert is_label_doubtful(result, **TAIL_POLICY) is expected


def test_tail_population_is_same_kept_references_not_untrimmed_pool_or_reference_loo() -> None:
    query = production_sample(100, label="C", angle=180)
    references = [production_sample(index, label="C", angle=angle)
                  for index, angle in enumerate([60, 60, -60, -60, 180], 1)]
    result = tail_score(query, [*references, production_sample(8, label="A", angle=180),
                               production_sample(9, label="B", angle=160)])
    own = result["candidates"][2]
    assert result["label_ref_count"] == result["label_ref_higher_count"] == 4
    assert result["label_ref_higher_fraction"] == 1
    assert references[-1]["id"] not in own["reference_ids"]
    assert own["reference_higher_count"] == 4
    assert own["reference_higher_fraction"] == 1
    assert result["label_doubtful"] is True
    # Three refs at60,0,-60 retain themselves in the common centroid. Their
    # benchmark is .5,1,.5, not the lower reference-LOO values 0,1,0.
    simple = [production_sample(index, label="C", angle=angle)
              for index, angle in enumerate([60, 0, -60], 1)]
    result = tail_score(production_sample(100, label="C", angle=70), simple)
    assert result["label_ref_higher_count"] == 3


@pytest.mark.parametrize("minimum", [-0.1, 1.1, float("nan"), float("inf"), True])
def test_invalid_empirical_tail_threshold_is_rejected(minimum) -> None:
    with pytest.raises(ValueError):
        tail_score(production_sample(100), [], label_min_ref_higher_fraction=minimum)


def test_legacy_mean_and_new_empirical_policies_cannot_be_combined() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        tail_score(production_sample(100), [], label_min_consistency=0.70)
    with pytest.raises(ValueError, match="exactly one"):
        score_voiceprint_batch([], model="CAM++", encoder_revision="frozen", dimensions=192,
                               trim_fraction=.2, min_duration_ms=400, **{
                                   key: value for key, value in TAIL_POLICY.items()
                                   if key != "label_min_ref_higher_fraction"})


def test_four_reference_loo_calibration_and_production_few_reference_fallback_are_distinct() -> None:
    query = production_sample(100, label="C", angle=95)
    references = [production_sample(index, label="C", angle=angle)
                  for index, angle in enumerate([0, 20, 30, 90], 1)]
    references += [production_sample(8, label="A", angle=95), production_sample(9, label="B", angle=85)]
    current = score_baseline(query, references)  # current default: reference LOO
    shared = score_baseline(query, references, reference_leave_one_out=False)
    own = current["candidates"][2]
    assert own["reference_higher_count"] == 3
    assert own["reference_higher_fraction"] == 0.75
    assert shared["candidates"][2]["reference_higher_count"] == 4
    legacy = tail_score(query, references)
    assert legacy["label_ref_higher_fraction"] == 1
    assert legacy["label_doubtful"] is True
    assert legacy["label_doubtful_basis"] is None
    # An isolated 95% LOO test fails; production intentionally uses the
    # common-rank/margin fallback for this four-reference cohort.
    assert not is_label_doubtful({**legacy, "label_ref_higher_fraction": 0.75}, **TAIL_POLICY)
    production = tail_score(query, references, label_tail_min_refs=8)
    assert production["label_ref_higher_count"] == 3
    assert production["label_ref_higher_fraction"] == 0.75
    assert production["label_doubtful"] is True
    assert production["label_doubtful_basis"] == "rank_margin_only_few_refs"


@pytest.mark.parametrize("count,higher,expected", [(2, 2, False), (3, 0, True), (7, 0, True),
                                                   (8, 7, False), (8, 8, True), (20, 19, True)])
def test_current_tail_starts_at_eight_without_changing_three_reference_common_gate(count, higher, expected):
    result = {"label_rank": 3, "label_margin": 0.15, "duration_ms": 1000,
              "label_ref_count": count, "label_ref_higher_count": higher,
              "label_ref_higher_fraction": higher / count}
    assert is_label_doubtful(result, **TAIL_POLICY, label_tail_min_refs=8) is expected
    assert not is_label_doubtful({**result, "label_margin": 0.149}, **TAIL_POLICY, label_tail_min_refs=8)


def test_current_large_cohort_uses_tail_basis_and_preserves_target_scores():
    query = production_sample(100, label="C", angle=180)
    references = [production_sample(index, label="C") for index in range(1, 10)]
    references += [production_sample(20, label="A", angle=180), production_sample(21, label="B", angle=170)]
    current = tail_score(query, references, label_tail_min_refs=8)
    legacy = tail_score(query, references)
    assert current["label_ref_count"] == current["label_ref_higher_count"] == 8
    assert current["label_ref_higher_fraction"] == 1
    assert current["label_doubtful_basis"] == "tail"
    for left, right in zip(current["candidates"], legacy["candidates"]):
        assert left["similarity"] == right["similarity"]
        assert left["reference_ids"] == right["reference_ids"]
        assert left["reference_consistency"] == right["reference_consistency"]
    assert current["gap"] == legacy["gap"]


def test_degenerate_reference_loo_abstains_without_dropping_candidate_or_shrinking_denominator():
    def vector(*values):
        return [*values, *([0.0] * (192 - len(values)))]
    vectors = [vector(0, 0, 0, 1), vector(1, 0, 0, 0), vector(-.5, sin(radians(120)), 0, 0),
               vector(-.5, -sin(radians(120)), 0, 0), vector(0, 1, 0, 0), vector(0, -1, 0, 0),
               vector(0, 0, 1, 0), vector(0, 0, -1, 0)]
    references = [{**production_sample(index, label="C"), "vector": value}
                  for index, value in enumerate(vectors, 1)]
    references += [production_sample(20, label="A"), production_sample(21, label="B", angle=30)]
    query = production_sample(100, label="C")
    result = score_voiceprint_line(query, references, model="CAM++", encoder_revision="frozen",
                                  dimensions=192, trim_fraction=0, min_duration_ms=400,
                                  **TAIL_POLICY, label_tail_min_refs=8)
    assert result["label_ref_count"] == 8
    assert result["label_ref_higher_count"] is result["label_ref_higher_fraction"] is None
    assert result["label_doubtful"] is False
    assert result["label_doubtful_basis"] is None
    rare = score_baseline(query, [production_sample(1, label="rare")])["candidates"][0]
    assert rare["speaker"] == "rare" and rare["reference_count"] == 1
    assert rare["reference_higher_count"] is rare["reference_higher_fraction"] is None


@pytest.mark.parametrize("minimum", [0, 2, True, 8.0])
def test_tail_population_minimum_is_validated_without_weakening_the_common_minimum(minimum):
    with pytest.raises(ValueError, match="label_tail_min_refs"):
        tail_score(production_sample(100), [], label_tail_min_refs=minimum)
    with pytest.raises(ValueError, match="label_tail_min_refs"):
        label_score(production_sample(100), [], label_tail_min_refs=8)


@pytest.mark.parametrize("metrics,isolated_tail", [
    ({"label_rank": 6, "label_margin": 0.2891652123734639, "duration_ms": 1300,
      "label_ref_count": 70, "label_ref_higher_count": 70, "label_ref_higher_fraction": 1}, True),
    ({"label_rank": 7, "label_margin": 0.2701004322619014, "duration_ms": 2500,
      "label_ref_count": 4, "label_ref_higher_count": 3, "label_ref_higher_fraction": .75}, False),
])
def test_confirmed_ep04_current_policy_keeps_both_flags_without_calling_few_reference_case_a_tail_success(
    metrics, isolated_tail,
):
    # Actual v12-vector LOO diagnostics for 47.4–48.7s and 293.5–296.0s.
    # The CLI checks actual vectors; this regression freezes policy semantics.
    assert is_label_doubtful(metrics, **TAIL_POLICY, label_tail_min_refs=8)
    assert is_label_doubtful(metrics, **TAIL_POLICY) is isolated_tail


def test_few_reference_fallback_keeps_common_gate_when_loo_calibration_is_degenerate():
    query = production_sample(100, label="C", angle=90)
    references = [production_sample(1, label="C", vector=[1, 0]),
                  production_sample(2, label="C", vector=[0, 1]),
                  production_sample(3, label="C", vector=[0, -1]),
                  production_sample(8, label="A", angle=90), production_sample(9, label="B", angle=60)]
    result = tail_score(query, references, label_tail_min_refs=8)
    assert result["label_ref_count"] == 3
    assert result["label_ref_higher_count"] is result["label_ref_higher_fraction"] is None
    assert result["label_doubtful"] is True
    assert result["label_doubtful_basis"] == "rank_margin_only_few_refs"
