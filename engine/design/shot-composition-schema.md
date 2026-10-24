# Shot Composition Schema (draft v1)

> Archive status (2026-10-24): the experiment directories named below have been retired. Composition and shot-tags data are kept in a local Git-ignored ZIP; see the [summary and recovery instructions](../../docs/dev/design-experiment-cleanup.md#本地归档与恢复). Paths and commands below describe the historical experiment, not an active run request.

Date: 2026-10-19. Status: **draft for experiment** — not a production contract. The tagging experiment ([brief](shot-tagging-experiment-brief.md)) produces records in this shape so that the evaluation can decide which fields earn a place in the production `video_shot_feature` table and the shots ES index.

## 1. Purpose and boundaries

The shot gallery (`engine/experiment/composition-models/20261018-shot-gallery/`) gives every shot an identity, keyframes, reuse group, OP/ED flags and a Doubao content vector. It has **no composition information**. This schema adds it.

- **Composition first.** Fields describe *how the frame is organised*: camera angle, shot size, composition category, where people are, how big they are, empty space, depth and lines.
- **Content only for exclusion.** `setting` and `location_hint` exist so ranking can push down "same room, different angle". They never add to a composition score.
- **Observation, not interpretation.** No emotion, story or character-name fields. Moments (funny/hype/tearjerking) are a later, separate layer.
- **Every field records its source.** A VLM tag, a detector count and a PCP distribution are different kinds of evidence and are never merged into one number without saying so.

## 2. Units

| Unit | Identity | Tags |
|---|---|---|
| Keyframe | `frame_id` (from gallery) | **All tags are produced per keyframe.** Composition can change inside a long or moving shot. |
| Shot | `shot_id` (from gallery) | Aggregate: the representative keyframe's tags, plus `varies_within_shot` listing fields that differ between its keyframes. |

Query semantics: a shot **matches a filter if any of its keyframes matches**, and is **ranked by its best-scoring keyframe**. The returned evidence names that keyframe and its timestamp.

## 3. Field groups

Legend — **Use**: `F` hard filter for explicit query conditions · `S` soft ranking signal · `X` exclusion / content penalty only · `Q` quality gate (drop or flag rows).

### 3.1 Camera (VLM)

| Field | Type | Values | Use |
|---|---|---|---|
| `camera.shot_size` | enum | `extreme_close_up`, `close_up`, `medium_close_up`, `medium`, `medium_long`, `full`, `long`, `extreme_long`, `insert_detail`, `unknown` | F, S |
| `camera.vertical_angle` | enum | `overhead`, `high`, `eye_level`, `low`, `worm`, `unknown` | F, S |
| `camera.roll` | enum | `level`, `dutch`, `unknown` | F, S |
| `camera.framing_device` | enum[] (0–2) | `over_the_shoulder`, `pov`, `through_frame` (window, door, gap), `reflection`, `screen_in_screen` | F, S |
| `camera.clarity` | enum | `clear`, `ambiguous` — the VLM's own judgment for this group | — |

Definitions (written into the VLM prompt):

- **Shot size** is judged from the main person's visible body: eyes/part of face → `extreme_close_up`; head fills frame → `close_up`; head and shoulders → `medium_close_up`; waist up → `medium`; knees up → `medium_long`; whole body with little margin → `full`; whole body small in a visible environment → `long`; people tiny or absent, environment dominates → `extreme_long`. A hand, object or prop filling the frame → `insert_detail`. With no person, judge from the main subject's share of the frame.
- **Vertical angle**: camera looks nearly straight down, floor/tabletop fills the frame → `overhead`; clearly looking down, horizon/floor tilted away → `high`; roughly level with the subject's eyes → `eye_level`; looking up → `low`; extreme upward, from ground → `worm`.

### 3.2 Composition (VLM + PCP)

| Field | Type | Values | Use |
|---|---|---|---|
| `composition.primary` | enum | label scheme `shot-comp-labels-v1` (below) | F, S |
| `composition.secondary` | enum[] (0–2) | same scheme, excluding `primary` | S |
| `composition.dominant_lines` | enum[] (0–2) | `horizontal`, `vertical`, `diagonal_rising`, `diagonal_falling`, `converging`, `curved` | S |
| `composition.depth_structure` | enum | `flat`, `layered` (clear foreground/background), `deep` (strong recession) | S |
| `composition.foreground_element` | bool | an object or person partly blocks the near frame | S |
| `composition.negative_space` | object | `amount`: `none`/`some`/`large`; `sides`: subset of `left`, `right`, `top`, `bottom` | F (Q14, Q38), S |
| `composition.clarity` | enum | `clear`, `ambiguous` | — |
| `pcp.probs` | float[9] | KU-PCP softmax over the 9 PCP classes, from the existing PCP model | S |

`shot-comp-labels-v1` = the 9 KU-PCP classes so VLM and PCP can be compared directly — `center`, `rule_of_thirds`, `symmetric`, `horizontal`, `vertical`, `diagonal`, `triangle`, `curve`, `pattern` — plus `vanishing_point`, `frame_within_frame`, `fill_frame`, `none_clear`. Product parent classes (e.g. one coarse "diagonal") are a later mapping, not part of v1.

### 3.3 People and main subject

VLM-observed:

| Field | Type | Values | Use |
|---|---|---|---|
| `subjects.main_subject_kind` | enum | `person`, `group`, `object`, `environment`, `text` | S |
| `subjects.main_subject_cell` | enum | 3×3 cell `TL TC TR ML MC MR BL BC BR` | F, S |
| `subjects.main_subject_scale` | enum | `tiny` (<2% of frame), `small` (2–10%), `medium` (10–30%), `large` (30–60%), `dominant` (>60%) | F, S |
| `subjects.person_count` | int or `"many"` | exact up to 6 visible people; `"many"` beyond | F, S |
| `subjects.partial_people` | int | people visible only as hands, legs or cut-off silhouettes | S |
| `subjects.persons[]` | list (≤4, largest first) | `cell`, `facing` (`camera`, `left`, `right`, `away`, `three_quarter_left`, `three_quarter_right`), `visible_extent` (`face`, `bust`, `waist_up`, `knee_up`, `full_body`, `partial`) | S |
| `subjects.arrangement` | enum | `single`, `side_by_side`, `front_back`, `facing_each_other`, `back_to_back`, `row`, `scattered`, `none` | F, S |
| `subjects.clarity` | enum | `clear`, `ambiguous` | — |

Detector-measured (existing Grounding DINO, **with NMS**, IoU 0.5):

| Field | Type | Notes | Use |
|---|---|---|---|
| `detector.person_boxes` | list of `[x0,y0,x1,y1,score]` | normalised to the full frame, 0–1 | S (layout distance) |
| `detector.person_count` | int | count after NMS | S |
| `detector.largest_person_area` | float | share of frame, 0–1 | S |
| `detector.people_centroid` | `[x,y]` or null | area-weighted centre of person boxes | S |
| `detector.count_agreement` | enum | `agree`, `vlm_higher`, `detector_higher` — vs `subjects.person_count` | Q |

**`person_count` is never 0 just because the detector found nothing.** If the VLM sees people and the detector does not, the row keeps both values and `count_agreement = vlm_higher`. Filters on person count use the VLM value and treat disagreement as uncertain.

### 3.4 Frame quality and kind (VLM)

| Field | Type | Values | Use |
|---|---|---|---|
| `frame.kind` | enum | `story`, `title_card`, `credits`, `text_screen`, `black_or_flash`, `transition` | Q |
| `frame.style_flags` | enum[] | `chibi`, `monochrome_or_filtered` (flashback tint), `split_screen`, `heavy_blur`, `screen_content` (phone/TV fills frame), `speed_lines` | Q, S |
| `frame.burned_subtitles` | bool | fansub subtitles present — ignored for every composition judgment | — |

### 3.5 Content, for exclusion only (VLM)

| Field | Type | Values | Use |
|---|---|---|---|
| `content.setting` | enum | `interior`, `exterior`, `stage`, `vehicle`, `abstract`, `unknown` | X |
| `content.location_hint` | string | ≤6 lowercase English words, e.g. `bedroom with grey sofa`, `school rooftop`. No character names. | X |

### 3.6 Optional structure grids (phase 2 of the experiment)

| Field | Type | Notes | Use |
|---|---|---|---|
| `structure.depth_grid` | ref to `.npy` 16×16 | Depth Anything V2 Small, min-max normalised per frame | S |
| `structure.saliency_grid` | ref to `.npy` 16×16 | any offline saliency model; record which | S |

Both computed with the bottom subtitle band (lowest 15% of height) masked out.

## 4. Provenance and uncertainty rules

- Each group stores `source` (`vlm:<model>`, `detector:grounding-dino-tiny@<rev>`, `pcp:<weights sha>`), `prompt_version` or model revision, and run ID.
- `unknown` means "looked, could not tell". A missing group means "not computed". These are never interchangeable.
- VLM `clarity` is the only uncertainty the VLM reports. No invented 0–1 confidences.
- Raw VLM responses are kept beside the parsed record so a schema change can re-parse without re-calling.

## 5. Example record (one keyframe)

```json
{
  "schema_version": "shot-composition-draft-v1",
  "frame_id": "mygo-ep01-s00152-f2",
  "shot_id": "mygo-ep01-s00152",
  "frame_timestamp_ms": 542125,
  "camera": {"shot_size": "long", "vertical_angle": "overhead", "roll": "level",
             "framing_device": [], "clarity": "clear"},
  "composition": {"primary": "diagonal", "secondary": ["rule_of_thirds"],
                  "dominant_lines": ["diagonal_falling"], "depth_structure": "flat",
                  "foreground_element": false,
                  "negative_space": {"amount": "large", "sides": ["left", "top"]},
                  "clarity": "clear"},
  "pcp": {"probs": [0.03, 0.11, 0.02, 0.05, 0.04, 0.61, 0.06, 0.03, 0.05]},
  "subjects": {"main_subject_kind": "environment", "main_subject_cell": "BR",
               "main_subject_scale": "small", "person_count": 1, "partial_people": 0,
               "persons": [{"cell": "BR", "facing": "away", "visible_extent": "full_body"}],
               "arrangement": "single", "clarity": "clear"},
  "detector": {"person_boxes": [[0.62, 0.71, 0.80, 0.93, 0.58]], "person_count": 1,
               "largest_person_area": 0.04, "people_centroid": [0.71, 0.82],
               "count_agreement": "agree"},
  "frame": {"kind": "story", "style_flags": [], "burned_subtitles": false},
  "content": {"setting": "interior", "location_hint": "bedroom with sofa and rug"},
  "provenance": {"camera": "vlm:gpt-5.6-sol", "prompt_version": "shot-tag-v1",
                 "detector": "detector:grounding-dino-tiny@a2bb814", "pcp": "pcp:<sha>"}
}
```

Values above are illustrative, not real model output.

## 6. Shot-level aggregate

```json
{
  "shot_id": "mygo-ep01-s00152",
  "representative_frame_id": "mygo-ep01-s00152-f2",
  "keyframe_ids": ["mygo-ep01-s00152-f1", "mygo-ep01-s00152-f2"],
  "tags": "<representative keyframe record>",
  "varies_within_shot": []
}
```

`varies_within_shot` lists dotted field names whose values differ across the shot's keyframes, such as `subjects.main_subject_cell` for a pan.

## 7. How fields map to retrieval

| Query need | Fields |
|---|---|
| "overhead", "low angle", "close-up" | `camera.vertical_angle`, `camera.shot_size` (F) |
| "person on the left, empty right side" | `subjects.main_subject_cell`, `composition.negative_space` (F) |
| "two people", "through a window" | `subjects.person_count` + `count_agreement`, `camera.framing_device` (F) |
| general "similar composition" | `composition.*`, `pcp.probs`, `camera.*`, `subjects.*` scale/cell, detector layout (S) |
| "other shots, not the same place" | `reuse_group_id`, same-episode time window, `content.location_hint`, Doubao vector (X) |
| drop non-story frames | `frame.kind`, `is_op_ed`, segment kind (Q) |

Later production mapping (not part of this experiment): enums → ES `keyword`; PCP and category scores → `rank_features`; detector boxes and grids → `detail` (disabled) for Python reranking; vectors → `dense_vector`.

## 8. What the experiment must decide

1. Which VLM fields are **self-consistent** (same answer on a repeat run) and **agree with the user's judgments**. Unstable fields are dropped or coarsened.
2. Whether `composition.primary` from the VLM and the PCP argmax agree enough to keep both, or one is redundant.
3. Whether detector layout adds ranking value beyond VLM `cell` + `scale`.
4. Whether structure grids add anything beyond tags.
5. The cheapest VLM that keeps acceptable agreement, given ~6k–12k frames per 13 episodes.
