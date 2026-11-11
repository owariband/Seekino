"""Select visually distinct changes and measure their boundaries on the source clock."""

from __future__ import annotations

import json
import subprocess
from bisect import bisect_left
from collections import Counter
from contextlib import contextmanager
from pathlib import Path
from statistics import median

from PIL import Image, ImageChops, ImageStat


@contextmanager
def _raw_frames(source, ffmpeg, width, height, mode):
    pixel_format = "gray" if mode == "L" else "rgb24"
    size = width * height * len(mode)
    command = [ffmpeg, "-nostdin", "-v", "error", "-i", str(source), "-map", "0:v:0", "-an",
               "-vf", f"scale={width}:{height},format={pixel_format}", "-fps_mode", "passthrough",
               "-f", "rawvideo", "pipe:1"]
    with subprocess.Popen(command, stdout=subprocess.PIPE) as process:
        try:
            yield iter(lambda: process.stdout.read(size), b"")
            if process.wait() != 0:
                raise ValueError("reference scan could not decode the source stream")
        finally:
            if process.poll() is None:
                process.kill()


def _candidate_ranges(times, diffs, flashes, frame_ms):
    starts = []
    for i in range(1, len(times)):
        window = diffs[bisect_left(times, times[i] - 500, 1, i):i]
        local = median(window) if window else 0
        if diffs[i] > max(5, 2.5 * local + 1) and (not starts or times[i] - times[starts[-1]] >= 199.5):
            starts.append(i)
    groups = []
    for index in sorted(set(starts) | flashes):
        if not groups or times[index] - times[groups[-1][-1]] > 500:
            groups.append([])
        groups[-1].append(index)
    pieces = []
    pending = groups[::-1]
    while pending:
        group = pending.pop()
        if times[group[-1]] - times[group[0]] <= 3000:
            pieces.append(group)
        else:
            split = max(range(1, len(group)), key=lambda i: times[group[i]] - times[group[i - 1]])
            pending.extend((group[split:], group[:split]))
    ranges = []
    start_set = set(starts)
    pad_frames = max(1, int(250 / frame_ms + 1e-6))
    for group in pieces:
        if len(start_set.intersection(group)) < 3 and flashes.isdisjoint(group):
            continue
        left = max(0, group[0] - pad_frames)
        right = min(len(times), group[-1] + pad_frames)
        if ranges and left <= ranges[-1][1]:
            ranges[-1] = (ranges[-1][0], max(right, ranges[-1][1]))
        else:
            ranges.append((left, right))
    return ranges, start_set


def _appearance(rgb):
    colors = Counter()
    coarse = Counter()
    avg = [0] * (32 * 18 * 3)
    for pixels in rgb:
        for i, (r, g, b) in enumerate(zip(pixels[0::3], pixels[1::3], pixels[2::3], strict=True)):
            colors[(r >> 4, g >> 4, b >> 4)] += 1
            coarse[(r >> 5, g >> 5, b >> 5)] += 1
            avg[3*i] += r
            avg[3*i+1] += g
            avg[3*i+2] += b
    total = len(rgb) * 32 * 18
    palette = [{"hex": "#%02x%02x%02x" % tuple(16*v + 8 for v in color), "share": round(n / total, 3)}
               for color, n in sorted(colors.items(), key=lambda p: (-p[1], p[0]))[:4]]
    return {k: n / total for k, n in coarse.items()}, [v / (len(rgb)*255) for v in avg], palette


def _distance(a, b):
    hist = sum(abs(a["hist"].get(k, 0) - b["hist"].get(k, 0)) for k in a["hist"].keys() | b["hist"].keys())
    thumb = sum(abs(x-y) for x, y in zip(a["thumb"], b["thumb"], strict=True)) / len(a["thumb"])
    return hist / 4 + 2 * thumb


