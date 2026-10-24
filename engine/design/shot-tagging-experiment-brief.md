# Brief: Shot composition tagging and ranking ablation

> Archive status (2026-10-24): the experiment directories named below have been retired. Composition and shot-tags data are kept in a local Git-ignored ZIP; see the [summary and recovery instructions](../../docs/dev/design-experiment-cleanup.md#本地归档与恢复). Paths and commands below describe the historical experiment, not an active run request.

Hand this file to the experiment agent as its task. Date: 2026-10-19.

---

## Task

Tag every shot in the MyGO shot gallery with composition features defined in [shot-composition-schema.md](shot-composition-schema.md), then run a ranking ablation and prepare a **blind** judging pool for the user. You do **not** judge relevance yourself.

Work in a new directory: `engine/experiment/composition-models/20261019-shot-tags/`. Write the README in English.

## Ground rules

- No production code, config, database or index changes. No commits, no branch switches. All outputs stay in the experiment directory (git-ignored).
- **Do not label relevance.** Pools and the judging page are for the user. Never present VLM tags as ground truth or as accuracy.
- Keep raw VLM responses next to parsed records. Record model, prompt version, temperature/reasoning settings, timestamps, latency and token use per call.
- Reuse existing code and caches: gallery embeddings, PCP and Grounding DINO set-up from `20261017-first-pass/` and `engine/experiment/grounding-dino/`, models in `.cache/composition-models/`. Don't download new model weights except Depth Anything V2 Small in phase 4 (if it isn't cached, record its source and revision).
- Validate every parsed record against the schema enums. Invalid output is retried once with the validation error, then stored as `invalid` — never silently repaired.

## Inputs

- Gallery: use `engine/experiment/composition-models/<date>-shot-gallery-v1.1/` if it exists and its validation passed; otherwise `20261018-shot-gallery/`. If you use v1, say so in the README, and in phase 5 collapse adjacent near-duplicates (same episode, within 5 s, Doubao cosine ≥ 0.97) so concert-lighting false cuts don't flood the pools.
- Frames: `gallery.jsonl` (`frame_path`, `is_representative`, `is_op_ed`, `reuse_group_id`, `event_ids`, …), embeddings `embeddings.npy` + `embedding_rows.jsonl`.
- VLM: the project's configured `models.vision` binding (`gpt-5.6-sol`) through the existing gateway model factory. Candidate cheaper model: `doubao-seed-2-0-lite-260428` on the `ark_default` source.

## Phase 1 — Pilot (stop-gate)

1. Pick **200 representative keyframes**, stratified by episode, shot duration bucket (<1 s, 1–4 s, 4–8 s, >8 s) and `internal_motion`, excluding OP/ED and low-information frames. Always include `mygo-ep01-s00152-f2`, `mygo-ep07-s00056-f1` and the Ep10 shot covering 15:30.3–15:35.6.
2. Tag them with the VLM prompt below, **twice** with the primary model, and once with the cheaper model.
3. Run Grounding DINO (prompt `person`, existing thresholds, **then NMS at IoU 0.5**) and PCP on the same 200.
4. Report:
   - Schema-valid rate per model.
   - **Self-consistency** between the two primary runs, per field: exact agreement and Cohen's κ for `shot_size`, `vertical_angle`, `composition.primary`, `main_subject_cell`, `main_subject_scale`, `person_count` bucket (0, 1, 2, 3–5, many).
   - Cross-model agreement (primary vs cheaper), same fields.
   - VLM vs detector person-count agreement, and the VLM `composition.primary` vs PCP argmax confusion table.
   - Latency, tokens and estimated cost/time for the full gallery for each model.
   - A **review sheet**: one HTML page or contact sheet showing each pilot frame with its tags and detector boxes overlaid, so the user can eyeball errors.
5. **Gate.** Continue to phase 2 only if: schema-valid ≥ 98%, and κ ≥ 0.6 on `vertical_angle` and `shot_size`. Choose the cheaper model only if its agreement with the primary model on those two fields is within 0.1 κ of the primary's self-consistency. If the gate fails, **stop**, report which fields are unstable, and propose prompt changes (bump `prompt_version`). These are consistency checks, not accuracy.

## Phase 2 — Full tagging

