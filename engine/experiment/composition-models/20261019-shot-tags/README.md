# Shot composition tags: completed-data exploratory subset

Status: **exploratory subset and judging pool complete; full tagging stopped at the user’s request**. The selected model is `gpt-5.6-sol`, using `primary_a` as the reusable pilot cache. The delivered annotations cover **4,015 keyframes from 2,889 shots**, with 3,964 unique JPEG hashes. Full-gallery tagging is unfinished. The completed selection is strongly biased toward earlier episodes and supports no conclusion about full-gallery retrieval quality. This directory is a local, git-ignored experiment; production code, databases, indexes, and the source gallery were not changed.

The experiment uses `shot-composition-draft-v1` from `engine/design/shot-composition-schema.md`, copied into `prompt/schema-source.md`, and the pinned `shot-tag-v1` prompt. Five VLM groups describe camera, composition, subjects, frame, and content. Offline PCP composition probabilities and GroundingDINO person detections remain separate observations. No model output is treated as a relevance judgment.

## Source and sampling

No validated `*-shot-gallery-v1.1` directory was available, so the pilot uses validated `../20261018-shot-gallery` (v1): 5,898 shots, 12,447 keyframes, and 7,103 mapped Events over 13 MyGO episodes. `input_manifest.json` pins its validation record and SHA-256.

The 200-frame pilot samples representative frames excluding OP/ED and `low_information`. It covers all 104 populated episode × duration × internal-motion strata from 5,545 eligible frames. Duration buckets are `<1 s`, `1–4 s` inclusive, `>4–8 s`, and `>8 s`. The existing internal-motion flag measures pixel change and does not independently establish camera motion.

Sampling reserves four required anchors, guarantees one item per populated stratum, allocates the remaining items in proportion to remaining stratum capacity using largest remainders, and orders candidates with a pinned SHA-256 seed. `pilot/sampling.json` records the complete method, seed, counts, and selected manifest hash. There are 200 unique shots and image hashes, with 56/72/46/26 frames across duration buckets and 102 static / 98 internally moving frames.

The required anchors are `mygo-ep01-s00152-f2`, `mygo-ep07-s00056-f1`, `mygo-ep10-s00336-f2`, and `mygo-ep10-s00337-f2`. The requested Episode 10 interval [930300, 935600) ms spans two source shots, so both are included. The Episode 1 frame is already its shot representative; no representative override was needed. Anchor inclusion conveys no relevance label.

## Pilot runs and strict validation

`primary_a` and `primary_b` use the configured `gpt-5.6-sol` vision binding with low reasoning effort, as independent runs. `cheap` uses `doubao-seed-2-0-lite-260428` with temperature 0, top-p 1, and reasoning disabled. Codex sampling controls unavailable in its turn API are recorded as unavailable. Actual settings, requested and reported model identifiers, raw provider responses, validation errors, and all recorded usage remain under `raw/` and in the run metadata.

The runner validates the original returned JSON with the strict local schema. An invalid response permits one explicit correction request; API failures do not silently switch models. The report keeps first-response and final schema rates separate and uses all 200 frames as denominator, including failures. Invalid outputs are excluded from agreement pairs with coverage shown, never converted to `unknown`.

Six fields receive exact-agreement and unweighted Cohen's kappa measurements: shot size, vertical angle, primary composition, main-subject cell, main-subject scale, and bucketed person count. Count reporting uses 0, 1, 2, 3–5, and 6+; exact schema count 6 and literal `many` both enter the 6+ reporting bucket, while the original literal `many` continues to mean more than six people.

Both primary runs must reach final schema validity ≥98%, and self-agreement kappa must reach ≥0.6 for vertical angle and shot size. Cheap is eligible only if its schema validity reaches ≥98% and primary-a-versus-cheap kappa is no more than 0.1 below the corresponding primary self kappa for both fields. Higher cross-model kappa is allowed. Undefined kappa fails a gate check. A failed primary gate stops full tagging for review and a possible prompt revision.

## Offline features and visual review

`features.py` runs cached PCP and GroundingDINO models offline and records model hashes, revisions, preprocessing, thresholds, NMS, and timing. GroundingDINO uses prompt `person .`, box threshold 0.35, text threshold 0.25, and NMS IoU 0.5. Detector person boxes use normalized `[x0,y0,x1,y1,score]` coordinates after clipping and NMS; a zero count means no retained detection. PCP has nine native classes. Its native `curved` class maps explicitly to schema `curve` for canonical comparisons; confusion matrices preserve all 13 VLM rows and nine native PCP columns.

