"""Validate extracted keyframes, group shot reuse, and publish the local gallery."""

import argparse
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageOps


OUT = Path(__file__).resolve().parent
ANCHORS = [(1, 543300, "Reference: Ep1 09:02.300-09:04.300"),
           (7, 248600, "Candidate: Ep7 04:08.600")]
REUSE_PARAMS = {
    "strict": {"phash_distance_max": 6, "dhash_distance_max": 8, "cosine_min": 0.97},
    "relaxed": {"phash_distance_max": 10, "dhash_distance_max": 12, "cosine_min": 0.99},
    "low_information_gray_std_min": 8.0,
    "low_information_gray_entropy_bits_min": 1.5,
    "clustering": "complete-link between shots; each shot pair needs a qualifying keyframe pair",
}


def read_jsonl(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def write_jsonl(path, rows):
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows))


def timestamp(ms):
    return f"{int(ms) // 60000:02d}:{int(ms) % 60000 / 1000:06.3f}"


def anchor_rows(rows, required=True):
    result = []
    for episode, at, label in ANCHORS:
        matches = [r for r in rows if r["episode"] == episode and r["start_ms"] <= at < r["end_ms"]]
        if not matches:
            if required:
                raise ValueError(f"Missing required anchor: {label}")
            continue
        row = min(matches, key=lambda r: abs(r["frame_timestamp_ms"] - at))
        result.append((row, label))
    return result


