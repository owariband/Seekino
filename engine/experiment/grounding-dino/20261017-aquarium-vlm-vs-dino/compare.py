"""Compare frozen assistant person boxes with DINO; neither is ground truth."""

import hashlib
import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

root = Path(__file__).resolve().parent
freeze = json.loads((root / "freeze.json").read_text())
for name, sha in freeze["files"].items():
    assert hashlib.sha256((root / name).read_bytes()).hexdigest() == sha
assistant = json.loads((root / "assistant_annotation.json").read_text())
results = json.loads((root / "results.json").read_text())
assert results["inference_started_utc"] > freeze["frozen_at_utc"]
width, height = assistant["image_size"]


def iou(a, b):
    intersection = max(0, min(a[2], b[2]) - max(a[0], b[0])) * max(0, min(a[3], b[3]) - max(a[1], b[1]))
    area_a = (a[2] - a[0]) * (a[3] - a[1])
    area_b = (b[2] - b[0]) * (b[3] - b[1])
    return intersection / (area_a + area_b - intersection)


comparisons = []
for case in results["records"]:
    people = [(i, d) for i, d in enumerate(case["detections"]) if d["label"] == "person"]
    pairs = sorted([(iou(p["box_xyxy"], d["box_xyxy"]), pi, di)
                    for pi, p in enumerate(assistant["people"]) for di, d in people], reverse=True)
    used_assistant, used_dino, matches = set(), set(), []
    for overlap, pi, di in pairs:
        if overlap < 0.5 or pi in used_assistant or di in used_dino:
            continue
        used_assistant.add(pi)
        used_dino.add(di)
        person, detection = assistant["people"][pi], case["detections"][di]
        a, b = person["box_xyxy"], detection["box_xyxy"]
        matches.append({"assistant_id": person["id"], "dino_detection_index": di,
                        "dino_score": detection["score"], "iou": overlap,
                        "assistant_box": a, "dino_box": b,
                        "center_delta_xy_percentage_points": [100 * ((b[0]+b[2]-a[0]-a[2])/2)/width,
                                                               100 * ((b[1]+b[3]-a[1]-a[3])/2)/height]})
    comparisons.append({"case": case["id"], "dino_person_box_count": len(people),
                        "matches": sorted(matches, key=lambda m: m["assistant_id"]),
                        "unmatched_assistant_ids": [p["id"] for i, p in enumerate(assistant["people"]) if i not in used_assistant],
                        "unmatched_dino_indices": [i for i, _ in people if i not in used_dino]})
output = {"method": "Frozen assistant visual estimates vs DINO outputs, not ground-truth evaluation. Greedy highest-IoU one-to-one person matching with IoU>=0.5. DINO boxes do not contain native line endpoints; no line accuracy metric or line derivation fabricated.",
          "frozen_files_verified": freeze["files"], "assistant_frozen_at_utc": freeze["frozen_at_utc"],
          "dino_started_at_utc": results["inference_started_utc"], "comparisons": comparisons}
(root / "comparison.json").write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n")
font = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial.ttf", 28)
cell_width, cell_height = width // 2, height // 2
canvas = Image.new("RGB", (cell_width * 2, cell_height + 70), "#151a22")
draw = ImageDraw.Draw(canvas)
for index, (filename, title) in enumerate([
    ("assistant_annotation.png", "A: Assistant BEFORE DINO | 6 lines + 2 person boxes"),
    ("scene_structures.jpg", "B: DINO | scene_structures | raw boxes, no lines"),
]):
    with Image.open(root / filename) as im:
        canvas.paste(im.convert("RGB").resize((cell_width, cell_height), Image.Resampling.LANCZOS), (index * cell_width, 70))
    draw.text((index * cell_width + 16, 20), title, font=font, fill="white")
canvas.save(root / "comparison.jpg", quality=95)
print(json.dumps(comparisons, ensure_ascii=False, indent=2))