`pilot_report.py` verified matching frame IDs and image hashes, revalidated final tags, and wrote `pilot/report.json`, `pilot/report.md`, `review.html`, and `validation.json` with `stage: pilot`. The HTML contains all 200 local source images with inline SVG detector overlays, PCP probabilities, and all five tag groups from each of the three runs. It contains no relevance labeling controls. Node's syntax check passed for the generated script. Browser visual inspection was blocked by the browser's local-file URL policy; this limitation is recorded in validation.

## Usage and projection

The report retains raw usage and sampling metadata, latency, retry counts, token-field coverage, and exact applicable price snapshots from `config/model-pricing.yaml`. Missing cost remains unavailable. The repository has no configured Codex monetary rate; SDK-reported USD, if present, remains separate from estimated CNY. Pricing assumes zero audio tokens because requests contain text and one JPEG, with raw token metadata retained unchanged. Cached and reasoning counts are token subsets and are not added to total tokens again.

The original full-tagging target covers 8,147 frames: all 5,898 representatives plus 2,249 additional keyframes in shots longer than four seconds. Per-model projections scale observed pilot work, including retries, by 8,147/200. They are planning estimates; the stratified pilot excludes OP/ED and low-information frames and does not match every full-gallery distribution. The pilot gate passed and full tagging was launched, then explicitly stopped by the user. The projections remain historical planning estimates; they are not elapsed-time or cost measurements for the delivered subset.

## Reproduction

Run local analysis commands from the repository root with the existing environment. API runners require the source-specific credential environment: the actual launches used `uv run --no-sync --env-file .env.local`, which loads configuration without printing secret values. Codex also uses the existing local authenticated runtime. Offline feature extraction uses its cached-model environment, shown below.

```sh
.venv/bin/python engine/experiment/composition-models/20261019-shot-tags/prepare_pilot.py
UV_CACHE_DIR=/private/tmp/anivag-shot-tags-uv uv run --no-sync --env-file .env.local python engine/experiment/composition-models/20261019-shot-tags/tag_vlm.py --run primary_a --model primary --workers 6
UV_CACHE_DIR=/private/tmp/anivag-shot-tags-uv uv run --no-sync --env-file .env.local python engine/experiment/composition-models/20261019-shot-tags/tag_vlm.py --run primary_b --model primary --workers 4
UV_CACHE_DIR=/private/tmp/anivag-shot-tags-uv uv run --no-sync --env-file .env.local python engine/experiment/composition-models/20261019-shot-tags/tag_vlm.py --run cheap --model cheap --workers 6
.venv/bin/python engine/experiment/composition-models/20261019-shot-tags/pilot_report.py
```

The historical ten-worker full launch was:

```sh
UV_CACHE_DIR=/private/tmp/anivag-shot-tags-uv uv run --no-sync --env-file .env.local python engine/experiment/composition-models/20261019-shot-tags/tag_vlm.py --run full --model primary --manifest engine/experiment/composition-models/20261019-shot-tags/full/missing.jsonl --workers 10
```

That run was stopped; the command is recorded for provenance and was not restarted. Ten interrupted raw requests remain unresolved and unconsumed. No interrupted call was replayed and no new call was made after the stop. Preserve the original full manifests, frozen raw files, and historical `pilot/full-summary.json` (`status: running` at its last progress snapshot); the authoritative stopped-run receipts are [full/interruption.json](full/interruption.json) and [full/audit-final.json](full/audit-final.json). The explicitly authorized selection and limitation are pinned by [scope_change.json](scope_change.json) and [exploration/manifest.json](exploration/manifest.json).

The completed offline MPS feature launch used the cached-model virtual environment:

```sh
.cache/composition-models/.venv/bin/python engine/experiment/composition-models/20261019-shot-tags/features.py --input engine/experiment/composition-models/20261019-shot-tags/full/frames.jsonl --output engine/experiment/composition-models/20261019-shot-tags/full/features.jsonl --device mps --threads 6 > engine/experiment/composition-models/20261019-shot-tags/full/features.log 2>&1
```

This feature run exited successfully; its output and audit are retained, so no rerun is needed.

The local delivery check performs no inference or scoring:

