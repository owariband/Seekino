"""Compare fixed PCM cohorts, keeping original registry labels and both score definitions."""
from __future__ import annotations

import csv
from datetime import UTC, datetime
import hashlib
import json
import math
from pathlib import Path
import statistics

HERE = Path(__file__).resolve().parent
MODELS = {
    "campp_baseline": "CAM++（冻结基线）",
    "eres2netv2": "ERes2NetV2 common",
    "redimnet2_vox2": "ReDimNet2-B6 VoxCeleb2 LM",
    "redimnet2_mixed": "ReDimNet2-B6 mixed LM",
    "wavlm_large": "WavLM-Large SV",
}


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def cosine(a, b):
    return math.fsum(x * y for x, y in zip(a, b)) / math.hypot(*a) / math.hypot(*b)


def compare(target, references, embeddings, excluded_ids=()):
    valid = [sample for sample in references if sample["id"] != target["id"]
             and sample["id"] not in excluded_ids and sample["pcm_sha256"] != target["pcm_sha256"]
             and max(sample["start_ms"], target["start_ms"]) >= min(sample["end_ms"], target["end_ms"])]
    rows = []
    vector = embeddings[target["id"]]["vector"]
    for speaker in sorted({sample["speaker"] for sample in valid}):
        selected = [sample for sample in valid if sample["speaker"] == speaker]
        vectors = [embeddings[sample["id"]]["vector"] for sample in selected]
        scores = [cosine(vector, other) for other in vectors]
        center = [math.fsum(values) / len(vectors) for values in zip(*vectors)]
        rows.append({"speaker": speaker, "count": len(selected), "mean_pairwise_cosine": math.fsum(scores) / len(scores),
                     "normalized_centroid_cosine": cosine(vector, center), "mean_reference_vector_norm": math.hypot(*center),
                     "individual_scores": [{"reference_id": sample["id"], "cosine": score} for sample, score in zip(selected, scores)]})
    rankings = {}
    for metric in ("mean_pairwise_cosine", "normalized_centroid_cosine"):
        ordered = sorted(rows, key=lambda row: (-row[metric], row["speaker"]))
        expected = next((i + 1 for i, row in enumerate(ordered) if row["speaker"] == target["known_actual_speaker"]), None)
        rankings[metric] = {"top1": ordered[0]["speaker"], "top1_score": ordered[0][metric],
                            "top1_top2_margin": ordered[0][metric] - ordered[1][metric] if len(ordered) > 1 else None,
                            "known_actual_rank": expected, "ordered_speakers": [row["speaker"] for row in ordered]}
    return {"target_id": target["id"], "cohort": target["cohort"], "start_ms": target["start_ms"], "end_ms": target["end_ms"],
            "registered_speaker": target["speaker"], "known_actual_speaker": target["known_actual_speaker"],
            "pcm_sha256": target["pcm_sha256"], "candidates": rows, "rankings": rankings}


