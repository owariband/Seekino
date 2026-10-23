"""Offline EP04 v11 leave-one-out audit; no services, inference or registry writes."""
from __future__ import annotations

from collections import Counter
import hashlib
import json
import math
from pathlib import Path

HERE = Path(__file__).resolve().parent
SNAPSHOT = HERE / "voiceprint-trace-20261018T002342.json"
TASK = "0f681291-4f52-4254-a3be-91c1ee835c3c"
VIDEO = "0019c16c-a7b7-5f6c-8275-5019869c1207"


def main():
    data = json.loads(SNAPSHOT.read_text())
    task = data["task"]
    assert task["id"] == TASK and task["video_id"] == VIDEO
    registry = task["detail"]["voiceprint"]["voiceprintList"]
    refs = [dict(ref, speaker=group["speaker"], model=group["model"])
            for group in registry for ref in group["references"]]
    assert len(refs) == 31
    speakers = sorted({ref["speaker"] for ref in refs})
    normalized = []
    for ref in refs:
        vector = ref["vector"]
        assert len(vector) == 192 and all(math.isfinite(x) for x in vector)
        norm = math.sqrt(math.fsum(x * x for x in vector))
        assert norm > 0 and ref["start_ms"] < ref["end_ms"]
        normalized.append([x / norm for x in vector])

    # The same exclusions apply to own and other groups; no target self-score.
    pairs, scores = [], {}
    for i, left in enumerate(refs):
        for j in range(i + 1, len(refs)):
            right = refs[j]
            if (left["model"] != right["model"]
                    or left["pcm_sha256"] == right["pcm_sha256"]
                    or max(left["start_ms"], right["start_ms"]) < min(left["end_ms"], right["end_ms"])):
                continue
            value = math.fsum(a * b for a, b in zip(normalized[i], normalized[j]))
            scores[i, j] = scores[j, i] = value
            pairs.append({"left": i, "right": j, "cosine": value})

    rows = []
    for i, ref in enumerate(refs):
        groups = {}
        for speaker in speakers:
            peers = [scores[i, j] for j, peer in enumerate(refs)
                     if peer["speaker"] == speaker and (i, j) in scores]
            groups[speaker] = {"count": len(peers), "mean": math.fsum(peers) / len(peers) if peers else None}
        own = groups[ref["speaker"]]["mean"]
        other = sorted((speaker for speaker in speakers if speaker != ref["speaker"]
                        and groups[speaker]["mean"] is not None), key=lambda speaker: -groups[speaker]["mean"])
        best = other[0]
        rows.append({
            "index": i, "speaker": ref["speaker"], "model": ref["model"],
            "source_ref": f'{ref["chunk_id"]}:{ref["event_fingerprint"]}',
            **{key: ref[key] for key in ("start_ms", "end_ms", "duration_ms", "source_version", "pcm_sha256")},
            "groups": groups, "candidate_mean": own, "best_other_speaker": best,
            "best_other_mean": groups[best]["mean"],
            "other_minus_own": groups[best]["mean"] - own if own is not None else None,
        })

    previous = json.loads((HERE / "candidate-mean-audit.json").read_text())
    expected = {(row["speaker"], row["start_ms"], row["end_ms"]): row["candidate_mean"]
                for row in previous["sorted_reference_scores"]}
    for row in rows:
        old = expected[row["speaker"], row["start_ms"], row["end_ms"]]
        assert (row["candidate_mean"] is None and old is None) or math.isclose(row["candidate_mean"], old, abs_tol=1e-12)
    conflicts = sorted((row for row in rows if row["other_minus_own"] is not None
                        and row["other_minus_own"] > 0), key=lambda row: -row["other_minus_own"])
    counts = Counter(ref["speaker"] for ref in refs)
    known_wrong = {row["index"] for row in rows
                   if (row["start_ms"], row["end_ms"]) in {(47500, 48800), (293500, 296000)}}
    assert len(known_wrong) == 2
    without_known_wrong = []
    for i in sorted(known_wrong):
        groups = {}
        for speaker in speakers:
            peers = [scores[i, j] for j, peer in enumerate(refs)
                     if peer["speaker"] == speaker and j not in known_wrong and (i, j) in scores]
            groups[speaker] = {"count": len(peers), "mean": math.fsum(peers) / len(peers) if peers else None}
        without_known_wrong.append({"target_index": i, "groups": groups})
    result = {
        "task_id": TASK, "video_id": VIDEO, "input_snapshot": SNAPSHOT.name,
        "input_sha256": hashlib.sha256(SNAPSHOT.read_bytes()).hexdigest(),
        "method": "Mean of individual cosine scores per speaker, equal weight per reference. Own group excludes target. No centroid normalization, confidence weights, trimming or relabeling.",
        "exclusions": "Exclude self, overlapping audio, repeated PCM and different encoders; all rows are from this one video/task snapshot.",
        "summary": {"reference_count": len(refs), "pair_count": len(pairs),
                    "own_comparable": sum(row["candidate_mean"] is not None for row in rows),
                    "other_stronger_count": len(conflicts), "own_group_top_count": sum(row["other_minus_own"] is not None and row["other_minus_own"] <= 0 for row in rows),
                    "reference_counts": dict(counts)},
        "references": rows, "pairs": pairs,
        "sensitivity_check_excluding_two_user_confirmed_wrong_references": without_known_wrong,
        "limits": ["Registry labels are not ground truth; label agreement is not accuracy.",
                   "One-reference Raana has no own comparison; Mutsumi has no registered reference.",
                   "Short/noisy clips and shared acoustic conditions are not calibrated out.",
                   "The two user-confirmed incorrect labels are retained in this experiment.",
                   "No model calls, DB writes, speaker updates, restart or reparse."],
    }
    (HERE / "cross-group-mean-audit.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    lines = ["# 第四集 v11 跨人物组均值实验", "",
             f"固定任务 `{TASK}`，共31条参考、{len(pairs)}个不重复无序配对。输入SHA256：`{result['input_sha256']}`。", "",
             "对每条参考，与每个人物组中的参考逐一计算cosine并等权平均。本组排除自身；所有组均排除重叠音频、重复PCM和不同编码器。此处没有质心再归一化，也没有使用Agent自报confidence加权。", "",
             "30条可做本组比较，其中25条本组仍最高、5条其他组更高。这是与登记标签的一致性，不能作为识别准确率。乐奈只有1条，不能做本组比较；睦没有登记向量。", "",
             "## 其他人物组均值超过本组的全部参考", "",
             "| 登记人物 | 区间（秒） | 本组均值 | 最高其他组 | 其他组均值 | 其他－本组 |",
             "|---|---|---:|---|---:|---:|"]
    for row in conflicts:
        lines.append(f'| {row["speaker"]} | {row["start_ms"]/1000:.3f}–{row["end_ms"]/1000:.3f} | {row["candidate_mean"]:.6f} | {row["best_other_speaker"]} | {row["best_other_mean"]:.6f} | {row["other_minus_own"]:.6f} |')
    lines += ["", "## 全部参考与各人物组的均值", "",
              "本组列排除自身，常规本组分母5、其他主要人物组分母6；乐奈列分母仅1。完整分母、来源和每一对cosine见JSON。", "",
              "| 登记人物 | 区间（秒） | " + " | ".join(speakers) + " |",
              "|---|---|" + "---:|" * len(speakers)]
    for row in sorted(rows, key=lambda row: (row["speaker"], row["start_ms"])):
        values = [f'{row["groups"][speaker]["mean"]:.6f}' if row["groups"][speaker]["mean"] is not None else "—" for speaker in speakers]
        lines.append(f'| {row["speaker"]} | {row["start_ms"]/1000:.3f}–{row["end_ms"]/1000:.3f} | ' + " | ".join(values) + " |")
    lines += ["", "## 结论与边界", "",
              "293.5–296.0秒（用户确认爱音，登记祥子）明显指向爱音：0.661548，高于本组0.377256。", "",
              "47.5–48.8秒（用户确认爽世，登记灯）能被列为跨组疑点，但最高是祥子0.515009；爽世仅0.418053，低于灯组0.435419。当前库的简单均值不能把它正确自动归回爽世。", "",
              "补充离线检查：只从比对候选池排除这两条已知错标（不重贴标签、不改正式库），47.5–48.8秒仍以祥子0.492428最高，爽世仍0.418053；293.5–296.0秒仍以爱音0.661548最高。这说明经典片段的问题不能仅用这两条错标污染来解释。其余参考身份仍未全部人工核实。", "",
              "283.1–284.1秒祥子组参考虽只有0.370394，本组仍略高于灯组0.368776，差0.001619；422.5–423.2秒相反，仅灯组高0.001148。这种差异不能直接确定人物身份。本轮283.1–284.1的边界与此前用户试听的283.0–283.7不同。", "",
              "爽世6条本组均第一；立希6条中4条本组均第一，另2条更像爱音，其中751.7–753.3差0.367225、763.2–764.1差0.027325，均未新增人工身份确认。", "",
              "跨组相对比较能减少把低绝对分数直接判错的情况，适合给Agent提供需复核的证据。多数参考正确的假设不足以保证均值top1必定正确；本轮不改正式门禁、不自动重标，也不把这些差值当成已校准阈值。", "",
              "复现：在项目根目录运行 `.venv/bin/python engine/experiment/voiceprint-agent/20261018-mygo-ep04-three-quarter-sampling/cross_group_mean.py`。", "",
              "[原始结果和465对cosine](cross-group-mean-audit.json) · [输入快照](voiceprint-trace-20261018T002342.json)"]
    (HERE / "cross-group-mean-audit.md").write_text("\n".join(lines) + "\n")
    print(json.dumps(result["summary"], ensure_ascii=False))


if __name__ == "__main__":
    main()
