"""Replay frozen speaker windows and export acoustic-change figures.

python -m evals.speaker.speaker_change --manifest <manifest.json> --out <new-dir>
No model calls, clustering, ASR, database writes or production changes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import time

import numpy as np

from engine.parsev2 import speaker_change
from server.constants.parse import SPEAKER_CONSISTENCY_PARAMETERS


PRIMARY = dict(SPEAKER_CONSISTENCY_PARAMETERS)
PAPER = "https://ccrma.stanford.edu/workshops/mir2009/references/Foote_00.pdf"


def render(case, result, output):
    import matplotlib.pyplot as plt

    matrix = np.asarray(result["matrix"])
    if not len(matrix):
        return
    starts = np.array([w["start_ms"] for w in case["windows"]]) / 1000
    half_hop = np.median(np.diff(starts)) / 2 if len(starts) > 1 else 0.2
    extent = (starts[0] - half_hop, starts[-1] + half_hop,
              starts[-1] + half_hop, starts[0] - half_hop)
    fig, (heat, curve) = plt.subplots(2, 1, figsize=(11, 10), height_ratios=(3, 1.3),
                                     layout="constrained")
    image = heat.imshow(matrix, vmin=0, vmax=1, cmap="Blues", extent=extent, aspect="auto")
    fig.colorbar(image, ax=heat, label="cosine")
    heat.set(xlabel="窗口起点 / 秒（每窗 1 秒）", ylabel="窗口起点 / 秒")
    fig.suptitle(f"{case['id']}  ·  {case['start_ms']/1000:g}–{case['end_ms']/1000:g}s\n"
                 "1 秒窗口 · 400ms 步长 · 红／橙色区域需要重新核对", fontsize=15)
    for key, label, color in (("contrast", "两侧内部 − 跨侧", "#1269a8"),
                              ("edge_contrast", "短侧支持不足时的差异", "#da8500")):
        curve.plot([r["time_ms"] / 1000 for r in result["scans"]],
                   [r[key] for r in result["scans"]], ".-", color=color, label=label)
    curve.plot([r["time_ms"] / 1000 for r in result["foote"]],
               [2 * r["novelty"] for r in result["foote"]], "--", color="#777777",
               label="Foote 核曲线 × 2（对照，包含共享音频）")
    for key, color in (("contrast_threshold", "#1269a8"), ("edge_threshold", "#da8500")):
        curve.axhline(result["parameters"][key], color=color, linestyle=":", alpha=0.7)
    for region in result["candidates"]:
        left, right = region["start_ms"] / 1000, region["end_ms"] / 1000
        color = "#c13232" if region["kind"] == "block_change" else "#da8500"
        heat.axvspan(left, right, color=color, alpha=0.13)
        heat.axhspan(left, right, color=color, alpha=0.13)
        curve.axvspan(left, right, color=color, alpha=0.15)
        curve.axvline(region["peak_ms"] / 1000, color=color, linewidth=1)
    curve.axhline(0, color="#999999", linewidth=0.6)
    curve.set(xlabel="原视频时间 / 秒", ylabel="反差（不是概率）",
              xlim=(case["start_ms"] / 1000, case["end_ms"] / 1000))
    curve.legend(loc="best", fontsize=8)
    ranges = "；".join(f"{r['start_ms']/1000:.1f}–{r['end_ms']/1000:.1f}s"
                      for r in result["candidates"]) or "无候选"
    fig.supxlabel(f"复核范围：{ranges}\n空缺表示支持不足；无候选不等于确认单人。", fontsize=11)
    fig.savefig(output / f"{case['id']}.png", dpi=160)
    plt.close(fig)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    for key, value in PRIMARY.items():
        parser.add_argument("--" + key.replace("_", "-"), type=type(value), default=value)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("MPLCONFIGDIR", str(args.out / "mpl-cache"))
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams["font.family"] = ["Heiti TC", "Arial Unicode MS", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False
    content = args.manifest.read_bytes()
    manifest = json.loads(content)
    parameters = {key: getattr(args, key) for key in PRIMARY}
    variants = [("primary", parameters)]
    variants += [(f"contrast_{value:g}", {**parameters, "contrast_threshold": value})
                 for value in (0.05, 0.15, 0.20)]
    variants += [(f"edge_{value:g}", {**parameters, "edge_threshold": value})
                 for value in (0.10, 0.20)]
    protocol = {
        "manifest_sha256": hashlib.sha256(content).hexdigest(),
        "algorithm_sha256": hashlib.sha256(Path(speaker_change.__file__).read_bytes()).hexdigest(),
        "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "parameters": parameters, "sensitivity_profiles": dict(variants), "paper": PAPER,
        "scope": "exploratory candidate detection; no calibrated speaker probability or automatic split",
        "labels": "two known phrase identities; ordinary controls use general user feedback, not turn annotation",
    }
    protocol_path = args.out / "protocol.json"
    encoded_protocol = json.dumps(protocol, ensure_ascii=False, indent=2) + "\n"
    if protocol_path.exists() and protocol_path.read_text() != encoded_protocol:
        raise ValueError("Use a new output directory for a changed protocol or implementation")
    # Write the protocol before evaluating any sample or control.
    protocol_path.write_text(encoded_protocol)
    started = time.monotonic()
    results, sensitivity = [], []
    revision = None
    for case in manifest["cases"]:
        if revision is not None and revision != case["encoder_revision"]:
            raise ValueError("All cases must use one frozen encoder")
        revision = case["encoder_revision"]
        path = args.manifest.parent / case["embedding_path"]
        if hashlib.sha256(path.read_bytes()).hexdigest() != case["embedding_sha256"]:
            raise ValueError(f"Embedding file changed: {case['id']}")
        with np.load(path, allow_pickle=False) as archive:
            vectors = archive["embeddings"]
            assert vectors.shape == (len(case["windows"]), 192)
            np.testing.assert_array_equal(archive["windows_ms"],
                                          [[w["start_ms"], w["end_ms"]] for w in case["windows"]])
        for name, config in variants:
            result = speaker_change.detect_speaker_changes(
                case["windows"], vectors, start_ms=case["start_ms"], end_ms=case["end_ms"], **config,
            )
            if name == "primary":
                results.append({key: value for key, value in case.items() if key != "windows"}
                               | {"window_count": len(case["windows"]), "result": result})
                render(case, result, args.out)
            sensitivity.append({"case_id": case["id"], "profile": name,
                                "candidates": result["candidates"]})
    output = {"protocol": protocol, "cases": results, "sensitivity": sensitivity,
              "elapsed_seconds": time.monotonic() - started}
    (args.out / "results.json").write_text(json.dumps(output, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    fig, axes = plt.subplots(len(results), 1, figsize=(12, len(results) * 0.82 + 1),
                             layout="constrained", squeeze=False)
    lines = ["# EP04 声学变化候选：冻结参数回放", "",
             "候选供 Understanding 重 ASR 复核；不会据此自动断言人数、姓名或单人纯度。",
             "普通对照依据用户一般性反馈；未取得逐段换人／重叠边界，触发数不能直接称为误报率。",
             f"Foote 核原理：[原论文]({PAPER})；masked median 和短边缘分支为项目扩展。", "",
             "| 样本 | 原范围 / 秒 | 候选复核范围 / 秒 |", "| --- | --- | --- |"]
    for row, ax in zip(results, axes[:, 0], strict=True):
        left, right = row["start_ms"] / 1000, row["end_ms"] / 1000
        regions = row["result"]["candidates"]
        ax.plot([left, right], [0, 0], color="#c8d0d6", linewidth=12, solid_capstyle="butt")
        for region in regions:
            color = "#c13232" if region["kind"] == "block_change" else "#da8500"
            ax.plot([region["start_ms"] / 1000, region["end_ms"] / 1000], [0, 0],
                    color=color, linewidth=12, solid_capstyle="butt")
        ax.set(xlim=(left, right), ylim=(-1, 1), yticks=[], xticks=np.linspace(left, right, 5))
        ax.set_title(f"{row['id']}  ·  {row['window_count']} 窗  ·  {len(regions)} 个复核范围",
                     loc="left", fontsize=10)
        ax.tick_params(axis="x", labelsize=8)
        for spine in ax.spines.values():
            spine.set_visible(False)
        ranges = "、".join(f"{r['start_ms']/1000:.1f}–{r['end_ms']/1000:.1f}"
                          for r in regions) or "无"
        lines.append(f"| {row['id']} | {left:g}–{right:g} | {ranges} |")
        print(json.dumps({"case": row["id"], "candidates": regions}, ensure_ascii=False))
    fig.suptitle(f"EP04 全部 {len(results)} 段回放 · 红：持续块变化候选 · 橙：支持有限的差异\n"
                 "灰：未触发复核；不代表已经确认单人。横轴均为原视频秒数。", fontsize=13)
    fig.savefig(args.out / "overview.png", dpi=160)
    plt.close(fig)
    (args.out / "report.md").write_text("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