```sh
.venv/bin/python engine/experiment/composition-models/20261019-shot-tags/validate_delivery.py
```

It first archives the original pilot validation byte-for-byte as `pilot/validation.json`, then writes the delivery-stage `validation.json`. It requires the selected frame universe, strict tags, source JPEG hashes, raw provenance, aggregate consistency, all required anchors, query/pool coverage, completed feature audit, interrupted VLM integrity audit, and independent ranking audit. Missing evidence cannot produce a passed result. HTML checks cover local paths, embedded data, and JavaScript syntax; browser rendering remains unverified because local-file preview was prohibited by browser URL policy.

## Scoring user judgments

`score.py` is prepared but has not been executed. It reads only the saved pool's current query/method rankings, checks their recorded hashes, and accepts user-exported labels. Category and layout are evaluated separately. Pool aliases share one user judgment, while every method's original ranking positions and cutoffs remain unchanged. Repeated aliases are not removed and lower-ranked shots never refill the original top 5, 10, or 50; otherwise a shot outside the judged top-10 union could enter a top-10 metric.

- **nDCG@10:** each cluster contributes relevance gain only at its first occurrence in the original top 10; later occurrences contribute zero gain at their original positions. Gain is `2^label - 1`, discounted by `log2(rank + 1)`. The ideal denominator uses the ten highest labels among distinct judged candidates in that query's pool. Queries with no judged positives or no judged top-10 positions have undefined nDCG and are omitted from its mean.
- **P@5:** count distinct positively judged clusters (`label >= 1`) in the original top five, then divide by five or the available ranking length if shorter. The supplemental `judged_only_precision_at_5` uses the same distinct-positive numerator divided by the number of judged original slots; repeated judged aliases still occupy denominator slots. This is intentionally not precision over a compacted list of unique clusters.
- **Content leakage:** apply the shared same-location flag to every original top-10 slot, including repeated aliases. Report both positive flags divided by all available slots and positive flags divided by judged slots. With partial flags, the first is an observed lower bound.
- **Pooled recall@50:** count distinct judged-positive pool clusters reached within the original top 50, divided by all judged-positive clusters in that query's pool. It is not corpus-wide recall.

Unjudged labels stay null; provisional nDCG/P@5 calculations assign their positions no observed gain and report coverage explicitly. A fully judged top 10 means every original slot's cluster has a label; it does not imply that the entire pool or corpus has been judged. Per-method reports include the number of queries contributing to each mean. The latest timestamp wins when exports contain multiple records for a pair, including an explicit all-null record produced by clearing previous answers.

The pool uses one consensus display frame per collapsed candidate. Its judgment therefore represents the candidate's aliases and other method-selected frames as well; those alternative frames remain in the separate audit. No assistant-created labels are used for scoring.

## Results

All three runs finished with 200/200 schema-valid final outputs. Both primary runs were valid on the first attempt for every frame. The cheap run was first-response valid on 198/200 frames (99%) and needed two explicit validation retries. Each comparison below uses all 200 valid pairs.

| Field | Primary self exact agreement | Primary self κ | Primary A vs cheap κ |
|---|---:|---:|---:|
| Shot size | 81.5% | 0.784 | 0.497 |
| Vertical angle | 95.5% | 0.888 | 0.673 |
| Primary composition | 81.0% | 0.770 | 0.394 |
| Main-subject cell | 88.5% | 0.747 | 0.573 |
| Main-subject scale | 80.5% | 0.682 | 0.522 |
| Person-count bucket | 93.0% | 0.898 | 0.891 |

The primary model passes both ≥0.6 kappa gates. The cheap model misses its comparison thresholds: size κ must reach 0.684 and angle κ must reach 0.788, but it reaches 0.497 and 0.673. Therefore the selected model remains `gpt-5.6-sol`; these measurements establish repeatability and agreement, not accuracy.

Primary A took 457.008 seconds with six workers and used 2,916,054 input tokens and 74,610 output tokens, including recorded cache and reasoning subsets. Primary B took 654.544 seconds with four workers. Linear projection from primary A gives approximately 5.17 hours at its observed six-worker throughput, or an idealized 2.89 hours at ten workers from summed request time. The ten-worker estimate excludes scheduling, throttling, long-tail, and startup overhead. The repository provides no monetary rate for Codex, so primary cost remains unavailable.

