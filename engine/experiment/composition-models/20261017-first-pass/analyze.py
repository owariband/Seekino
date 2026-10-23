"""Compare frozen illustrative triplets; never train or tune on these labels."""
import hashlib
import json
import statistics
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageOps

ROOT = Path(__file__).resolve().parent
manifest = json.loads((ROOT / "manifest.json").read_text())
reference = json.loads((ROOT / "reference.json").read_text())
manifest_hash = hashlib.sha256((ROOT / "manifest.json").read_bytes()).hexdigest()
ids = [s["id"] for s in manifest["samples"]]
labels = reference["labels"]
models = []


def load(path):
    data = json.loads((ROOT / path).read_text())
    assert data["manifest_sha256"] == manifest_hash
    assert [r["id"] for r in data["records"]] == ids
    return data


def compare(name, title, data, vectors, description, source):
    matrix = np.asarray(vectors, dtype=np.float64)
    assert matrix.shape[0] == len(ids) and np.isfinite(matrix).all()
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    matrix = np.divide(matrix, norms, out=np.zeros_like(matrix), where=norms > 0)
    sims = matrix @ matrix.T
    rankings = {}
    for i, sid in enumerate(ids):
        if norms[i, 0] == 0:
            rankings[sid] = []
            continue
        rankings[sid] = [{"id": ids[j], "similarity": float(sims[i, j]),
                          "shared_reference_layout": bool(set(labels[sid]["layouts"]) & set(labels[ids[j]]["layouts"])),
                          "tie_group_size": int(sum(abs(sims[i, j]-sims[i, k]) <= 1e-8 for k in range(len(ids)) if k != i and norms[k, 0] > 0))}
                         for j in sorted((j for j in range(len(ids)) if j != i and norms[j, 0] > 0), key=lambda j: -sims[i, j])]
    triples = []
    for t in reference["triplets"]:
        qi, pi, ni = [ids.index(t[k]) for k in ["query", "positive", "negative"]]
        positive, negative = float(sims[qi, pi]), float(sims[qi, ni])
        margin = positive - negative
        valid = all(norms[i, 0] > 0 for i in [qi, pi, ni])
        triples.append({**t, "positive_similarity": positive, "negative_similarity": negative,
                        "margin": margin, "valid": valid,
                        "unavailable_reason": None if valid else "At least one image has no nonzero feature",
                        "win": valid and margin > 1e-8, "tie": valid and abs(margin) <= 1e-8})
    main = [t for t in triples if t["split"] == "main"]
    boundary = [t for t in triples if t["split"] == "boundary"]
    records = {r["id"]: {k: v for k, v in r.items()
                          if k not in ["feature", "penultimate_feature", "embedding", "measurements"]}
               for r in data["records"]}
    item = {"id": name, "label": title, "dimension": matrix.shape[1],
            "device": data.get("device", data.get("runtime", "cpu")),
            "median_ms": data.get("median_pipeline_ms", statistics.median(
                r.get("median_pipeline_ms", r.get("pipeline_ms", r.get("detect_including_grayscale_ms", 0))) for r in data["records"])),
            "description": description, "source_file": source, "records": records,
            "rankings": rankings, "triplets": triples,
            "zero_vectors": [ids[i] for i, n in enumerate(norms[:, 0]) if n == 0],
            "main_wins": sum(t["win"] for t in main), "main_ties": sum(t["tie"] for t in main),
            "main_count": len(main), "main_valid_count": sum(t["valid"] for t in main),
            "main_unavailable_count": sum(not t["valid"] for t in main),
            "boundary_wins": sum(t["win"] for t in boundary),
            "boundary_count": len(boundary),
            "valid_query_count": sum(bool(v) for v in rankings.values()),
            "mean_reference_overlap_at_3": float(np.mean([sum(v["shared_reference_layout"] for v in ranks[:3])/min(3, len(ranks)) for ranks in rankings.values() if ranks]))}
    models.append(item)
    return item


pcp = load("pcp/results.json")
compare("pcp_classes", "PCP · 9类构图分布", pcp,
        [[r["class_probabilities"][c] for c in pcp["class_order"]] for r in pcp["records"]],
        "官方摄影构图分类器；9类 softmax 分布的余弦。置信度未经动画域校准。", "pcp/results.json")
compare("pcp_fc7", "PCP · fc7 特征", pcp, [r["feature"] for r in pcp["records"]],
        "同一 PCP 模型的4096维中间特征；不能等同类别分布。", "pcp/results.json")
samp = load("samp/results.json")
compare("samp_fused", "SAMP-Net · 融合特征", samp, [r["feature"] for r in samp["records"]],
        "1024维 AAFF 融合后评分头输入。原任务是构图质量评分。", "samp/results.json")
compare("samp_penultimate", "SAMP-Net · 评分前特征", samp, [r["penultimate_feature"] for r in samp["records"]],
        "512维最终5档评分线性层前 ReLU 特征；辅助取层对照。", "samp/results.json")
for kind, title in [("clip", "CLIP ViT-B/32"), ("dinov2", "DINOv2 Small")]:
    for variant, note in [("native", "默认中心裁剪"), ("fullframe", "保留全画面")]:
        path = f"backbones/{kind}__{variant}.json"
        data = load(path)
        compare(f"{kind}_{variant}", f"{title} · {note}", data,
                [r["embedding"] for r in data["records"]],
                data["preprocessing"] + "; normalized visual embedding cosine", path)
