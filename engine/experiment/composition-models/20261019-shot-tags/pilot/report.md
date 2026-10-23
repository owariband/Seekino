# Shot-tag pilot report

Generated: 2026-10-19T02:34:58.560568+00:00

Pilot gate: **proceed**. Selected model kind: **primary**.

These are consistency measurements, not accuracy or retrieval relevance. No relevance judgments were generated.

## Coverage and schema validity

Rates use all 200 pilot frames, including API and runner failures. A retry does not overwrite the first-response result.

| Run | First response | Final valid | Retried | API errors |
|---|---:|---:|---:|---:|
| primary_a | 200/200 (100.0%) | 200/200 (100.0%) | 0 | 0 |
| primary_b | 200/200 (100.0%) | 200/200 (100.0%) | 0 | 0 |
| cheap | 198/200 (99.0%) | 200/200 (100.0%) | 2 | 0 |

## Agreement

Unweighted Cohen's kappa uses valid pairs only; invalid records are excluded with coverage shown. Valid `unknown` labels remain ordinary categories. Person count uses 0, 1, 2, 3–5, and 6+ (`many`) reporting buckets.

### primary_a_vs_primary_b

| Field | Valid pairs | Exact agreement | Cohen's kappa |
|---|---:|---:|---:|
| camera.shot_size | 200/200 | 81.5% | 0.784 |
| camera.vertical_angle | 200/200 | 95.5% | 0.888 |
| composition.primary | 200/200 | 81.0% | 0.770 |
| subjects.main_subject_cell | 200/200 | 88.5% | 0.747 |
| subjects.main_subject_scale | 200/200 | 80.5% | 0.682 |
| subjects.person_count | 200/200 | 93.0% | 0.898 |

### primary_a_vs_cheap

| Field | Valid pairs | Exact agreement | Cohen's kappa |
|---|---:|---:|---:|
| camera.shot_size | 200/200 | 56.5% | 0.497 |
| camera.vertical_angle | 200/200 | 88.5% | 0.673 |
| composition.primary | 200/200 | 51.5% | 0.394 |
| subjects.main_subject_cell | 200/200 | 78.0% | 0.573 |
| subjects.main_subject_scale | 200/200 | 72.0% | 0.522 |
| subjects.person_count | 200/200 | 92.5% | 0.891 |

### primary_b_vs_cheap

| Field | Valid pairs | Exact agreement | Cohen's kappa |
|---|---:|---:|---:|
| camera.shot_size | 200/200 | 57.5% | 0.505 |
| camera.vertical_angle | 200/200 | 88.0% | 0.670 |
| composition.primary | 200/200 | 48.0% | 0.359 |
| subjects.main_subject_cell | 200/200 | 78.5% | 0.579 |
| subjects.main_subject_scale | 200/200 | 73.5% | 0.553 |
| subjects.person_count | 200/200 | 89.5% | 0.849 |

## Gate

Both primary runs must be >=98% finally schema-valid over all 200 frames; primary self kappa must be >=0.6 for angle and size. Cheap is eligible only if >=98% valid and each primary_a-versus-cheap kappa is at least the corresponding primary self kappa minus 0.1. Higher cross kappa is allowed. Undefined kappa fails its check.

Root reviews the pilot gate before any full tagging.

```json
{
  "passed": true,
  "primary_pass": true,
  "primary_checks": {
    "primary_a_schema": true,
    "primary_a_artifact_integrity": true,
    "primary_b_schema": true,
    "primary_b_artifact_integrity": true,
    "self_camera.vertical_angle": true,
    "self_camera.shot_size": true
  },
  "cheap_checks": {
    "cheap_schema": true,
    "cheap_artifact_integrity": true,
    "cross_camera.vertical_angle_within_0.1": false,
    "cross_camera.shot_size_within_0.1": false
  },
  "cheap_eligible": false,
  "selected_model_kind": "primary",
  "selected_run": "primary_a",
  "selected_model": "gpt-5.6-sol",
  "decision": "proceed",
  "failed_checks": [],
  "rule": "Both primary runs must be >=98% finally schema-valid over all 200 frames; primary self kappa must be >=0.6 for angle and size. Cheap is eligible only if >=98% valid and each primary_a-versus-cheap kappa is at least the corresponding primary self kappa minus 0.1. Higher cross kappa is allowed. Undefined kappa fails its check.",
  "next_action": "Root reviews the pilot gate before any full tagging."
}
```