def contact_sheet(root, samples, path, title, columns=6):
    width, picture_h, label_h, header_h = 284, 160, 54, 46
    sheet = Image.new("RGB", (columns * width, header_h + math.ceil(len(samples) / columns) * (picture_h + label_h)), "#17202c")
    draw = ImageDraw.Draw(sheet)
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Menlo.ttc", 12)
        heading = ImageFont.truetype("/System/Library/Fonts/Menlo.ttc", 18)
    except OSError:
        font = heading = ImageFont.load_default()
    draw.text((12, 13), title, fill="white", font=heading)
    for i, (row, label) in enumerate(samples):
        x, y = (i % columns) * width, header_h + (i // columns) * (picture_h + label_h)
        with Image.open(root / row["frame_path"]) as original:
            thumb = ImageOps.contain(original.convert("RGB"), (width - 8, picture_h - 4))
        sheet.paste(thumb, (x + (width - thumb.width) // 2, y + (picture_h - thumb.height) // 2))
        lines = [label[:37], f"Ep{row['episode']:02d} frame {timestamp(row['frame_timestamp_ms'])}",
                 f"{timestamp(row['start_ms'])}-{timestamp(row['end_ms'])}"]
        draw.multiline_text((x + 5, y + picture_h), "\n".join(lines), fill="#dde6f0", font=font, spacing=2)
    path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(path, quality=91)


def make_contact_sheets(root, rows, preview=False):
    samples = anchor_rows(rows, required=not preview)
    by_episode = defaultdict(list)
    representatives = {}
    for row in rows:
        if row["shot_id"] not in representatives or row.get("is_representative"):
            representatives[row["shot_id"]] = row
    for row in representatives.values():
        by_episode[row["episode"]].append(row)
    selected = {r["frame_id"] for r, _ in samples}
    for episode, episode_rows in sorted(by_episode.items()):
        for index in np.linspace(0, len(episode_rows) - 1, 5).round().astype(int):
            row = episode_rows[index]
            if row["frame_id"] not in selected:
                samples.append((row, row["shot_id"]))
                selected.add(row["frame_id"])
    contact_sheet(root, samples, root / "contact_sheet.jpg", "MyGO shot gallery - reference anchors and samples across all episodes")
    write_jsonl(root / "contact_sheet_manifest.jsonl", [dict(frame_id=r["frame_id"], label=label) for r, label in samples])
    if preview:
        for episode, episode_rows in sorted(by_episode.items()):
            end = max(r["end_ms"] for r in episode_rows)
            times = list(range(0, min(end, 300000), 10000)) + list(range(max(300000, end - 210000), end, 10000))
            review = []
            for at in times:
                row = min(episode_rows, key=lambda r: abs(r["frame_timestamp_ms"] - at))
                review.append((row, f"Requested vicinity {timestamp(at)}"))
            contact_sheet(root, review, root / "op_ed_review" / f"ep{episode:02d}.jpg",
                          f"Ep{episode:02d} - opening/ending review; labels show actual frame times")


def validate_frames(root, rows, sources, params):
    required = {"frame_id", "shot_id", "episode", "start_ms", "end_ms", "frame_timestamp_ms", "frame_path", "sha256", "phash", "dhash",
                "start_frame", "end_frame", "frame_index", "is_representative"}
    frame_ids, shots, low_information = set(), {}, []
    episode_sources = {e["episode"]: e for e in sources["episodes"]}
    for index, row in enumerate(rows):
        if not required <= row.keys():
            raise ValueError(f"Missing frame fields: {required - row.keys()}")
        if row["frame_id"] in frame_ids:
            raise ValueError(f"Duplicate frame_id: {row['frame_id']}")
        frame_ids.add(row["frame_id"])
        if not 0 <= row["start_ms"] <= row["frame_timestamp_ms"] < row["end_ms"]:
            raise ValueError(f"Invalid frame/shot interval: {row['frame_id']}")
        identity = (row["episode"], row["start_ms"], row["end_ms"])
        if row["shot_id"] in shots and shots[row["shot_id"]] != identity:
            raise ValueError(f"Conflicting shot metadata: {row['shot_id']}")
        shots[row["shot_id"]] = identity
        path = (root / row["frame_path"]).resolve()
        if not path.is_relative_to(root.resolve()):
            raise ValueError(f"Frame is outside gallery directory: {path}")
        if hashlib.sha256(path.read_bytes()).hexdigest() != row["sha256"]:
            raise ValueError(f"Frame SHA-256 mismatch: {path}")
        with Image.open(path) as image:
            if image.format != "JPEG" or image.height != sources["capture_height"]:
                raise ValueError(f"Expected JPEG at capture height {sources['capture_height']}: {path}")
            gray = np.asarray(image.convert("L").resize((160, 90)), dtype=np.uint8)
        counts = np.bincount(gray.ravel(), minlength=256)
        probabilities = counts[counts > 0] / gray.size
        entropy = float(-(probabilities * np.log2(probabilities)).sum())
        std = float(gray.std())
        row["gray_std"] = round(std, 4)
        row["gray_entropy_bits"] = round(entropy, 4)
        row["low_information"] = std < REUSE_PARAMS["low_information_gray_std_min"] or entropy < REUSE_PARAMS["low_information_gray_entropy_bits_min"]
        low_information.append(row["low_information"])
        for name in ("phash", "dhash"):
            if not isinstance(row[name], str) or len(row[name]) != 16:
                raise ValueError(f"Expected 64-bit hexadecimal {name}: {row['frame_id']}")
            int(row[name], 16)
        if row["end_ms"] > episode_sources[row["episode"]]["duration_ms"] + 100:
            raise ValueError(f"Shot extends beyond source duration: {row['shot_id']}")
        if index and index % 1000 == 0:
            print(json.dumps({"jpeg_rows_verified": index, "total": len(rows)}), flush=True)
    if set(e for e, _, _ in shots.values()) != set(episode_sources):
        raise ValueError("Gallery does not cover every source episode")
    shot_path = root / "shots.jsonl"
    detected = []
    if shot_path.exists():
        detected = read_jsonl(shot_path)
    else:
        for checkpoint in sorted((root / "shots").glob("ep[0-9][0-9].json")):
            detected.extend(json.loads(checkpoint.read_text())["shots"])
    if detected:
        expected = {s["shot_id"] for s in detected}
        if set(shots) != expected:
            raise ValueError(f"Shot/keyframe mismatch: missing={len(expected - shots.keys())}, extra={len(shots.keys() - expected)}")
        for shot in detected:
            if shots[shot["shot_id"]] != (shot["episode"], shot["start_ms"], shot["end_ms"]):
                raise ValueError(f"Shot interval differs from detector output: {shot['shot_id']}")
    else:
        raise ValueError("No detected-shot manifest available for corpus validation")
    by_shot, by_episode = defaultdict(list), defaultdict(list)
    for row in rows:
        by_shot[row["shot_id"]].append(row)
    for shot in detected:
        by_episode[shot["episode"]].append(shot)
        selected = by_shot[shot["shot_id"]]
        if not 1 <= len(selected) <= 3:
            raise ValueError(f"Expected one to three keyframes: {shot['shot_id']}")
        if any((r["start_frame"], r["end_frame"]) != (shot["start_frame"], shot["end_frame"]) for r in selected):
            raise ValueError(f"Keyframe frame bounds differ from shot: {shot['shot_id']}")
        indexes = sorted(r["frame_index"] for r in selected)
        if len(set(indexes)) != len(indexes) or indexes != sorted(shot["frame_indices"]):
            raise ValueError(f"Captured frame indices differ from shot selection: {shot['shot_id']}")
        representatives = [r for r in selected if r["is_representative"]]
        if len(representatives) != 1:
            raise ValueError(f"Expected exactly one central representative: {shot['shot_id']}")
        count = shot["end_frame"] - shot["start_frame"]
        low, high = params["central_window"]
        begin = min(count - 1, max(0, int(count * low)))
        end = min(count, max(begin + 1, int(count * high)))
        if not begin <= representatives[0]["frame_index"] - shot["start_frame"] < end:
            raise ValueError(f"Representative outside central window: {shot['shot_id']}")
        for row in selected:
            if not shot["start_frame"] <= row["frame_index"] < shot["end_frame"]:
                raise ValueError(f"Frame index outside shot: {row['frame_id']}")
    coverage = []
    for episode, source in sorted(episode_sources.items()):
        episode_shots = sorted(by_episode[episode], key=lambda s: s["start_frame"])
        checkpoint_path = root / "shots" / f"ep{episode:02d}.json"
        checkpoint = json.loads(checkpoint_path.read_text()) if checkpoint_path.exists() else {}
        decoded_frames = checkpoint.get("decoded_frames", source.get("frame_count"))
        if decoded_frames is None or (source.get("frame_count") is not None and decoded_frames != source["frame_count"]):
            raise ValueError(f"Decoded/source frame count differs for episode {episode}")
        if checkpoint and checkpoint["parameters"] != params:
            raise ValueError(f"Detection checkpoint parameters differ for episode {episode}")
        if episode_shots[0]["start_frame"] != 0 or episode_shots[0]["start_ms"] != 0 or episode_shots[-1]["end_frame"] != decoded_frames:
            raise ValueError(f"Shots do not span all decoded frames for episode {episode}")
        for previous, following in zip(episode_shots, episode_shots[1:]):
            if previous["end_frame"] != following["start_frame"] or previous["end_ms"] != following["start_ms"]:
                raise ValueError(f"Shot partition gap/overlap for episode {episode}")
        coverage.append({"episode": episode, "decoded_frames": decoded_frames, "shots": len(episode_shots),
                         "start_frame": 0, "end_frame": episode_shots[-1]["end_frame"],
                         "end_ms": episode_shots[-1]["end_ms"], "contiguous_frame_and_ms_partition": True})
    anchor_rows(rows)
    integrity = {"episode_partitions": coverage, "keyframes_per_shot_min": min(map(len, by_shot.values())),
                 "keyframes_per_shot_max": max(map(len, by_shot.values())), "exactly_one_central_representative_per_shot": True,
                 "captured_frame_indices_match_detection": True, "detected_shot_manifest_checked": True}
    return shots, np.asarray(low_information, dtype=bool), integrity


def group_reuse(root, rows, low_information):
    vectors = []
    for row in rows:
        vector = np.load(root / "embeddings" / f"{row['sha256']}.npy", allow_pickle=False).reshape(-1).astype(np.float32)
        if vector.shape != (1024,) or not np.isfinite(vector).all() or np.linalg.norm(vector) == 0:
            raise ValueError(f"Invalid Doubao embedding: {row['frame_id']}")
        vectors.append(vector / np.linalg.norm(vector))
    vectors = np.stack(vectors)
    phashes = np.array([int(r["phash"], 16) for r in rows], dtype=np.uint64)
    dhashes = np.array([int(r["dhash"], 16) for r in rows], dtype=np.uint64)
    popcount = np.array([i.bit_count() for i in range(256)], dtype=np.uint8)
    shot_frames = defaultdict(list)
    for index, row in enumerate(rows):
        shot_frames[row["shot_id"]].append(index)
    best_edges = {}
    strict, relaxed = REUSE_PARAMS["strict"], REUSE_PARAMS["relaxed"]
    # Hash screening is exhaustive; only the small surviving set needs a cosine.
    for i, row in enumerate(rows):
        if low_information[i]:
            continue
        pd = popcount[np.bitwise_xor(phashes[i + 1:], phashes[i]).view(np.uint8).reshape(-1, 8)].sum(axis=1)
        dd = popcount[np.bitwise_xor(dhashes[i + 1:], dhashes[i]).view(np.uint8).reshape(-1, 8)].sum(axis=1)
        possible = np.flatnonzero((pd <= relaxed["phash_distance_max"]) & (dd <= relaxed["dhash_distance_max"]) & ~low_information[i + 1:]) + i + 1
        for j in possible:
            other = rows[int(j)]
            if row["shot_id"] == other["shot_id"]:
                continue
            cosine = float(vectors[i] @ vectors[j])
            p_distance, d_distance = int(pd[j - i - 1]), int(dd[j - i - 1])
            exact = row["sha256"] == other["sha256"]
            strict_match = (p_distance <= strict["phash_distance_max"] and d_distance <= strict["dhash_distance_max"]
                            and cosine >= strict["cosine_min"])
            relaxed_match = (p_distance <= relaxed["phash_distance_max"] and d_distance <= relaxed["dhash_distance_max"]
                             and cosine >= relaxed["cosine_min"])
            if not (exact or strict_match or relaxed_match):
                continue
            pair = tuple(sorted((row["shot_id"], other["shot_id"])))
            edge = {"shot_a": row["shot_id"], "shot_b": other["shot_id"],
                    "frame_a": row["frame_id"], "frame_b": other["frame_id"],
                    "reason": "sha256" if exact else "perceptual_hash_and_doubao", "phash_distance": p_distance,
                    "dhash_distance": d_distance, "cosine": round(cosine, 8)}
            priority = (exact, cosine, -p_distance, -d_distance)
            if pair not in best_edges or priority > best_edges[pair][0]:
                best_edges[pair] = (priority, edge)
        if i and i % 1000 == 0:
            print(json.dumps({"reuse_rows_scored": i, "total": len(rows), "qualifying_shot_pairs": len(best_edges)}), flush=True)
    owner = {shot: shot for shot in shot_frames}
    members = {shot: {shot} for shot in shot_frames}
    rejected = 0
    for pair, (_, edge) in sorted(best_edges.items(), key=lambda item: (item[1][0], item[0]), reverse=True):
        a, b = (owner[shot] for shot in pair)
        if a == b:
            continue
        # This forbids A~B~C chains when A and C have no direct visual match.
        if not all(tuple(sorted((left, right))) in best_edges for left in members[a] for right in members[b]):
            rejected += 1
            continue
        keep, drop = sorted((a, b))
        members[keep].update(members.pop(drop))
        for shot in members[keep]:
            owner[shot] = keep
    group_ids, groups = {}, []
    for shots in sorted(members.values(), key=lambda value: min(value)):
        ordered = sorted(shots)
        group_id = "reuse_" + hashlib.sha256("\n".join(ordered).encode()).hexdigest()[:16]
        indexes = sorted(i for shot in ordered for i in shot_frames[shot])
        for shot in ordered:
            group_ids[shot] = group_id
        groups.append({"reuse_group_id": group_id, "shot_ids": ordered, "shot_count": len(ordered),
                       "frame_count": len(indexes), "episodes": sorted({rows[i]["episode"] for i in indexes}),
                       "representative_frame_id": rows[indexes[0]]["frame_id"],
                       "low_information_frame_count": int(low_information[indexes].sum())})
    accepted = []
    for shot, indexes in shot_frames.items():
        for index in indexes[1:]:
            accepted.append({"shot_a": shot, "shot_b": shot, "frame_a": rows[indexes[0]]["frame_id"],
                             "frame_b": rows[index]["frame_id"], "reason": "same_shot", "reuse_group_id": group_ids[shot]})
    for pair, (_, edge) in sorted(best_edges.items()):
        if group_ids[pair[0]] == group_ids[pair[1]]:
            accepted.append(dict(edge, reuse_group_id=group_ids[pair[0]]))
    return group_ids, groups, accepted, {"qualifying_cross_shot_pairs": len(best_edges), "complete_link_rejected_merges": rejected,
                                       "accepted_cross_shot_pairs": sum(e["reason"] != "same_shot" for e in accepted), "embedding_dimensions": vectors.shape[1]}


def attach_events_and_op_ed(rows, events, annotation):
    by_episode = defaultdict(list)
    for event in events:
        if not event["start_ms"] < event["end_ms"]:
            raise ValueError(f"Invalid Event interval: {event['event_id']}")
        by_episode[event["episode"]].append(event)
    episodes = {r["episode"] for r in rows}
    if not episodes <= set(annotation["reviewed_episodes"]):
        raise ValueError("OP/ED annotation must explicitly review every episode")
    for segment in annotation["segments"]:
        if segment["episode"] not in episodes or segment["start_ms"] >= segment["end_ms"] or segment["label"] not in {"OP", "ED"}:
            raise ValueError(f"Invalid OP/ED segment: {segment}")
    mapped = {}
    for row in rows:
        shot = row["shot_id"]
        if shot not in mapped:
            overlapping = [e["event_id"] for e in by_episode[row["episode"]]
                           if e["start_ms"] < row["end_ms"] and row["start_ms"] < e["end_ms"]]
            mapped[shot] = sorted(set(overlapping))
        row["event_ids"] = mapped[shot]
        matches = [segment for segment in annotation["segments"] if segment["episode"] == row["episode"]
                   and segment["start_ms"] < row["end_ms"] and row["start_ms"] < segment["end_ms"]]
        row["is_op_ed"] = bool(matches)
        row["op_ed_labels"] = sorted({segment["label"] for segment in matches})


def write_readme(root, rows, groups, sources, params, annotation, stats):
    per_episode = []
    for source in sources["episodes"]:
        current = [r for r in rows if r["episode"] == source["episode"]]
        per_episode.append({"episode": source["episode"], "shots": len({r["shot_id"] for r in current}),
                            "keyframes": len(current), "reuse_groups": len({r["reuse_group_id"] for r in current}),
                            "op_ed_shots": len({r["shot_id"] for r in current if r["is_op_ed"]}),
                            "unmapped_shots": len({r["shot_id"] for r in current if not r["event_ids"]})})
    counts = {"episodes": len(per_episode), "shots": sum(e["shots"] for e in per_episode), "keyframes": len(rows),
              "unique_jpeg_sha256": len({r["sha256"] for r in rows}), "reuse_groups": len(groups),
              "groups_with_multiple_shots": sum(g["shot_count"] > 1 for g in groups),
              "cross_episode_reuse_groups": sum(len(g["episodes"]) > 1 for g in groups),
              "largest_reuse_group_shots": max(g["shot_count"] for g in groups),
              "low_information_keyframes": sum(r["low_information"] for r in rows),
              "op_ed_keyframes": sum(r["is_op_ed"] for r in rows),
              "indexed_events": len(read_jsonl(root / "events.jsonl")),
              "overlapped_events": len({event for r in rows for event in r["event_ids"]}), "per_episode": per_episode, **stats}
    (root / "counts.json").write_text(json.dumps(counts, indent=2) + "\n")
    anchors = []
    for row, label in anchor_rows(rows):
        anchors.append(f"| {label} | `{row['shot_id']}` | {timestamp(row['start_ms'])}–{timestamp(row['end_ms'])} | {timestamp(row['frame_timestamp_ms'])} | [{row['frame_id']}]({row['frame_path']}) |")
    table = "\n".join(f"| {e['episode']:02d} | {e['shots']} | {e['keyframes']} | {e['reuse_groups']} | {e['op_ed_shots']} | {e['unmapped_shots']} |" for e in per_episode)
    segments = "\n".join(f"| {s['episode']:02d} | {s['label']} | {timestamp(s['start_ms'])} | {timestamp(s['end_ms'])} |" for s in annotation["segments"])
    readme = f"""# MyGO shared shot composition gallery

Created {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} from the 13 already indexed MyGO source episodes. This frame-level candidate gallery is independent of the Event index. Later composition experiments can score every row, aggregate results by shot, and exclude a query's `reuse_group_id` when looking for other shots.

## Counts

**{counts['shots']:,} shots, {counts['keyframes']:,} keyframes, {counts['reuse_groups']:,} reuse groups.** There are {counts['groups_with_multiple_shots']:,} groups containing multiple shots and {counts['cross_episode_reuse_groups']:,} groups spanning episodes; the largest group contains {counts['largest_reuse_group_shots']} shots. All {counts['unique_jpeg_sha256']:,} unique JPEGs have a 1,024-dimensional Doubao embedding. {counts['overlapped_events']:,} of {counts['indexed_events']:,} indexed Events overlap gallery shots.

| Episode | Shots | Keyframes | Reuse groups present | OP/ED shots | Shots without Event overlap |
| --- | ---: | ---: | ---: | ---: | ---: |
{table}

Reuse-group counts per episode overlap and must not be summed. `counts.json` contains the same machine-readable counts.

## Required reference checks

| Reference | Shot ID | Shot interval | Keyframe timestamp | Image |
| --- | --- | --- | --- | --- |
{chr(10).join(anchors)}

The Ep1 check uses the midpoint (09:03.300) of the supplied 09:02.300–09:04.300 reference interval. Both required timestamps are contained in detected shot intervals and have gallery rows. `contact_sheet.jpg` starts with these two anchors, then samples shots throughout every episode.

![Reference anchors and per-episode shot sample](contact_sheet.jpg)

## Source and extraction method

Source identity, local paths, durations, source SHA-256 checksums, and source video probes are recorded in `sources.json`. Event IDs were reconciled against the current Elasticsearch publication; `events.jsonl` preserves the {counts['indexed_events']:,} source Event rows. Source times and output times are milliseconds; intervals are half-open `[start_ms, end_ms)`. Images are JPEGs at the existing capture height of {sources['capture_height']} pixels, with aspect ratio preserved. Their SHA-256 values refer to saved JPEG bytes.

The exact segmentation and representative-frame parameters used for this run are:

```json
{json.dumps(params, ensure_ascii=False, indent=2)}
```

PySceneDetect's AdaptiveDetector compares frame-content changes against a rolling local baseline; ThresholdDetector detects fades using frame intensity. See the [official detector documentation](https://www.scenedetect.com/docs/0.6.7/api/detectors.html). The configured minimum scene length of 0.25 seconds rounds to six frames at these sources' frame rate. This is a detector setting, not a guaranteed minimum output interval: AdaptiveDetector 0.6.7.1 checks the current input-frame distance while returning a buffered cut two frames earlier, and combining independently detected boundaries can produce shorter intervals. The gallery preserves the resulting intervals.

The central representative is the lowest-motion frame within 40–60% of each shot. Motion is the mean absolute difference between successive 160 × 90 grayscale thumbnails, smoothed with a five-frame moving average clipped to that shot; the first local difference excludes the preceding cut. Equal scores (rounded to four decimals) favor the frame nearest the center. For a shot longer than four seconds and at most eight seconds, one additional low-motion frame is selected from the 15–35% or 65–85% window, favoring the candidate farther from the central frame: **two frames total**. Shots longer than eight seconds, or flagged for internal motion, use both outer windows: **three frames total**. Duplicate selected frame indices are removed.

Internal motion requires both a 90th-percentile adjacent-frame difference of at least 2.5 grayscale levels and a mean absolute change of at least 8 levels between 32 × 18 thumbnails at 20% and 80% of the shot. This pixel-change proxy captures camera motion and substantial subject animation; it is not a camera-motion classifier. `is_representative`, `motion_score`, `motion_p90`, `motion_span`, `internal_motion`, and `selection_reason` retain the selection evidence.

`start_ms` and `end_ms` always describe the detected shot, while `frame_timestamp_ms` identifies the selected image within that shot. Multiple keyframes from a long or moving shot share the same `shot_id` and reuse group. Shot boundaries are detector estimates, not manually annotated ground truth; gradual transitions and low-contrast cuts can remain imperfect.

## Reuse grouping

Every pair of keyframes is screened with 64-bit pHash and dHash distances. Cross-shot reuse requires identical JPEG SHA-256, or both perceptual hash distances plus Doubao image cosine to pass one of the thresholds below. All embeddings are checked for finite nonzero 1,024-dimensional values and L2-normalized before cosine calculation.

```json
{json.dumps(REUSE_PARAMS, indent=2)}
```

Frames with grayscale standard deviation below 8 or grayscale entropy below 1.5 bits cannot create cross-shot reuse edges; {counts['low_information_keyframes']} such frames remain in the gallery. These measurements use a 160 × 90 grayscale thumbnail. Frames from the same detected shot always stay together. Cross-shot clusters use complete-link compatibility: every pair of shots in a cluster must have at least one independently qualifying keyframe pair. This prevents a chain of similar images from collapsing unrelated shots into a large group. The process considered {stats['qualifying_cross_shot_pairs']:,} qualifying shot pairs and rejected {stats['complete_link_rejected_merges']:,} proposed merges that lacked complete-link support.

`reuse_edges.jsonl` audits accepted same-shot and cross-shot edges with matched frames, distances, cosine, and group IDs. `reuse_groups.jsonl` lists every group, its shots, episodes, and frame count. Group IDs are deterministic hashes of sorted member shot IDs. These conservative visual groups are a practical exclusion aid, not manually verified reuse ground truth; changes in crop, credits, lighting, or animation timing may leave some reused shots separate, and very similar static shots may still group together.

`reuse_contact_sheet.jpg` shows up to six distinct shots from each of the twelve largest reuse groups for visual auditing.

## OP/ED annotations

`is_op_ed` is true when the detected shot has any positive time overlap with a manually reviewed OP/ED interval in `op_ed_segments.json`. A boundary-crossing shot is therefore flagged as a whole. `op_ed_labels` records OP and/or ED. The annotation distinguishes standard sequences from opening/ending credits over narrative footage using `segment_type`; the intervals include both kinds where present. Review evidence and boundary brackets/tolerances remain in the annotation file, so these manual boundaries should not be treated as frame-exact labels. {counts['op_ed_keyframes']:,} keyframes are flagged. Every episode was explicitly reviewed; these flags do not arise from positional guesses or embedding similarity.

| Episode | Type | Start | End |
| --- | --- | --- | --- |
{segments}

Annotation notes: {annotation.get('notes', 'See op_ed_segments.json for review evidence.')}

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
"""
    (root / "README.md").write_text(readme)
    return counts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=OUT)
    parser.add_argument("--preview", action="store_true", help="Only produce visual review sheets; no final gallery")
    args = parser.parse_args()
    root = args.root.resolve()
    rows = sorted(read_jsonl(root / "keyframes.jsonl"), key=lambda r: (r["episode"], r["start_ms"], r["frame_timestamp_ms"]))
    if not rows:
        raise ValueError("No keyframes available")
    if args.preview:
        make_contact_sheets(root, rows, preview=True)
        print(json.dumps({"preview_keyframes": len(rows), "contact_sheet": str(root / "contact_sheet.jpg")}))
        return
    sources = json.loads((root / "sources.json").read_text())
    params = json.loads((root / "parameters.json").read_text())
    annotation = json.loads((root / "op_ed_segments.json").read_text())
    events = read_jsonl(root / "events.jsonl")
    shots, low_information, integrity = validate_frames(root, rows, sources, params)
    attach_events_and_op_ed(rows, events, annotation)
    event_ids = {event["event_id"] for event in events}
    mapped_event_ids = {event_id for row in rows for event_id in row["event_ids"]}
    if len(event_ids) != len(events):
        raise ValueError("Duplicate Event IDs in source snapshot")
    expected_events = sources.get("event_count")
    if expected_events is not None and (len(event_ids) != expected_events or mapped_event_ids != event_ids):
        raise ValueError(f"Expected all {expected_events} snapshotted Events to overlap shots; source={len(event_ids)}, mapped={len(mapped_event_ids)}")
    integrity.update(source_event_count=len(event_ids), mapped_event_count=len(mapped_event_ids),
                     expected_event_count=expected_events, all_snapshot_events_mapped=mapped_event_ids == event_ids)
    group_ids, groups, edges, stats = group_reuse(root, rows, low_information)
    for row in rows:
        row["reuse_group_id"] = group_ids[row["shot_id"]]
    write_jsonl(root / "gallery.jsonl", rows)
    write_jsonl(root / "reuse_groups.jsonl", groups)
    write_jsonl(root / "reuse_edges.jsonl", edges)
    make_contact_sheets(root, rows)
    reuse_samples = []
    for group in sorted(groups, key=lambda g: (-g["shot_count"], -len(g["episodes"]), g["reuse_group_id"]))[:12]:
        if group["shot_count"] < 2:
            continue
        by_shot = {}
        for row in rows:
            if row["reuse_group_id"] == group["reuse_group_id"] and (row["shot_id"] not in by_shot or row.get("is_representative")):
                by_shot[row["shot_id"]] = row
        for index in np.linspace(0, len(group["shot_ids"]) - 1, min(6, len(group["shot_ids"]))).round().astype(int):
            shot = group["shot_ids"][index]
            reuse_samples.append((by_shot[shot], group["reuse_group_id"]))
    if reuse_samples:
        contact_sheet(root, reuse_samples, root / "reuse_contact_sheet.jpg", "Reuse-group audit sample - up to six shots per group")
    counts = write_readme(root, rows, groups, sources, params, annotation, stats)
    validation = {"status": "passed", "created_at": datetime.now(timezone.utc).isoformat(),
                  "corpus_integrity": integrity,
                  "keyframes_verified": len(rows), "shots_verified": len(shots), "jpeg_sha256_verified": len(rows),
                  "embedding_rows_verified": len(rows), "op_ed_reviewed_episodes": sorted(annotation["reviewed_episodes"]),
                  "event_overlap_rule": "same episode and event.start_ms < shot.end_ms and shot.start_ms < event.end_ms",
                  "anchors": [{"label": label, "frame_id": row["frame_id"], "shot_id": row["shot_id"],
                               "frame_timestamp_ms": row["frame_timestamp_ms"], "event_ids": row["event_ids"]}
                              for row, label in anchor_rows(rows)],
                  "gallery_sha256": hashlib.sha256((root / "gallery.jsonl").read_bytes()).hexdigest()}
    (root / "validation.json").write_text(json.dumps(validation, indent=2) + "\n")
    print(json.dumps(counts, indent=2))


if __name__ == "__main__":
    main()