1. VLM-tag **every representative keyframe** (one per shot). Also tag extra keyframes of shots longer than 4 s, so `varies_within_shot` can be computed. Skip extra keyframes of shots under 1 s.
2. Detector + NMS and PCP on the same frames.
3. Write `keyframe_tags.jsonl` (one schema record per keyframe) and `shot_tags.jsonl` (shot aggregates, per schema §6). Cache by image SHA-256 and `prompt_version`, so reruns only call for missing rows.
4. Report tag distributions (histograms per enum), invalid/unknown rates, `count_agreement` breakdown, the share of shots with `varies_within_shot` non-empty, and actual time and cost.

## Phase 3 — Query set

Select **25 query shots** from STORY frames, stratified by the new tags so the set covers: overhead, high, low angle, extreme close-up/close-up, long/extreme-long with a tiny figure, two-person, group, subject left vs right, symmetric, diagonal, frame-within-frame, large negative space. Must include `mygo-ep01-s00152`. List the 25 with keyframe thumbnails and the reason for each, plus 10 alternates the user can swap in.

## Phase 4 — Ranking methods

For each query, rank all gallery shots (best keyframe per shot), excluding the query's `reuse_group_id`, OP/ED, and `frame.kind != story`:

| ID | Method |
|---|---|
| M0 | Doubao cosine (baseline) |
| M1 | M0 + same-scene exclusion: drop same-episode shots within ±60 s of the query |
| M2 | M1 fused with a **tag-match score** (below) |
| M3 | M2 + PCP cosine |
| M4 | M2 + detector layout distance (Hungarian matching on person boxes: centre distance + log-area difference + unmatched-box penalty) |
| M5 | Tags + PCP + detector layout only, **no Doubao** (content-free) |
| M6 *(optional)* | M2 + depth/saliency grid similarity (Depth Anything V2 Small; subtitle band masked) |

Fusion: **reciprocal rank fusion (k = 60)** over the component rankings. Don't use raw-score weighting; the score scales aren't calibrated.

Tag-match score (equal weights in v1; record them): ordinal similarity for `vertical_angle` (overhead=0 … worm=4) and `shot_size` (1 − d/max); Jaccard over {primary} ∪ secondary for composition; person-count bucket (exact 1, adjacent 0.5, else 0); `main_subject_cell` 1 − Manhattan/4; `main_subject_scale` ordinal; overlap of `negative_space.sides`. Fields marked `unknown` contribute nothing (not zero, not one).

Also record, per method and query, where the known candidates land (Ep7 `s00056`, Ep10 15:30 shot for the Ep1 query), as a sanity check only.

## Phase 5 — Blind judging pool and page

1. For each query, pool the **top 10 shots from every method** (M0–M5, plus M6 if run), collapsed by shot and near-duplicate neighbours (see Inputs). Typical pool: ~30 shots per query.
2. Build an **offline static HTML judging page** (no network calls, images referenced locally or embedded):
   - Shows the query keyframe large, each candidate keyframe beside it, **in random order with method identity hidden**.
   - Keyboard input per candidate: **category match** 0/1/2, **layout match** 0/1/2, **same location/scene** flag, **duplicate/reused shot** flag, optional note.
   - Saves progress in `localStorage` (wrapped in try/catch) and exports `judgments.jsonl` (query_shot_id, candidate_shot_id, labels, timestamp).
   - Short on-page definitions: *category* = the same organising pattern (angle, shot size, composition type) regardless of content; *layout* = people/subject in similar positions, sizes and counts.
3. Write `score.py`, which reads `judgments.jsonl` and reports per method: nDCG@10 for category and layout separately, P@5, **content leakage** (share of top 10 flagged same-location), and pooled recall@50 (share of judged positives ≥1 retrieved in the top 50). It must run on partial judgments and report how many were used.

Don't run `score.py` on any labels you made yourself; there should be none.

## Deliverables

```text
20261019-shot-tags/
├── README.md                 method, models, prompt version, gates, results, limitations
├── prompt/shot-tag-v1.md     exact VLM prompt used (copy of the one below, plus any revisions)
├── pilot/                    pilot tags (both runs + cheaper model), agreement report, review sheet
├── keyframe_tags.jsonl       full per-keyframe records
├── shot_tags.jsonl           shot aggregates
├── raw/                      raw VLM responses keyed by sha256 + prompt_version
├── distributions.json/html   tag histograms
├── queries.json              25 queries + 10 alternates with reasons
├── rankings/                 per method per query, full ranked lists with component ranks
├── pool.json                 blinded judging pool
├── judge.html                offline judging page
├── score.py                  metrics from judgments.jsonl
└── validation.json           schema validity, counts, hashes, coverage
```

