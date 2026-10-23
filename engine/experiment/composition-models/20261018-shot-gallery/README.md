# MyGO shared shot composition gallery

Created 2026-10-18 14:17 UTC from the 13 already indexed MyGO source episodes. This frame-level candidate gallery is independent of the Event index. Later composition experiments can score every row, aggregate results by shot, and exclude a query's `reuse_group_id` when looking for other shots.

## Counts

**5,898 shots, 12,447 keyframes, 5,585 reuse groups.** There are 169 groups containing multiple shots and 58 groups spanning episodes; the largest group contains 12 shots. All 12,389 unique JPEGs have a 1,024-dimensional Doubao embedding. 7,103 of 7,103 indexed Events overlap gallery shots.

| Episode | Shots | Keyframes | Reuse groups present | OP/ED shots | Shots without Event overlap |
| --- | ---: | ---: | ---: | ---: | ---: |
| 01 | 393 | 782 | 388 | 10 | 2 |
| 02 | 423 | 879 | 416 | 22 | 11 |
| 03 | 311 | 717 | 308 | 21 | 9 |
| 04 | 455 | 944 | 444 | 20 | 7 |
| 05 | 418 | 796 | 407 | 20 | 5 |
| 06 | 483 | 1022 | 470 | 20 | 8 |
| 07 | 467 | 1075 | 460 | 20 | 14 |
| 08 | 404 | 810 | 396 | 20 | 19 |
| 09 | 548 | 1136 | 537 | 10 | 19 |
| 10 | 595 | 1213 | 577 | 44 | 8 |
| 11 | 361 | 769 | 353 | 19 | 10 |
| 12 | 580 | 1275 | 565 | 19 | 11 |
| 13 | 460 | 1029 | 449 | 33 | 14 |

Reuse-group counts per episode overlap and must not be summed. `counts.json` contains the same machine-readable counts.

## Required reference checks

| Reference | Shot ID | Shot interval | Keyframe timestamp | Image |
| --- | --- | --- | --- | --- |
| Reference: Ep1 09:02.300-09:04.300 | `mygo-ep01-s00152` | 08:58.496–09:04.878 | 09:02.125 | [mygo-ep01-s00152-f2](frames/ep01/mygo-ep01-s00152-f2.jpg) |
| Candidate: Ep7 04:08.600 | `mygo-ep07-s00056` | 04:06.121–04:09.499 | 04:07.998 | [mygo-ep07-s00056-f1](frames/ep07/mygo-ep07-s00056-f1.jpg) |

The Ep1 check uses the midpoint (09:03.300) of the supplied 09:02.300–09:04.300 reference interval. Both required timestamps are contained in detected shot intervals and have gallery rows. `contact_sheet.jpg` starts with these two anchors, then samples shots throughout every episode.

![Reference anchors and per-episode shot sample](contact_sheet.jpg)

## Source and extraction method

Source identity, local paths, durations, source SHA-256 checksums, and source video probes are recorded in `sources.json`. Event IDs were reconciled against the current Elasticsearch publication; `events.jsonl` preserves the 7,103 source Event rows. Source times and output times are milliseconds; intervals are half-open `[start_ms, end_ms)`. Images are JPEGs at the existing capture height of 720 pixels, with aspect ratio preserved. Their SHA-256 values refer to saved JPEG bytes.

The exact segmentation and representative-frame parameters used for this run are:

```json
{
  "detector": "PySceneDetect AdaptiveDetector + ThresholdDetector fades",
  "scenedetect_version": "0.6.7.1",
  "adaptive_threshold": 3.0,
  "min_content_val": 15.0,
  "min_scene_len_seconds": 0.25,
  "window_width": 2,
  "detection_width": 320,
  "detection_height": 180,
  "fade_black_threshold": 12,
  "fade_white_threshold": 240,
  "fade_min_scene_seconds": 0.5,
  "fade_cut_distance_from_existing_seconds": 0.25,
  "capture_height": 720,
  "jpeg_qscale": 3,
  "motion_grid": [
    160,
    90
  ],
  "motion_window_frames": 5,
  "central_window": [
    0.4,
    0.6
  ],
  "extra_windows": [
    [
      0.15,
      0.35
    ],
    [
      0.65,
      0.85
    ]
  ],
  "long_shot_seconds": 4,
  "very_long_shot_seconds": 8,
  "motion_p90_threshold": 2.5,
  "motion_span_threshold": 8.0,
  "intervals": "Half-open [start_ms, end_ms); decoded frame n maps to n/fps."
}
```