## PCP and detector comparison

PCP has nine native classes. Its `curved` label maps explicitly to VLM `curve` for canonical comparisons; all other native labels retain their names. The 13×9 matrices retain native PCP column names. Four VLM classes have no PCP equivalent. Neither source is treated as ground truth.

### primary_a

| VLM \ PCP | rule_of_thirds | vertical | horizontal | diagonal | curved | triangle | center | symmetric | pattern |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| center | 1 | 3 | 1 | 0 | 0 | 1 | 27 | 0 | 1 |
| rule_of_thirds | 14 | 4 | 0 | 1 | 1 | 3 | 38 | 0 | 1 |
| symmetric | 1 | 0 | 0 | 0 | 0 | 2 | 4 | 0 | 0 |
| horizontal | 4 | 1 | 0 | 3 | 0 | 0 | 5 | 1 | 0 |
| vertical | 3 | 1 | 0 | 0 | 1 | 1 | 1 | 0 | 0 |
| diagonal | 3 | 0 | 0 | 3 | 2 | 1 | 24 | 0 | 0 |
| triangle | 1 | 1 | 0 | 0 | 0 | 0 | 2 | 0 | 0 |
| curve | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| pattern | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| vanishing_point | 0 | 4 | 0 | 1 | 0 | 0 | 2 | 0 | 0 |
| frame_within_frame | 0 | 3 | 0 | 0 | 0 | 0 | 5 | 0 | 0 |
| fill_frame | 3 | 2 | 0 | 1 | 0 | 1 | 17 | 0 | 0 |
| none_clear | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |

Detector count bucket agreement: 78.5%; kappa 0.671; coverage 200/200. Exact-count direction counts: `{"vlm_higher": 21, "agree": 152, "detector_higher": 27}`.

Literal VLM many means >6; reporting buckets map both exact 6 and many to 6+. Detector count is retained boxes, not ground truth.

### primary_b

| VLM \ PCP | rule_of_thirds | vertical | horizontal | diagonal | curved | triangle | center | symmetric | pattern |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| center | 1 | 3 | 1 | 0 | 0 | 1 | 32 | 0 | 1 |
| rule_of_thirds | 14 | 4 | 0 | 1 | 1 | 1 | 35 | 0 | 1 |
| symmetric | 1 | 0 | 0 | 0 | 0 | 2 | 2 | 0 | 0 |
| horizontal | 5 | 2 | 0 | 2 | 0 | 0 | 5 | 1 | 0 |
| vertical | 1 | 0 | 0 | 0 | 1 | 0 | 1 | 0 | 0 |
| diagonal | 3 | 0 | 0 | 4 | 2 | 2 | 22 | 0 | 0 |
| triangle | 2 | 0 | 0 | 0 | 0 | 0 | 4 | 0 | 0 |
| curve | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| pattern | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| vanishing_point | 0 | 4 | 0 | 1 | 0 | 0 | 3 | 0 | 0 |
| frame_within_frame | 0 | 4 | 0 | 0 | 0 | 0 | 4 | 0 | 0 |
| fill_frame | 3 | 2 | 0 | 1 | 0 | 3 | 17 | 0 | 0 |
| none_clear | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |

Detector count bucket agreement: 75.0%; kappa 0.621; coverage 200/200. Exact-count direction counts: `{"vlm_higher": 27, "agree": 145, "detector_higher": 28}`.

Literal VLM many means >6; reporting buckets map both exact 6 and many to 6+. Detector count is retained boxes, not ground truth.

### cheap

