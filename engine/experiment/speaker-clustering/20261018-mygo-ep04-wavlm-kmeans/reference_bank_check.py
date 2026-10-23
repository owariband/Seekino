"""Compare frozen targets with old and cluster-selected references in the same speaker pool."""
from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
import math
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]
OLD = ROOT / "engine/experiment/speaker-encoder/20261018-mygo-ep04-model-comparison"
# The previously reported exploratory K=8 view; not selected again using targets.
CANDIDATE_NAMES = {0: "千早爱音", 2: "高松灯", 3: "长崎爽世", 7: "椎名立希"}
TARGET_IDS = ("v11_02", "v11_27")


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def cosine(left, right):
    return math.fsum(a * b for a, b in zip(left, right)) / math.hypot(*left) / math.hypot(*right)


def main():
    manifest = json.loads((HERE / "manifest.json").read_text())
    original = json.loads((OLD / "manifest.json").read_text())
    assert manifest["video"]["sha256"] == original["video"]["sha256"]
    new_samples = {row["id"]: row for row in manifest["samples"]}
    old_samples = {row["id"]: row for row in original["samples"]}
    new_vectors = {row["id"]: row["vector"] for row in json.loads((HERE / "embeddings.json").read_text())}
    old_vectors = {row["id"]: row["vector"] for row in json.loads((OLD / "wavlm_large/embeddings.json").read_text())}
    view = next(row for row in json.loads((HERE / "granularity.json").read_text())["views"] if row["k"] == 8)
    for identifier in TARGET_IDS:
        assert old_vectors[identifier] == new_vectors[identifier]
    samples, vectors = {**old_samples, **new_samples}, {**old_vectors, **new_vectors}
    old_bank = {}
    for row in original["samples"]:
        if row["cohort"] == "v11" and row["id"] not in TARGET_IDS:
            old_bank.setdefault(row["speaker"], []).append(row["id"])
    new_bank = {name: list(refs) for name, refs in old_bank.items()}
    for group in view["clusters"]:
        if group["cluster"] in CANDIDATE_NAMES:
            refs = [row["id"] for row in group["representatives"]]
            assert len(refs) == 3 and all(new_samples[identifier]["fit_eligible"] for identifier in refs)
            new_bank[CANDIDATE_NAMES[group["cluster"]]] = refs
    assert set(old_bank) == set(new_bank) and len(old_bank) == 6
    banks = {"old_references": old_bank, "four_groups_replaced": new_bank}
    conditions = []
    for identifier in (*TARGET_IDS, "asr_c04_i014_p00", "asr_c00_i010_p00"):
        target = samples[identifier]
        actual = "千早爱音" if identifier in ("v11_02", "asr_c04_i014_p00") else "长崎爽世"
        for bank_name, bank in banks.items():
            candidates = []
            for speaker, refs in sorted(bank.items()):
                valid = [ref for ref in refs if ref != identifier
                         and samples[ref]["pcm_sha256"] != target["pcm_sha256"]
                         and (samples[ref]["end_ms"] <= target["start_ms"] or target["end_ms"] <= samples[ref]["start_ms"])]
                assert valid and len({samples[ref]["pcm_sha256"] for ref in valid}) == len(valid)
                scores = [cosine(vectors[identifier], vectors[ref]) for ref in valid]
                center = [math.fsum(values) / len(valid) for values in zip(*(vectors[ref] for ref in valid))]
                candidates.append({"speaker": speaker, "reference_ids": valid,
                                   "individual_scores": [{"reference_id": ref, "cosine": score} for ref, score in zip(valid, scores)],
                                   "mean_pairwise_cosine": math.fsum(scores) / len(valid),
                                   "normalized_centroid_cosine": cosine(vectors[identifier], center)})
            rankings = {}
            for metric in ("mean_pairwise_cosine", "normalized_centroid_cosine"):
                ordered = sorted(candidates, key=lambda row: (-row[metric], row["speaker"]))
                true = next(row for row in ordered if row["speaker"] == actual)
                other = max(row[metric] for row in ordered if row["speaker"] != actual)
                rankings[metric] = {"ordered_speakers": [row["speaker"] for row in ordered],
                                    "true_rank": next(i + 1 for i, row in enumerate(ordered) if row["speaker"] == actual),
                                    "true_score": true[metric], "true_minus_best_other": true[metric] - other}
            conditions.append({"target_id": identifier, "start_ms": target["start_ms"], "end_ms": target["end_ms"],
                               "known_actual_speaker": actual, "bank": bank_name, "candidates": candidates, "rankings": rankings})
    result = {"created_at": datetime.now(UTC).isoformat(), "k_view": 8,
              "inputs": {str(path.relative_to(ROOT)): sha(path) for path in
                         (HERE / "manifest.json", HERE / "embeddings.json", HERE / "granularity.json", Path(__file__),
                          OLD / "manifest.json", OLD / "wavlm_large/embeddings.json")},
              "speaker_pool": sorted(old_bank), "banks": banks,
              "references": {ref: {key: samples[ref].get(key) for key in
                                    ("start_ms", "end_ms", "pcm_sha256", "audio", "weak_speakers", "speaker", "text")}
                             for ref in sorted({ref for bank in banks.values() for refs in bank.values() for ref in refs})},
              "conditions": conditions,
              "limits": ["K=8 is the previously presented exploratory view, not a new target-optimized selection.",
                         "Four new speaker labels are provisional associations to clusters, not independently confirmed identities.",
                         "Saki and Rana retain old references because no reliable new independent group was established.",
                         "Both known wrong reference sources are excluded from both banks; targets and overlapping PCM cannot enroll.",
                         "Only two distinct confirmed utterances; ASR boundary variants are not extra independent truth.",
                         "Mean pairwise cosine and normalized-centroid cosine are separate scoring policies, not probabilities.",
                         "Anon already ranks first with old WavLM references; no new repair is claimed for that target."]}
    (HERE / "reference-bank-check.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    lines = ["# 聚类选源后的两句留出验证", "", "目标是先筛参考声源，再检查两句错判；保持六个人物候选一致，只替换四组参考，不删掉竞争人物。", "",
             "K=8 是上一轮已经展示的探索分组，不是用两句目标重新选出的最优 K。候选组的姓名关联仍需试听核验。", "",
             "| 候选人物 | 旧参考数 | 新对照参考数 | 处理 |", "|---|---:|---:|---|"]
    for speaker in sorted(old_bank):
        lines.append(f"| {speaker} | {len(old_bank[speaker])} | {len(new_bank[speaker])} | {'K8 三条代表源，身份待核验' if speaker in CANDIDATE_NAMES.values() else '保留旧来源，尚无可靠新独立簇'} |")
    lines += ["", "两条已确认错标来源在两套参考中都排除；查询本身、重复 PCM、重叠音频不得登记。", "",
              "## 两条历史固定目标", "", "| 目标 | 参考库 | 正确人物均值／名次 | 均值最高人物 | 正确人物质心分／名次 | 质心最高人物 |", "|---|---|---:|---|---:|---|"]
    for condition in conditions:
        if condition["target_id"] not in TARGET_IDS:
            continue
        mean, centroid = (condition["rankings"][metric] for metric in ("mean_pairwise_cosine", "normalized_centroid_cosine"))
        lines.append(f"| {condition['target_id']}：{condition['known_actual_speaker']} | {condition['bank']} | {mean['true_score']:.6f}／{mean['true_rank']} | {mean['ordered_speakers'][0]} | {centroid['true_score']:.6f}／{centroid['true_rank']} | {centroid['ordered_speakers'][0]} |")
    lines += ["", "## 新参考对照的全部候选分数", "", "逐条均值 = mean(cos(target, ref))；质心分 = cos(target, normalize(mean(ref)))。分开报告，没有设 0.6 等通过阈值。", "",
              "| 目标 | 候选 | 参考数 | 逐条均值 | 归一化质心 |", "|---|---|---:|---:|---:|"]
    for condition in conditions:
        if condition["target_id"] in TARGET_IDS and condition["bank"] == "four_groups_replaced":
            for row in sorted(condition["candidates"], key=lambda row: -row["mean_pairwise_cosine"]):
                lines.append(f"| {condition['known_actual_speaker']} | {row['speaker']} | {len(row['reference_ids'])} | {row['mean_pairwise_cosine']:.6f} | {row['normalized_centroid_cosine']:.6f} |")
    lines += ["", "## 结论与边界", "",
              "爱音目标与新爱音参考高于祥子参考，且六人物中仍排第一；它在旧 WavLM 库本来就排第一，不能称本轮新增修复。爽世目标与新爽世参考明显高于灯、爱音参考；完整六人物候选下，逐条均值仍以极小差距输给乐奈，而质心口径爽世第一。不能只展示排除乐奈后的成功，也不能把两种聚合交替使用而不说明。", "",
              "这说明聚类可以产出更支持目标的候选参考。当前仍是待核身份的候选库：C03 三条灯参考中有一条原姓名为空；祥子、乐奈沿用的旧标签也未全量独立核验。高簇内支持、随机起点稳定与正确人物身份是不同证据。没有全人物准确率、已校准接受阈值或生产写回。", "",
              "[K2～12代表源试听](granularity.html) · [全候选／逐条分数／来源及ASR边界变体](reference-bank-check.json) · [主实验](README.md)", ""]
    (HERE / "reference-bank-check.md").write_text("\n".join(lines))
    print(json.dumps({"conditions": len(conditions), "speakers": len(old_bank), "model_calls": 0}))


if __name__ == "__main__":
    main()