PySceneDetect's AdaptiveDetector compares frame-content changes against a rolling local baseline; ThresholdDetector detects fades using frame intensity. See the [official detector documentation](https://www.scenedetect.com/docs/0.6.7/api/detectors.html). The configured minimum scene length of 0.25 seconds rounds to six frames at these sources' frame rate. This is a detector setting, not a guaranteed minimum output interval: AdaptiveDetector 0.6.7.1 checks the current input-frame distance while returning a buffered cut two frames earlier, and combining independently detected boundaries can produce shorter intervals. The gallery preserves the resulting intervals.

The central representative is the lowest-motion frame within 40–60% of each shot. Motion is the mean absolute difference between successive 160 × 90 grayscale thumbnails, smoothed with a five-frame moving average clipped to that shot; the first local difference excludes the preceding cut. Equal scores (rounded to four decimals) favor the frame nearest the center. For a shot longer than four seconds and at most eight seconds, one additional low-motion frame is selected from the 15–35% or 65–85% window, favoring the candidate farther from the central frame: **two frames total**. Shots longer than eight seconds, or flagged for internal motion, use both outer windows: **three frames total**. Duplicate selected frame indices are removed.

Internal motion requires both a 90th-percentile adjacent-frame difference of at least 2.5 grayscale levels and a mean absolute change of at least 8 levels between 32 × 18 thumbnails at 20% and 80% of the shot. This pixel-change proxy captures camera motion and substantial subject animation; it is not a camera-motion classifier. `is_representative`, `motion_score`, `motion_p90`, `motion_span`, `internal_motion`, and `selection_reason` retain the selection evidence.

`start_ms` and `end_ms` always describe the detected shot, while `frame_timestamp_ms` identifies the selected image within that shot. Multiple keyframes from a long or moving shot share the same `shot_id` and reuse group. Shot boundaries are detector estimates, not manually annotated ground truth; gradual transitions and low-contrast cuts can remain imperfect.

## Reuse grouping

Every pair of keyframes is screened with 64-bit pHash and dHash distances. Cross-shot reuse requires identical JPEG SHA-256, or both perceptual hash distances plus Doubao image cosine to pass one of the thresholds below. All embeddings are checked for finite nonzero 1,024-dimensional values and L2-normalized before cosine calculation.

```json
{
  "strict": {
    "phash_distance_max": 6,
    "dhash_distance_max": 8,
    "cosine_min": 0.97
  },
  "relaxed": {
    "phash_distance_max": 10,
    "dhash_distance_max": 12,
    "cosine_min": 0.99
  },
  "low_information_gray_std_min": 8.0,
  "low_information_gray_entropy_bits_min": 1.5,
  "clustering": "complete-link between shots; each shot pair needs a qualifying keyframe pair"
}
```

Frames with grayscale standard deviation below 8 or grayscale entropy below 1.5 bits cannot create cross-shot reuse edges; 144 such frames remain in the gallery. These measurements use a 160 × 90 grayscale thumbnail. Frames from the same detected shot always stay together. Cross-shot clusters use complete-link compatibility: every pair of shots in a cluster must have at least one independently qualifying keyframe pair. This prevents a chain of similar images from collapsing unrelated shots into a large group. The process considered 1,020 qualifying shot pairs and rejected 87 proposed merges that lacked complete-link support.