| VLM \ PCP | rule_of_thirds | vertical | horizontal | diagonal | curved | triangle | center | symmetric | pattern |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| center | 0 | 1 | 0 | 0 | 0 | 1 | 16 | 0 | 0 |
| rule_of_thirds | 17 | 6 | 0 | 2 | 1 | 4 | 69 | 0 | 0 |
| symmetric | 2 | 6 | 1 | 2 | 0 | 2 | 9 | 1 | 1 |
| horizontal | 2 | 1 | 0 | 2 | 0 | 0 | 4 | 0 | 0 |
| vertical | 4 | 3 | 0 | 0 | 0 | 0 | 1 | 0 | 0 |
| diagonal | 0 | 0 | 0 | 3 | 2 | 2 | 14 | 0 | 0 |
| triangle | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| curve | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| pattern | 3 | 0 | 0 | 0 | 1 | 0 | 4 | 0 | 1 |
| vanishing_point | 0 | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| frame_within_frame | 0 | 0 | 0 | 0 | 0 | 0 | 2 | 0 | 0 |
| fill_frame | 2 | 1 | 0 | 0 | 0 | 0 | 5 | 0 | 0 |
| none_clear | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 0 |

Detector count bucket agreement: 75.5%; kappa 0.632; coverage 200/200. Exact-count direction counts: `{"vlm_higher": 17, "agree": 146, "detector_higher": 37}`.

Literal VLM many means >6; reporting buckets map both exact 6 and many to 6+. Detector count is retained boxes, not ground truth.

## Usage, latency, and configured cost

| Run | Attempts / calls | Input / output tokens | Known CNY subtotal | Full-run CNY estimate | Observed wall seconds |
|---|---:|---:|---:|---:|---:|
| primary_a | 200 / 200 | 2916054 / 74610 | unavailable | unavailable | 457.008 |
| primary_b | 200 / 200 | 2916158 / 75186 | unavailable | unavailable | 654.544 |
| cheap | 202 / 202 | 754035 / 41993 | 0.6035958 | 24.587474913 | 194.597 |

| Run | Workers | Median attempt seconds | Full-run serial attempt hours | Full-run wall hours at observed workers | Idealized full-run hours at 10 workers |
|---|---:|---:|---:|---:|---:|
| primary_a | 6 | 12.483 | 28.883 | 5.171 | 2.888 |
| primary_b | 4 | 12.542 | 29.223 | 7.406 | 2.922 |
| cheap | 6 | 5.477 | 13.039 | 2.202 | 1.304 |

The ten-worker estimate divides summed request latency by ten. It is not measured ten-worker throughput and excludes scheduling, throttling, long-tail, and startup overhead.

Offline feature pilot wall time: 362.0 seconds on CPU. Median PCP inference: 4.330 ms; detector pipeline: 1659.952 ms. A linear 8,147-frame projection at this observed throughput is 4.10 hours. It does not assume an acceleration backend or subtract future cache hits.

Smoke runs are excluded from all pilot schema and agreement denominators. Their experimental usage is itemized separately:

| Smoke run | Frames | Input / output tokens | Known CNY subtotal | Known SDK USD subtotal |
|---|---:|---:|---:|---:|
| cheap_smoke | 1 | 3732 / 220 | 0.0030312 | unavailable |
| primary_smoke | 1 | 14578 / 722 | unavailable | unavailable |

Pilot plus separately itemized smoke usage: 604 attempts / 604 calls. Known configured CNY subtotal: 0.6066270; complete all-call configured cost: unavailable. Full token and USD coverage is recorded in report.json.

The full-run projection covers 8,147 frames: 5,898 representatives plus 2,249 additional frames from shots longer than four seconds. Each run is projected independently by 8,147/200, including its observed retries; this is a planning estimate, not a billing quote. Pilot selection excludes OP/ED and low-information frames, so the full distribution differs.

Configured rates come from `config/model-pricing.yaml`; report.json includes its hash, exact per-call rate snapshots, raw usage, timing, sampling settings, and unpriced-call coverage. Text plus JPEG requests contain zero audio tokens; this explicit pricing assumption leaves raw metadata unchanged. Codex has no configured monetary rate. Missing prices remain unavailable; CNY and any SDK-reported USD remain separate. Cached and reasoning tokens are subsets and are not double-counted.

## Artifacts

- `../review.html`: all 200 source frames, detector overlays, PCP probabilities, and all three VLM outputs.
- `report.json`: metrics, decisions, source hashes, raw call metadata, and cost projections.
- `../validation.json`: pilot artifact integrity and the separate pilot-gate result.

No full tagging, retrieval relevance assessment, or production write is performed by this report.
