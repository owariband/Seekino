"""Compare pinned official-path embeddings with the frozen earlier WavLM run."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import statistics
import sys

HERE = Path(__file__).resolve().parent
EXPERIMENT = HERE.parent
sys.path.insert(0, str(EXPERIMENT))
from compare import compare, cosine

CRITERIA = {"normalized_vector_max_abs": 1e-5, "vector_cosine_min": 1 - 1e-6,
            "within_cohort_pair_score_max_abs": 1e-5}


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def weak_label_auc(samples, embeddings):
    rows = []
    for cohort in ("v11", "balanced"):
        refs = [sample for sample in samples if sample["cohort"] == cohort
                and sample["target_role"] == "reference" and sample["id"] not in {"v11_02", "v11_27"}]
        positive, negative = [], []
        for index, left in enumerate(refs):
            for right in refs[index + 1:]:
                if (left["pcm_sha256"] == right["pcm_sha256"]
                        or max(left["start_ms"], right["start_ms"]) < min(left["end_ms"], right["end_ms"])):
                    continue
                score = cosine(embeddings[left["id"]]["vector"], embeddings[right["id"]]["vector"])
                (positive if left["speaker"] == right["speaker"] else negative).append(score)
        auc = math.fsum((same > different) + 0.5 * (same == different)
                        for same in positive for different in negative) / (len(positive) * len(negative))
        rows.append({"cohort": cohort, "references": len(refs), "same_label_pairs": len(positive),
                     "different_label_pairs": len(negative), "auc": auc,
                     "limitation": "Unverified registry labels; this is label agreement, not identity accuracy."})
    return rows


def main(run):
    status = json.loads((run / "status.json").read_text())
    assert status["status"] == "complete" and status["sample_count"] == 47
    manifest_path = EXPERIMENT / "manifest.json"
    samples = json.loads(manifest_path.read_text())["samples"]
    old_path, new_path = EXPERIMENT / "wavlm_large/embeddings.json", run / "embeddings.json"
    provenance = json.loads((run / "provenance.json").read_text())
    environment_path = EXPERIMENT.parents[3] / "outputs/local-models/wavlm-speaker-sv/official-pinned/environment.json"
    assert provenance["manifest_sha256"] == sha(manifest_path)
    old = {row["id"]: row for row in json.loads(old_path.read_text())}
    new = {row["id"]: row for row in json.loads(new_path.read_text())}
    assert set(old) == set(new) == {sample["id"] for sample in samples}
    differences = []
    for sample in samples:
        left, right = old[sample["id"]], new[sample["id"]]
        assert left["pcm_sha256"] == right["pcm_sha256"] == sample["pcm_sha256"]
        assert len(left["vector"]) == len(right["vector"]) == 256
        assert all(math.isfinite(value) for value in right["vector"])
        assert abs(math.hypot(*right["vector"]) - 1) < 1e-6
        differences.append({"id": sample["id"], "pcm_sha256": sample["pcm_sha256"],
                            "normalized_max_abs": max(abs(a - b) for a, b in zip(left["vector"], right["vector"])),
                            "vector_cosine": cosine(left["vector"], right["vector"]),
                            "normalized_l2": math.hypot(*(a - b for a, b in zip(left["vector"], right["vector"]))),
                            "old_raw_norm": left.get("raw_norm"), "new_raw_norm": right.get("raw_norm")})
    pairs = []
    for cohort in ("v11", "balanced"):
        selected = [sample for sample in samples if sample["cohort"] == cohort]
        for index, left in enumerate(selected):
            for right in selected[index + 1:]:
                a, b = left["id"], right["id"]
                before = cosine(old[a]["vector"], old[b]["vector"])
                after = cosine(new[a]["vector"], new[b]["vector"])
                pairs.append({"left": a, "right": b, "cohort": cohort, "old_cosine": before,
                              "official_cosine": after, "absolute_difference": abs(before - after)})
    assert len(pairs) == 585
    probes = {}
    for label, embeddings in (("previous_wrapper", old), ("official_pinned", new)):
        probes[label] = [compare(target, [sample for sample in samples if sample["cohort"] == target["cohort"]
                                         and sample["target_role"] == "reference"], embeddings)
                         for target in samples if target["known_actual_speaker"]]
    max_abs = max(row["normalized_max_abs"] for row in differences)
    min_cos = min(row["vector_cosine"] for row in differences)
    max_pair = max(row["absolute_difference"] for row in pairs)
    equivalent = (max_abs <= CRITERIA["normalized_vector_max_abs"]
                  and min_cos >= CRITERIA["vector_cosine_min"]
                  and max_pair <= CRITERIA["within_cohort_pair_score_max_abs"])
    report = {"created_at": datetime.now(timezone.utc).isoformat(), "run": str(run.relative_to(HERE)),
              "manifest_sha256": sha(manifest_path), "old_embedding_sha256": sha(old_path),
              "official_embedding_sha256": sha(new_path), "official_provenance": provenance,
              "environment_manifest_sha256": sha(environment_path),
              "acceptance_criteria": CRITERIA, "approximately_equivalent": equivalent,
              "summary": {"samples": 47, "within_cohort_pairs": 585, "maximum_normalized_element_difference": max_abs,
                          "minimum_vector_cosine": min_cos, "maximum_pair_score_difference": max_pair},
              "per_sample_differences": differences, "pair_differences": pairs, "known_targets": probes,
              "weak_label_auc": {"previous_wrapper": weak_label_auc(samples, old), "official_pinned": weak_label_auc(samples, new)},
              "limits": ["Only two distinct utterances have user-confirmed identity; three probe conditions include two Soyo variants.",
                         "Mirror checkpoint identity restriction is unchanged; this tests the same bytes through two execution paths.",
                         "Historical environment and CPU architecture details are recorded, not inferred from the model name."]}
    (HERE / "comparison.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    lines = ["# WavLM 官方指定版本复测", "", "同一份 Large SV 权重、同一批 47 段 PCM，与之前的兼容封装逐项比较。", "",
             f'完成运行：`{run.name}`。近似等价检查：**{equivalent}**。', "",
             "| 检查 | 实际最大差／最小值 | 预先固定的界限 |", "|---|---:|---:|",
             f'| 归一化向量逐元素最大差 | {max_abs:.12g} | ≤1e-5 |',
             f'| 同一音频两条路径的向量 cosine 最小值 | {min_cos:.12g} | ≥0.999999 |',
             f'| 585 个池内配对的 cosine 最大差 | {max_pair:.12g} | ≤1e-5 |', "",
             "## 已确认身份条件", "", "逐条 cosine 等权均值，保留原登记标签、参考池和目标边界，排除自身／重复／重叠。", "",
             "| 路径 | 目标 | 正确人物 | 正确人物均值 | 最高候选／均值 | 正确人物名次 |", "|---|---|---|---:|---|---:|"]
    for label, conditions in probes.items():
        for probe in conditions:
            expected = next(row for row in probe["candidates"] if row["speaker"] == probe["known_actual_speaker"])
            rank = probe["rankings"]["mean_pairwise_cosine"]
            lines.append(f'| {label} | {probe["target_id"]} | {probe["known_actual_speaker"]} | {expected["mean_pairwise_cosine"]:.9f} | {rank["top1"]}／{rank["top1_score"]:.9f} | {rank["known_actual_rank"]} |')
    lines += ["", "## 按原登记标签的辅助分离度", "", "| 路径 | v11（排除两条已知错标，29 refs）AUC | balanced（15 refs）AUC |", "|---|---:|---:|"]
    for label, rows in report["weak_label_auc"].items():
        lines.append(f'| {label} | {rows[0]["auc"]:.9f} | {rows[1]["auc"]:.9f} |')
    lines += ["", "这些参考身份未全部人工审核，AUC 衡量与登记标签的一致性，不是人物准确率。", "",
              "## 实际运行方式", "",
              "直接执行官方 UniSpeech `verification.init_model`／ECAPA，特征来自官方指定 commit 的 s3prl `UpstreamExpert`、`UpstreamBase` 和 hooks。只将远程 hub 下载入口路由到同版本提供的 `wavlm_local`，读取已有本地初始化文件；模型网络、前处理和特征代码没有替换。原模型先按官方入口载入，再检查所有 embedding 状态键并仅排除训练分类投影后严格加载。", "",
              f'Python：`{provenance["python"].splitlines()[0]}`；平台：`{provenance["platform"]}`。',
              f'依赖版本：`{json.dumps(provenance["versions"], ensure_ascii=False)}`。', "",
              "s3prl 固定到官方 README 指定的 `7ab62aaf2606d83da6c71ee74e7d16e0979edbc3`。原 requirements 的六个版本保留；Torch 1.9.0／NumPy 1.20.3／Python 3.9.25 为兼容运行选择，fairseq 使用官方完整 v0.12.2 tag 原样构建。训练和其他 upstream 的依赖未全部安装，实际运行依赖及缺失项逐一记录，不宣称复原完整训练环境。", "",
              "原生 ARM 安装遇到旧 NumPy／SciPy 与 sentencepiece 编译问题，实际推理使用本机已有 Rosetta 执行 x86_64 二进制包。官方 PyPI fairseq 0.12.2 sdist 缺少两份 C++ 源文件；切换完整同版本 Git tag 后构建成功。模型源码没有补丁。上游 requirements 的传递依赖未完整锁 hash，因此保留六个版本、记录实际 freeze，没有宣称完整 hash-lock 安装成功。", "",
              f'47 条前向累计 {sum(row["embedding_ms"] for row in new.values())/1000:.3f} 秒，单段中位 {statistics.median(row["embedding_ms"] for row in new.values()):.3f}ms。CPU／FP32／4 线程／batch=1，另做一次预热；时间不含安装、导入和模型加载。不同环境或架构下的时间不作为生产吞吐比较。', "",
              "本次仍使用上一轮同一份可追溯镜像 SV 权重；微软原文件不可直接下载，尚未与其逐字节核对。这项来源限制与执行链路是否数值等价分别记录。", "",
              "[完整差异与逐条候选](comparison.json) · [本次向量](" + str(run.relative_to(HERE)) + "/embeddings.json) · [本次来源与资源](" + str(run.relative_to(HERE)) + "/provenance.json)", "",
              "[独立复算](verification.json) · [隔离环境与失败记录](../../../../../outputs/local-models/wavlm-speaker-sv/official-pinned/environment.json) · [完整 freeze](../../../../../outputs/local-models/wavlm-speaker-sv/official-pinned/freeze.txt)", "",
              "## 复现", "", "从项目根目录执行。保留已有运行；重新提取时选择尚不存在的 `run2` 或其他名称。", "", "```sh",
              "outputs/local-models/wavlm-speaker-sv/official-pinned/env-x86/bin/python -u engine/experiment/speaker-encoder/20261018-mygo-ep04-model-comparison/wavlm_official_pinned/runner.py --output engine/experiment/speaker-encoder/20261018-mygo-ep04-model-comparison/wavlm_official_pinned/run2",
              ".venv/bin/python engine/experiment/speaker-encoder/20261018-mygo-ep04-model-comparison/wavlm_official_pinned/compare_official.py --run engine/experiment/speaker-encoder/20261018-mygo-ep04-model-comparison/wavlm_official_pinned/run2",
              ".venv/bin/python engine/experiment/speaker-encoder/20261018-mygo-ep04-model-comparison/wavlm_official_pinned/verify.py", "```", "",
              "本轮生产逻辑 0 行；实验及隔离环境目录均被 Git 忽略，原项目环境、原实验音频与向量保留，未执行服务重启、迁移、解析任务、commit 或 push。", ""]
    (HERE / "README.md").write_text("\n".join(lines))
    print(json.dumps({"approximately_equivalent": equivalent, **report["summary"]}, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", type=Path, default=HERE / "run1")
    main(parser.parse_args().run.resolve())