`reuse_edges.jsonl` audits accepted same-shot and cross-shot edges with matched frames, distances, cosine, and group IDs. `reuse_groups.jsonl` lists every group, its shots, episodes, and frame count. Group IDs are deterministic hashes of sorted member shot IDs. These conservative visual groups are a practical exclusion aid, not manually verified reuse ground truth; changes in crop, credits, lighting, or animation timing may leave some reused shots separate, and very similar static shots may still group together.

`reuse_contact_sheet.jpg` shows up to six distinct shots from each of the twelve largest reuse groups for visual auditing.

## OP/ED annotations

`is_op_ed` is true when the detected shot has any positive time overlap with a manually reviewed OP/ED interval in `op_ed_segments.json`. A boundary-crossing shot is therefore flagged as a whole. `op_ed_labels` records OP and/or ED. The annotation distinguishes standard sequences from opening/ending credits over narrative footage using `segment_type`; the intervals include both kinds where present. Review evidence and boundary brackets/tolerances remain in the annotation file, so these manual boundaries should not be treated as frame-exact labels. 666 keyframes are flagged. Every episode was explicitly reviewed; these flags do not arise from positional guesses or embedding similarity.

| Episode | Type | Start | End |
| --- | --- | --- | --- |
| 01 | ED | 22:04.600 | 23:34.814 |
| 02 | OP | 00:55.687 | 02:26.052 |
| 02 | ED | 21:54.812 | 23:24.500 |
| 03 | OP | 00:00.000 | 01:30.437 |
| 03 | ED | 21:54.500 | 23:25.099 |
| 04 | OP | 01:24.812 | 02:55.050 |
| 04 | ED | 20:58.100 | 22:28.400 |
| 05 | OP | 01:17.312 | 02:47.294 |
| 05 | ED | 21:54.750 | 23:24.500 |
| 06 | OP | 02:21.054 | 03:50.750 |
| 06 | ED | 21:54.812 | 23:25.149 |
| 07 | OP | 01:58.400 | 03:27.500 |
| 07 | ED | 20:58.539 | 22:29.250 |
| 08 | OP | 00:32.500 | 02:03.200 |
| 08 | ED | 21:54.812 | 23:24.717 |
| 09 | ED | 21:54.812 | 23:25.175 |
| 10 | OP | 00:52.250 | 02:33.250 |
| 10 | ED | 21:54.812 | 23:24.500 |
| 11 | OP | 01:49.187 | 03:18.906 |
| 11 | ED | 21:54.812 | 23:25.123 |
| 12 | OP | 00:54.500 | 02:23.500 |
| 12 | ED | 21:54.812 | 23:24.750 |
| 13 | OP | 01:06.062 | 02:35.750 |
| 13 | ED | 22:32.500 | 23:33.500 |

Annotation notes: All 13 episodes were visually reviewed against source-video contact sheets. Labels include 10 standard OP sequences, 12 standard ED sequences, Episode 10 opening credits over narrative footage, and Episode 13 ending credits over narrative footage. segment_type distinguishes standard and narrative_credits; narrative intervals include story shots and gaps between credit cards. Boundaries are bracketed visual estimates with at most +/-542 ms tolerance, including one source frame of seek uncertainty. No OP was found in Episodes 1 and 9 in the reviewed first 180 seconds or indexed Event descriptions; see limitations and absence evidence.

## Event mapping and row schema

Each shot maps to every indexed Event in the same episode for which `event.start_ms < shot.end_ms` and `shot.start_ms < event.end_ms`. This uses original formal Event spans, including Events crossing Chunk boundaries; it does not use keyframe-only membership or clamp Event spans to capture bounds. An empty `event_ids` array is retained where the index has no overlapping Event. Multiple keyframes within a shot have identical mappings.

Each `gallery.jsonl` line contains `frame_id`, `shot_id`, `episode`, `start_ms`, `end_ms`, `frame_timestamp_ms`, `frame_path`, `sha256`, `reuse_group_id`, `event_ids`, and `is_op_ed`. It also retains pHash/dHash, OP/ED labels, grayscale information measures, and extraction metadata. `frame_path` is relative to this directory. A frame embedding is at `embeddings/<sha256>.npy`.