def main():
    manifest = json.loads((HERE / "manifest.json").read_text())
    samples = manifest["samples"]
    expected_ids = {sample["id"] for sample in samples}
    targets = [sample for sample in samples if sample["known_actual_speaker"]]
    excluded = {sample["id"] for sample in targets if sample["cohort"] == "v11"}
    results = {}
    (HERE / "matrices").mkdir(exist_ok=True)
    for key, label in MODELS.items():
        path = HERE / key / "embeddings.json"
        status_path = HERE / key / "status.json"
        status = json.loads(status_path.read_text()) if status_path.exists() else {}
        state = status.get("status", status.get("state"))
        if not path.exists() or (state is not None and state != "complete"):
            results[key] = {"label": label, "status": {**status, "status": state or "not_ready"}}
            continue
        raw = json.loads(path.read_text())
        records = raw["embeddings"] if isinstance(raw, dict) else raw
        if len(records) != len(samples):
            results[key] = {"label": label, "status": {"status": "partial", "count": len(records)}}
            continue
        embeddings = {row["id"]: row for row in records}
        assert set(embeddings) == expected_ids
        dimensions = {len(row["vector"]) for row in records}
        assert len(dimensions) == 1
        for sample in samples:
            row = embeddings[sample["id"]]
            assert row["pcm_sha256"] == sample["pcm_sha256"]
            assert all(math.isfinite(x) for x in row["vector"])
            assert abs(math.hypot(*row["vector"]) - 1) < 1e-5
        probes, sensitivity = [], []
        for target in targets:
            refs = [sample for sample in samples if sample["cohort"] == target["cohort"] and sample["target_role"] == "reference"]
            probes.append(compare(target, refs, embeddings))
            if target["cohort"] == "v11":
                sensitivity.append(compare(target, refs, embeddings, excluded))
        v11 = [sample for sample in samples if sample["cohort"] == "v11"]
        leave_one_out = [compare(target, v11, embeddings) for target in v11]
        for cohort in ("v11", "balanced"):
            selected = [sample for sample in samples if sample["cohort"] == cohort]
            with (HERE / "matrices" / f"{key}-{cohort}.csv").open("w", newline="") as stream:
                writer = csv.writer(stream)
                writer.writerow(["sample_id", *(sample["id"] for sample in selected)])
                for a in selected:
                    writer.writerow([a["id"], *("" if a["id"] == b["id"] else cosine(embeddings[a["id"]]["vector"], embeddings[b["id"]]["vector"]) for b in selected)])
        timings = [row["embedding_ms"] for row in records if "embedding_ms" in row]
        provenance_path = HERE / key / "provenance.json"
        provenance = json.loads(provenance_path.read_text()) if provenance_path.exists() else raw.get("provenance") if isinstance(raw, dict) else None
        results[key] = {"label": label, "status": {"status": "complete"}, "dimension": next(iter(dimensions)),
                        "embedding_file_sha256": digest(path), "provenance": provenance,
                        "timing": {"count": len(timings), "median_ms": statistics.median(timings) if timings else None,
                                   "total_ms": math.fsum(timings) if timings else None,
                                   "note": "Frozen CAM++ vectors have no timing in this run; model runners may include first-call cold work."},
                        "known_targets_original_labels": probes,
                        "sensitivity_excluding_two_known_wrong_reference_sources": sensitivity,
                        "v11_leave_one_out": leave_one_out}
    report = {"created_at": datetime.now(UTC).isoformat(), "manifest_sha256": digest(HERE / "manifest.json"),
              "sample_count": len(samples), "models": results,
              "method": "Each model re-embeds the same PCM; cosine mean and normalized-centroid cosine are reported separately. Cohorts never mix. Exclude target self, overlapping audio and duplicate PCM from enrollment.",
              "limits": ["Original registry labels are preserved, including two known incorrect identities.",
                         "Only three probe conditions have user-confirmed identities; two are boundary/reference variants of the same Soyo utterance.",
                         "Other sample labels are prior parse labels, not independently verified ground truth.",
                         "Scores are not probabilities or comparable absolute calibration across models; no global accuracy estimate.",
                         "CPU timings are offline process measurements, not production concurrency benchmarks."]}
    (HERE / "results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    lines = ["# 第四集固定音频：声纹编码器对照", "",
             "输入共47段：v11登记31段，旧balanced参考15段及1段目标。全部WAV的PCM与历史样本一致，参考池分开。模型、向量和来源详见JSON；未修改正式声纹库、speaker、Gateway或解析任务。", "",
             "## 已确认目标的逐条cosine均值", "",
             "每行分别使用该条件原有参考，排除目标自身、重复和重叠。v11仍保留原库其他错标，不能把两组不同边界／来源的爽世条件混成同一实验。", "",
             "对单位向量，逐条均值为 `mean(dot(target, ref_i))`；质心分数为 `dot(target, normalize(mean(ref_i)))`，分开报告。没有套用 0.6 等新通过阈值。", "",
             "| 模型 | 目标 | 真实人物 | 正确人物均值 | 均值最高候选 | 最高均值 | 第二候选 | 第一减第二 | 正确人物排名 |",
             "|---|---|---|---:|---|---:|---|---:|---:|"]
    for key, model in results.items():
        if model["status"]["status"] != "complete":
            lines.append(f'| {model["label"]} | {model["status"]["status"]} | — | — | — | — | — | — | — |')
            continue
        for probe in model["known_targets_original_labels"]:
            expected = next(row for row in probe["candidates"] if row["speaker"] == probe["known_actual_speaker"])
            rank = probe["rankings"]["mean_pairwise_cosine"]
            lines.append(f'| {model["label"]} | {probe["cohort"]} {probe["start_ms"]/1000:.3f}–{probe["end_ms"]/1000:.3f}s | {probe["known_actual_speaker"]} | {expected["mean_pairwise_cosine"]:.6f} | {rank["top1"]} | {rank["top1_score"]:.6f} | {rank["ordered_speakers"][1]} | {rank["top1_top2_margin"]:.6f} | {rank["known_actual_rank"]} |')
    lines += ["", "v11 经典爽世条件中，两支 ReDimNet2 都把爽世排第一，但第二名乐奈仅 1 条参考，逐条均值差距分别只有 0.026857／0.007730。较高的绝对分数不表示更可靠；这个固定参考库尚不足以据此自动填写所有 speaker。", "",
              "## 同目标的归一化质心分数", "", "| 模型 | 目标 | 最高候选 | 质心cosine | 第一减第二 | 正确人物排名 |", "|---|---|---|---:|---:|---:|"]
    for model in results.values():
        for probe in model.get("known_targets_original_labels", []):
            rank = probe["rankings"]["normalized_centroid_cosine"]
            lines.append(f'| {model["label"]} | {probe["cohort"]} {probe["start_ms"]/1000:.3f}–{probe["end_ms"]/1000:.3f}s | {rank["top1"]} | {rank["top1_score"]:.6f} | {rank["top1_top2_margin"]:.6f} | {rank["known_actual_rank"]} |')
    lines += ["", "## 资源记录", "", "| 模型 | 输出维数 | 本轮提取数 | 提取中位ms | 合计ms |", "|---|---:|---:|---:|---:|"]
    for model in results.values():
        if model["status"]["status"] != "complete":
            continue
        timing = model["timing"]
        median = f'{timing["median_ms"]:.3f}' if timing["median_ms"] is not None else "冻结复用"
        total = f'{timing["total_ms"]:.3f}' if timing["total_ms"] is not None else "—"
        lines.append(f'| {model["label"]} | {model["dimension"]} | {timing["count"]} | {median} | {total} |')
    lines += ["", "计时均为本机 CPU、FP32、4 线程、batch=1，累计为 47 段前向；不含下载、依赖导入或 checkpoint 加载。ERes2NetV2 与 WavLM-Large 各有一次预热；ReDimNet2 包含首条冷调用。各模型 provenance 另存加载时间、首条时间和进程峰值 RSS，不能直接推算生产并发吞吐。CAM++ 复用冻结向量，本轮未重新计时。", ""]
    wavlm = results["wavlm_large"]
    if wavlm["status"]["status"] == "complete":
        provenance = wavlm["provenance"]
        lines += ["WavLM-Large 的专用 SV 权重为 1,301,926,579 字节，另外保留 1,261,965,425 字节的上游初始化 checkpoint 用于读取精确配置；最终编码器与 ECAPA 权重全部来自专用 SV 文件。模型加载 4.583 秒，首次预热 2.007 秒，进程峰值 RSS 约 2.843GB。权重和源码集中保存在 `outputs/local-models/`，本轮未安装新依赖。", "",
                  f'WavLM 官方 Google Drive 下载限额、另一个 OneDrive 链接 403，因此使用 [UniAudio 作者镜像]({provenance["checkpoint_url"]})。文件 SHA 与另外两处镜像一致，其中 [模型卡](https://huggingface.co/bezzam/wavlm_large_finetune_seed_tts_eval)明确指向微软同一专用 SV 文件；未能直接与不可下载的微软原件逐字节核对。复用官方 standalone WavLM 和 ECAPA，适配 s3prl 的 25 层 hooks；710 个模型状态键严格加载，唯一显式排除项为训练分类投影 `loss_calculator.projection.weight`。网络和后端没有随机缺失参数，兼容适配与失败日志均保留。已按源码核对适配语义，未另装旧 s3prl 全管线做数值等价复测。', ""]
    lines += [
              "## 原音频、向量与复现", "",
              "| 条件 | 原音频 | 真值依据 |", "|---|---|---|"]
    for sample in targets:
        lines.append(f'| {sample["id"]}：{sample["start_ms"]/1000:.3f}–{sample["end_ms"]/1000:.3f}s | [WAV]({sample["audio"]}) | 用户确认：{sample["known_actual_speaker"]} |')
    lines += ["", "[完整候选、逐条参考分数和来源](results.json) · [固定样本](manifest.json) · [PCM核验](preparation.json) · [独立复算](verification.json) · [本次文件清单](artifacts.md)", ""]
    for key, model in results.items():
        if model["status"]["status"] == "complete":
            lines.append(f'- {model["label"]}：[47 条原始向量]({key}/embeddings.json)' + (f'、[权重／源码／资源记录]({key}/provenance.json)' if key != "campp_baseline" else '；来源记录包含在向量文件中。'))
        else:
            lines.append(f'- {model["label"]}：{model["status"]["status"]}；不计入已完成推理。')
    lines += ["", "已有向量的比较与独立复算：", "", "```sh",
              ".venv/bin/python engine/experiment/speaker-encoder/20261018-mygo-ep04-model-comparison/compare.py",
              ".venv/bin/python engine/experiment/speaker-encoder/20261018-mygo-ep04-model-comparison/verify.py", "```", "",
              "`matrices/`保存每模型各参考池的完整成对分数，空白对角线不作为证据。各模型目录保存原始向量、来源版本与运行记录。", "",
              "只有两个不同台词具有用户确认真值，其中爽世有两个边界／参考条件；其余登记身份未全部人工审核。这里不估计整集准确率，不以绝对分数变大证明更好；未自动按top1写回。"]
    (HERE / "README.md").write_text("\n".join(lines) + "\n")
    print(json.dumps({key: model["status"] for key, model in results.items()}, ensure_ascii=False))


if __name__ == "__main__":
    main()