Final report to the user: gate results, chosen model and why, full-run cost/time, tag distributions, where the known candidates landed per method, and the exact next step (the user judges with `judge.html`, then runs `score.py`).

---

## VLM prompt `shot-tag-v1`

Send one image per call. Use the lowest reasoning setting that passes the gate and deterministic sampling if the model supports it. Ask for JSON only.

### System

```text
You are a cinematography annotator for anime frames. You describe how a single frame is
composed — camera, framing, layout — not what the story is. Report only what is visible.
Ignore burned-in subtitles and on-screen credits when judging composition; do not count
them as subjects, lines or empty space. Never name characters. If you cannot tell, use
"unknown" or mark the group "ambiguous" instead of guessing. Output one JSON object that
follows the schema exactly, with no commentary.
```

### User

```text
Annotate this anime frame (1280×720). Use exactly these fields and values.

camera:
  shot_size: one of extreme_close_up | close_up | medium_close_up | medium | medium_long |
             full | long | extreme_long | insert_detail | unknown
    Judge from the MAIN person's visible body: eyes/part of face = extreme_close_up;
    head fills frame = close_up; head+shoulders = medium_close_up; waist up = medium;
    knees up = medium_long; whole body, little margin = full; whole body small in a visible
    environment = long; people tiny or absent and environment dominates = extreme_long;
    a hand/object/prop fills the frame = insert_detail. With no person, judge by the main
    subject's share of the frame.
  vertical_angle: overhead | high | eye_level | low | worm | unknown
    overhead = looking nearly straight down (floor/tabletop fills frame);
    high = clearly looking down; eye_level = roughly level with the subject;
    low = looking up; worm = extreme upward from the ground.
  roll: level | dutch | unknown        (dutch = horizon visibly tilted on purpose)
  framing_device: list, 0–2 of over_the_shoulder | pov | through_frame | reflection |
                  screen_in_screen      (through_frame = seen through a window, door, gap)
  clarity: clear | ambiguous

composition:
  primary: one of center | rule_of_thirds | symmetric | horizontal | vertical | diagonal |
           triangle | curve | pattern | vanishing_point | frame_within_frame | fill_frame |
           none_clear
  secondary: list, 0–2 from the same values, excluding primary
  dominant_lines: list, 0–2 of horizontal | vertical | diagonal_rising | diagonal_falling |
                  converging | curved
  depth_structure: flat | layered | deep
  foreground_element: true | false     (something in the near frame partly blocks the view)
  negative_space: {"amount": none | some | large,
                   "sides": list of left | right | top | bottom where the large empty area is}
  clarity: clear | ambiguous

subjects:
  main_subject_kind: person | group | object | environment | text
  main_subject_cell: TL | TC | TR | ML | MC | MR | BL | BC | BR
    (3×3 grid; the cell containing the main subject's visual centre)
  main_subject_scale: tiny (<2% of frame) | small (2–10%) | medium (10–30%) |
                      large (30–60%) | dominant (>60%)
  person_count: integer 0–6 of clearly visible people, or "many" if more than 6
  partial_people: integer of people visible only as hands, legs or cut-off silhouettes
  persons: up to 4, largest first, each
    {"cell": 3×3 cell, "facing": camera | left | right | away | three_quarter_left |
     three_quarter_right, "visible_extent": face | bust | waist_up | knee_up | full_body | partial}
  arrangement: single | side_by_side | front_back | facing_each_other | back_to_back |
               row | scattered | none
  clarity: clear | ambiguous

frame:
  kind: story | title_card | credits | text_screen | black_or_flash | transition
  style_flags: list of chibi | monochrome_or_filtered | split_screen | heavy_blur |
               screen_content | speed_lines
  burned_subtitles: true | false

content:
  setting: interior | exterior | stage | vehicle | abstract | unknown
  location_hint: at most 6 lowercase English words describing the place, no names

Return:
{"camera": {...}, "composition": {...}, "subjects": {...}, "frame": {...}, "content": {...}}
```