The embedding consolidation step also writes `embeddings.npy` and `embedding_rows.jsonl`. Join those rows to the gallery by `frame_id`; the row map is the authoritative matrix ordering. Stored vectors are the raw model outputs, while this finalizer normalizes them for cosine comparison. `embeddings.json` records the `doubao-embedding-vision-250615` encoder contract and execution metadata.

For later experiments, rank keyframes exhaustively, collapse hits by `shot_id`, and exclude the query's `reuse_group_id` when requested. Use `event_ids` for links back to Event retrieval. This is a candidate corpus; category-match and layout-match judgments still require an independently assembled evaluation set.

## Reproduction and validation

Run the following commands from the **repository root**. The extraction script uses the existing `.cache/composition-models/.venv` environment (OpenCV 4.11.0.86, NumPy 1.26.4, Pillow 13.0.0, SciPy 1.17.1). `environment.json` records the Python, platform, FFmpeg, and image/detection package versions used. PySceneDetect 0.6.7.1 and ImageHash 4.3.2 were installed only into this experiment's `_deps` directory; the script prepends that directory to its import path. The isolated installation can be reproduced with:

```sh
UV_CACHE_DIR=/private/tmp/anivag-shot-gallery-uv uv pip install --python .cache/composition-models/.venv/bin/python --no-deps --target engine/experiment/composition-models/20261018-shot-gallery/_deps scenedetect==0.6.7.1 ImageHash==4.3.2
```

For a fresh experiment, create the source snapshot once:

```sh
UV_CACHE_DIR=/private/tmp/anivag-shot-gallery-uv uv run --no-sync --env-file .env.local python engine/experiment/composition-models/20261018-shot-gallery/snapshot_sources.py
```

The snapshot command reads the local indexed data and deliberately refuses to replace an existing `sources.json` or `events.jsonl`. The completed experiment already contains both files. Continue with shot detection, frame extraction, and visual review:

```sh
.cache/composition-models/.venv/bin/python engine/experiment/composition-models/20261018-shot-gallery/build_keyframes.py --workers 3
.venv/bin/python engine/experiment/composition-models/20261018-shot-gallery/finalize_gallery.py --preview
```

Detection and capture resume from per-episode checkpoints. Cached detection parameters must exactly match `PARAMETERS` in `build_keyframes.py`; a mismatched checkpoint fails instead of silently mixing detector configurations. Review the opening/ending sheets and preserve all reviewed intervals and review notes in `op_ed_segments.json`, including explicit review of episodes without an OP or ED. Preview mode produces contact sheets without publishing a final gallery.

Complete the per-image embedding cache, consolidate the matrix, and finalize:

```sh
UV_CACHE_DIR=/private/tmp/anivag-shot-gallery-uv uv run --no-sync --env-file .env.local python engine/experiment/composition-models/20261018-shot-gallery/embed_frames.py --consolidate
.venv/bin/python engine/experiment/composition-models/20261018-shot-gallery/finalize_gallery.py
```

The embedding command uses the configured gateway, reuses validated cached vectors by image SHA-256, and requests only missing images. Extraction and finalization operate locally. Finalization requires embeddings for every frame and explicit OP/ED review for every episode. It verifies JPEG format and capture height, all JPEG SHA-256 values, unique frame IDs, consistent shot intervals, valid timestamps and hash encodings, source-duration bounds, coverage against `shots.jsonl` or the episode detection checkpoints, embedding dimensions and finiteness, and both reference anchors. Corpus checks require contiguous frame and millisecond partitions from frame zero through the decoded source frame count, one to three captured keyframes per shot matching the detector selection, and exactly one representative within the configured central window. Event mappings use the explicit overlap rule above; when `sources.json` specifies an Event count, every snapshotted Event must overlap a gallery shot. Results are written to `validation.json`.

All artifacts are under this ignored experiment directory. No production index is modified by finalization.