def scan_reference(source: Path, *, duration_ms: int, check_active, ffmpeg="ffmpeg", limit=8):
    """Return up to eight bounded segments. All labels come from decoded presentation timestamps."""
    probe = json.loads(subprocess.check_output([
        "ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
        "format=start_time:stream=width,height,duration,r_frame_rate:frame=best_effort_timestamp_time,duration_time",
        "-of", "json", str(source),
    ]))
    stream = probe["streams"][0]
    frames = probe["frames"]
    # FFmpeg's normal input clock subtracts the container start time.
    if not frames:
        return []
    origin = float(probe["format"].get("start_time", frames[0]["best_effort_timestamp_time"]))
    times = [round((float(frame["best_effort_timestamp_time"]) - origin) * 1000) for frame in frames]
    if times != sorted(set(times)):
        raise ValueError("reference stream requires distinct, ordered millisecond presentation timestamps")
    if len(times) < 3:
        return []
    num, den = map(int, stream["r_frame_rate"].split("/"))
    frame_ms = 1000 * den / num if num else median(b-a for a,b in zip(times, times[1:]))
    end = min(duration_ms, times[-1] + max(1, round(float(frames[-1].get("duration_time", 0))*1000 or frame_ms)))
    if end <= times[-1]:
        raise ValueError("reference timestamps exceed the source duration")
    height = max(2, round(stream["height"] * 160 / stream["width"] / 2) * 2)
    diffs, flashes = [], set()
    previous = previous2 = None
    with _raw_frames(source, ffmpeg, 160, height, "L") as raw:
        for index, pixels in enumerate(raw):
            if index % 128 == 0:
                check_active()
            frame = Image.frombytes("L", (160, height), pixels)
            change = ImageStat.Stat(ImageChops.difference(frame, previous)).mean[0] if previous else 0
            if previous2 and change > 8 and diffs[-1] > 8:
                if ImageStat.Stat(ImageChops.difference(frame, previous2)).mean[0] < .35 * min(change, diffs[-1]):
                    flashes.add(index-1)
            diffs.append(change)
            previous2, previous = previous, frame
    if len(diffs) != len(times):
        raise ValueError("source clock and grayscale decode differ")
    ranges, starts = _candidate_ranges(times, diffs, flashes, frame_ms)
    if not ranges:
        return []
    edges = [*times, end]
    candidates = []
    with _raw_frames(source, ffmpeg, 32, 18, "RGB") as raw:
        features = []
        for i, pixels in enumerate(raw):
            if i % 128 == 0:
                check_active()
            if len(candidates) == len(ranges):
                continue
            left, right = ranges[len(candidates)]
            if left <= i < right:
                features.append(pixels)
            if i + 1 != right:
                continue
            span = range(left, right)
            dur = (edges[right] - edges[left]) / 1000
            count = len(starts.intersection(span))
            score = count / dur * (1 if .8 <= dur <= 3 else .5) + 2 * len(flashes.intersection(span))
            hist, thumb, palette = _appearance(features)
            if right - left > 90:
                right = left + 90
                _, _, palette = _appearance(features[:90])
            span = range(left, right)
            threshold = max(8, 4 * median(diffs[left:right]))
            candidates.append({"hist": hist, "thumb": thumb, "score": score, "segment": {
                "segment_id": f"seg-{edges[left]:07d}", "start_ms": edges[left], "end_ms": edges[right],
                "first_frame": left, "frame_ms": times[left:right], "fps": 1000 / frame_ms,
                "frame_delta": [round(x, 2) for x in diffs[left:right]], "fine_starts_ms": [times[j] for j in range(left+1, right) if diffs[j] > threshold],
                "flashes_ms": [times[j] for j in sorted(flashes.intersection(span))], "palette": palette,
            }})
            features = []
    if len(candidates) != len(ranges):
        raise ValueError("source clock and colour decode differ")
    selected = [max(candidates, key=lambda c: c["score"])]
    while len(selected) < min(limit, len(candidates)):
        selected.append(max((c for c in candidates if c not in selected),
                            key=lambda c: c["score"] * min(_distance(c, other) for other in selected)))
    return sorted((item["segment"] for item in selected), key=lambda s: s["start_ms"])