Cheap pilot configured cost is CNY 0.6035958 across 202 attempts; its separate smoke call adds CNY 0.0030312. Its 8,147-frame projection is CNY 24.587474913, but it was not selected. Across 600 pilot observations and two separately itemized smoke observations, there were 604 attempts/calls, 6,604,557 input tokens, and 192,731 output tokens. The known priced subtotal is CNY 0.6066270; total all-provider monetary cost is unavailable because primary calls are unpriced. No missing price is represented as zero.

Offline CPU features completed for all 200 frames in 361.99 seconds, with median PCP time 4.33 ms and detector pipeline time 1,659.95 ms. Raw metadata, exact configured rate snapshots, all three 13×9 PCP confusion matrices, detector count comparisons, and per-model time/cost projections are in [pilot/report.json](pilot/report.json) and [pilot/report.md](pilot/report.md). [review.html](review.html) is the local 200-frame review page. The original pilot artifact-integrity receipt is preserved as [pilot/validation.json](pilot/validation.json); [validation.json](validation.json) records the final delivery audit separately from the pilot gate.

## Completed exploratory annotations

The interrupted full run produced 3,765 valid results, all first-pass valid, in addition to the 200 selected primary pilot results. The 3,965 completed unique image hashes cover 4,020 original target rows. Selection retains every completed frame whose shot representative is also complete: 4,015 frames, 2,889 shots, and 3,964 unique hashes. Five rows sharing one image hash are excluded because their shot representatives were unfinished. The original target was 8,147 frames over 5,898 shots, so 4,132 originally requested frames and 3,009 gallery shots remain outside this selection. No incomplete response was parsed, repaired, or relabeled.

| Episode | Selected shots | Selected keyframes |
|---|---:|---:|
| 1 | 393 | 586 |
| 2 | 423 | 587 |
| 3 | 311 | 503 |
| 4 | 455 | 620 |
| 5 | 418 | 588 |
| 6 | 483 | 650 |
| 7 | 280 | 355 |
| 8 | 19 | 19 |
| 9 | 24 | 24 |
| 10 | 26 | 26 |
| 11 | 17 | 17 |
| 12 | 22 | 22 |
| 13 | 18 | 18 |
| **Total** | **2,889** | **4,015** |

Dispatch followed episode order. Episodes 1–7 account for 95.64% of selected shots and 96.86% of selected keyframes; later episodes mainly contribute the stratified pilot and required anchors. All methods use this same annotated universe, and untagged gallery shots are not ranked. Selecting queries across all episodes does not remove the candidate-pool bias.

All 4,015 selected tags pass the strict schema. [keyframe_tags.jsonl](keyframe_tags.jsonl) contains all five VLM groups with original raw-response and model provenance, plus PCP and detector observations. [shot_tags.jsonl](shot_tags.jsonl) uses each shot’s representative tags and records disagreements across available valid frames. There are 743 shots with observed differences (25.72% of selected shots), 2,140 with fewer than two tagged frames, and six with no observed difference. Missing originally requested long-shot frames make `variation_incomplete` true for 37 shots. An empty difference list is not evidence of temporal stability.

[distributions.json](distributions.json) contains both all-keyframe and one-representative-per-shot histograms; [distributions.html](distributions.html) displays the all-keyframe histograms. All-keyframe unknown rates are 2.24% for shot size, 2.71% for vertical angle, 1.79% for roll, and 4.58% for setting. VLM/detector counts agree on 3,101 frames; the detector is higher on 552 and VLM higher on 362. These are model observations and disagreement measurements, not accuracy estimates.

Offline MPS feature extraction completed for the entire original 8,147-frame target, independently of the stopped VLM run: 8,092 unique image hashes, 4,845.34 seconds elapsed (80.76 minutes), median PCP pipeline 5.68 ms, and detector pipeline 544.63 ms. [full/features-validation.json](full/features-validation.json) passed source-image/cache/contract, class-order/probability, NMS, and box-geometry checks for every row. Only features for the annotated selection enter ranking.

The stopped VLM run’s last progress snapshot records 4,963.906 seconds (82.73 minutes); the exact termination timestamp was not recorded, so this is not asserted as precise stop-to-start wall time. Its 3,765 completed calls reported 54,887,348 input tokens (43,908,480 cached) and 1,385,018 output tokens (626,014 reasoning), totaling 56,272,366. Across completed pilot, smoke, and full calls, recorded totals are 61,491,905 input, 1,577,749 output, and 63,069,654 total tokens. Cached and reasoning subsets are not added again. The ten interrupted attempts have no terminal usage, so reported totals omit their unknown usage and cost. The known cheap-model configured estimate remains CNY 0.6066270, with pilot and smoke separated above; Codex and total experiment monetary cost remain unavailable.