musiq = load("backbones/musiq_ava.json")
compare("musiq_ava", "MUSIQ AVA · 美学特征", musiq, [r["embedding"] for r in musiq["records"]],
        "AVA美学权重；384维 transformer CLS，评分头前。美学分不是构图类别。", "backbones/musiq_ava.json")
lines = load("lines/results.json")
compare("lsd", "LSD · 线段方向基线", lines, [r["embedding"] for r in lines["records"]],
        "传统算法，无学习权重。最长12线段的12bin方向直方图；无位置、主次及语义。", "lines/results.json")

grounding = load("backbones/grounding_dino.json")
overlays = ROOT / "backbones/grounding_dino_overlays"
overlays.mkdir(exist_ok=True)
font = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial.ttf", 16)
for sample, row in zip(manifest["samples"], grounding["records"]):
    image = Image.open(ROOT / sample["image_path"]).convert("RGB")
    draw = ImageDraw.Draw(image)
    for detection in row["detections"]:
        box = [v*image.size[j % 2] for j, v in enumerate(detection["box_xyxy_normalized"])]
        color = "#ff4444" if detection["label"] == "person" else "#00bfff"
        draw.rectangle(box, outline=color, width=3)
        xy = (max(0, box[0]), max(0, box[1]-19))
        text = f"{detection['label']} {detection['score']:.2f}"
        draw.rectangle(draw.textbbox(xy, text, font=font), fill="black")
        draw.text(xy, text, font=font, fill=color)
    image.save(overlays / f"{sample['id']}.jpg", quality=94)
native_outputs = {}
for i, sid in enumerate(ids):
    native_outputs[sid] = {
        "pcp": {k: pcp["records"][i][k] for k in ["top_class", "class_probabilities"]},
        "samp": {k: samp["records"][i][k] for k in ["mean_composition_quality", "score_distribution_1_to_5", "pattern_importance_weights"]},
        "musiq": {k: musiq["records"][i][k] for k in ["aesthetic_score_1_to_10", "score_distribution"]},
        "grounding": {"detections": grounding["records"][i]["detections"], "overlay_path": f"backbones/grounding_dino_overlays/{sid}.jpg"},
        "lines": {k: lines["records"][i][k] for k in ["selected_count", "overlay_path"]}}
report = {"metadata": {"generated_at_utc": datetime.now(timezone.utc).isoformat(),
                       "manifest_sha256": manifest_hash,
                       "reference_sha256": hashlib.sha256((ROOT / "reference.json").read_bytes()).hexdigest(),
                       "scope": "17 selected images from one anime; assistant visual reference, not human expert ground truth; 10 exploratory main triplets +3 boundary cases; correlated cases, no statistical winner claim.",
                       "reference_freeze_note": "Candidate visual labels were reviewed before learned-model rankings. The reference file/triplets were materialized after initial SAMP/LSD extraction; remaining backbone/PCP inference followed. This is exploratory, not a blinded preregistered benchmark.",
                       "retrieval": "L2-normalized feature cosine, exclude self and zero-vector candidates; zero-vector queries unavailable. Ties within 1e-8 shown; display order within ties follows manifest and is not a preference. No trained metric or tuned mixture. Every layer/preprocessing variant reported.",
                       "timing": "CPU SAMP/PCP/LSD and MPS CLIP/DINOv2/MUSIQ/Grounding; one-pass backbone vs repeated CPU timing; not a head-to-head speed benchmark.",
                       "remote_api_models_included": False},
          "samples": [{**s, **labels[s["id"]]} for s in manifest["samples"]],
          "models": models, "native_outputs": native_outputs}
(ROOT / "comparison.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
print(json.dumps([{"model": m["label"], "wins": m["main_wins"], "ties": m["main_ties"],
                   "n": m["main_count"], "boundary": m["boundary_wins"], "P3_reference": round(m["mean_reference_overlap_at_3"], 3)} for m in models], ensure_ascii=False, indent=2))

for query in ["aquarium", "interior", "stage_left"]:
    chosen = [m for m in models if m["id"] in ["pcp_classes", "pcp_fc7", "clip_fullframe", "dinov2_fullframe", "samp_fused", "musiq_ava", "lsd"]]
    sheet = Image.new("RGB", (1120, len(chosen)*195+30), "#f3f4f6")
    draw = ImageDraw.Draw(sheet)
    draw.text((10, 5), f"Query: {query} | columns: query, rank 1, rank 2, rank 3", font=font, fill="black")
    for row, model in enumerate(chosen):
        y = 30+row*195
        draw.text((8, y), model["id"], font=font, fill="black")
        candidates = [(query, None)] + [(v["id"], v["similarity"]) for v in model["rankings"][query][:3]]
        for col, (sid, score) in enumerate(candidates):
            sample = manifest["samples"][ids.index(sid)]
            im = ImageOps.contain(Image.open(ROOT/sample["image_path"]), (272, 150))
            sheet.paste(im, (col*280+(280-im.width)//2, y+20))
            label = sid if score is None else f"{sid}: {score:.3f}"
            draw.text((col*280+5, y+172), label, font=font, fill="black")
    sheet.save(ROOT/f"retrieval_{query}.jpg", quality=94)