## Query selection, rankings, and user judging

[queries.json](queries.json) and [queries.html](queries.html) contain 25 queries and ten disjoint alternates, all 35 from distinct reuse groups. Deterministic tag-coverage selection uses 2,685 eligible story representatives and includes the Episode 1 reference. It excludes OP/ED and low-information query frames, covers all 13 episodes and all 14 requested categories, and checks extra story frames before declaring any category unavailable. Every category was available among representatives, so no frame override was needed. Reasons state observed tag coverage and do not assign relevance.

Six methods produce 150 shot rankings (25 queries × six methods). The shared annotated input has 3,693 valid story, non-OP/ED frames before query-specific exclusions. Candidate filtering differs from stricter query sampling: low-information frames are not separately excluded if the VLM labeled them story.

| Method | Ranking components | Temporal exclusion |
|---|---|---|
| M0 | Doubao image cosine | None |
| M1 | Doubao image cosine | Same episode, shot-midpoint distance ≤60 s |
| M2 | Doubao + tags | Same as M1 |
| M3 | Doubao + tags + PCP | Same as M1 |
| M4 | Doubao + tags + person-box layout | Same as M1 |
| M5 | Tags + PCP + person-box layout | Same as M1 |

All methods exclude the query shot and its reuse group. M2–M5 use equal-weight reciprocal-rank fusion with `k=60`, component competition ranks for tied raw scores, and deterministic final ties. Fusion happens per frame before retaining the best frame per shot. This gives shots with more selected frames more opportunities to score highly. A missing layout component contributes no RRF term and can lower the fused score. M5 excludes Doubao from its ranking score, but existing reuse groups and later pool collapse still use prior Doubao evidence. Optional M6 was not run. [rankings/methods.json](rankings/methods.json) records exact tag, PCP, layout, missing-value, and exclusion definitions.

The union of each method’s original top ten contains 735 query–shot pairs. Complete-link duplicate collapse yields **718 blind judging candidates**, 19–37 per query. Collapse requires same-episode proximity within five seconds and pairwise best-frame Doubao cosine ≥0.97 for every pair in a cluster. [pool.json](pool.json) and [judge.html](judge.html) hide methods, ranks, scores, and tag rationale. [method_components.json](method_components.json) and per-method ranking/component files retain the separate audit trail. [rankings/validation.json](rankings/validation.json) independently passed all ranking, eligibility, tie, pool, and blinded-page checks.

The mandatory reference’s recorded ranks are sanity observations only; no candidate is declared relevant:

| Method | Ep7 s00056 | Ep10 s00336 | Ep10 s00337 |
|---|---:|---:|---:|
| M0 | 22 | 7 | 69 |
| M1 | 14 | 6 | 59 |
| M2 | 4 | 5 | 69 |
| M3 | 4 | 5 | 121 |
| M4 | 2 | 6 | 113 |
| M5 | 3 | 48 | 558 |

Open the local `judge.html` for human review. Rate category and layout separately and answer both same-location and duplicate flags; optional notes are retained. Progress distinguishes pairs rated from pairs with both flags answered, and “Next unjudged” finds any missing rating or flag. Answers are saved in browser local storage; export JSONL to preserve a portable copy. Clearing a previously answered pair exports an explicit null record so later scoring can remove stale labels. The experiment has generated no user judgments and has not run `score.py`. Ranking comparisons and retrieval-quality conclusions await human labels and remain limited to this episode-biased exploratory pool.

After saving a human-reviewed export as `engine/experiment/composition-models/20261019-shot-tags/judgments.jsonl`, the user can compute the separate category/layout metrics with:

```sh
.venv/bin/python engine/experiment/composition-models/20261019-shot-tags/score.py engine/experiment/composition-models/20261019-shot-tags/judgments.jsonl --output engine/experiment/composition-models/20261019-shot-tags/scores.json
```

This command has not been run. It accepts the current pool’s exported labels, preserves the original ranking cutoffs, reports partial judgment coverage, and carries the exploratory scope and episode-bias limitation into its report.
