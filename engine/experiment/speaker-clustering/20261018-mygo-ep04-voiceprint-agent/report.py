"""Recompute held-out scores after the Agent stops; never feeds results back to it."""
from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
import hashlib
import html
import json
import math
import os
from pathlib import Path
from urllib.parse import quote

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]
FROZEN = HERE.parent / "20261018-mygo-ep04-wavlm-kmeans"
OLD = ROOT / "engine/experiment/speaker-encoder/20261018-mygo-ep04-model-comparison"
TARGETS = {"v11_02": "千早爱音", "v11_27": "长崎爽世",
           "asr_c04_i014_p00": "千早爱音", "asr_c00_i010_p00": "长崎爽世"}


def read(path):
    return json.loads(path.read_text())


def cosine(a, b):
    return math.fsum(x * y for x, y in zip(a, b, strict=True)) / math.hypot(*a) / math.hypot(*b)


def main():
    status, registry, candidates, protocol = (read(HERE / name) for name in
        ("status.json", "registry.json", "candidates.json", "protocol.json"))
    if status["status"] == "running":
        raise RuntimeError("Wait for the frozen Agent run to finish before evaluating")
    old, new = read(OLD / "manifest.json"), read(FROZEN / "manifest.json")
    assert old["video"]["sha256"] == new["video"]["sha256"] == candidates["video"]["sha256"]
    samples = {r["id"]: r for r in old["samples"] + new["samples"]}
    vectors = {r["id"]: r["vector"] for r in read(OLD / "wavlm_large/embeddings.json") + read(FROZEN / "embeddings.json")}
    old_bank, agent_bank = {}, {}
    for row in old["samples"]:
        if row["cohort"] == "v11" and row["id"] not in protocol["scoring"]["excluded_old_reference_ids"]:
            old_bank.setdefault(row["speaker"], []).append(row["id"])
    for row in registry["references"]:
        assert row["embedding"] == vectors[row["sample_id"]]
        agent_bank.setdefault(row["speaker"], []).append(row["sample_id"])
    mixed = {**old_bank, **agent_bank}  # Keep every new competitor and explicitly retain missing old speakers.
    previous = read(FROZEN / "reference-bank-check.json")["banks"]["four_groups_replaced"]
    banks = {"old_v11_excluding_known_errors": old_bank, "previous_exploratory_selection": previous,
             "agent_only": agent_bank, "agent_plus_missing_old": mixed}
    metrics = (protocol["scoring"]["primary"], protocol["scoring"]["secondary"])
    conditions = []
    for target_id, actual in TARGETS.items():
        target = samples[target_id]
        for bank_name, bank in banks.items():
            scores = []
            for speaker, refs in sorted(bank.items()):
                valid = [r for r in refs if r != target_id and samples[r]["pcm_sha256"] != target["pcm_sha256"]
                         and (samples[r]["end_ms"] <= target["start_ms"] or samples[r]["start_ms"] >= target["end_ms"])]
                assert valid and len(valid) == len(refs)
                assert len({samples[r]["pcm_sha256"] for r in valid}) == len(valid)
                pairwise = [{"reference_id": r, "cosine": cosine(vectors[target_id], vectors[r])} for r in valid]
                center = [math.fsum(v) / len(valid) for v in zip(*(vectors[r] for r in valid), strict=True)]
                scores.append({"speaker": speaker, "reference_ids": valid, "individual_scores": pairwise,
                    "mean_pairwise_cosine": math.fsum(s["cosine"] for s in pairwise) / len(valid),
                    "normalized_centroid_cosine": cosine(vectors[target_id], center)})
            rankings = {}
            for metric in metrics:
                ordered = sorted(scores, key=lambda r: (-r[metric], r["speaker"]))
                true = next((r for r in ordered if r["speaker"] == actual), None)
                rivals = [r[metric] for r in ordered if r["speaker"] != actual]
                rankings[metric] = {"ordered_speakers": [r["speaker"] for r in ordered],
                    "true_rank": ordered.index(true) + 1 if true else None,
                    "true_score": true[metric] if true else None,
                    "true_minus_best_other": true[metric] - max(rivals) if true and rivals else None}
            conditions.append({"target_id": target_id, "known_actual_speaker": actual, "bank": bank_name,
                               "candidates": scores, "rankings": rankings})
    trace = [json.loads(line) for line in (HERE / "trace.jsonl").read_text().splitlines()]
    counts = Counter(r["tool"] for r in trace if r["event"] == "tool_started")
    frame_calls = [r for r in trace if r["event"] == "tool_started" and r["tool"] == "inspect_candidate_frames"]
    usage = read(HERE / "usage.json")
    result = {"created_at": datetime.now(UTC).isoformat(), "status": status, "bank_ready": registry["ready"],
              "banks": banks, "old_fallback_speakers": sorted(set(old_bank) - set(agent_bank)),
              "missing_original_speakers": sorted(set(old_bank) - set(agent_bank)), "conditions": conditions,
              "tools": dict(counts), "distinct_frame_sources": len({r["arguments"]["source_ref"] for r in frame_calls}),
              "usage": usage, "inputs": {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in
                  (HERE / "registry.json", HERE / "protocol.json", HERE / "candidates.json", Path(__file__),
                   OLD / "manifest.json", OLD / "wavlm_large/embeddings.json", FROZEN / "manifest.json", FROZEN / "embeddings.json",
                   FROZEN / "reference-bank-check.json")},
              "limitations": ["Only two known utterances; ASR variants are not extra independent truth.",
                  "K8 is exploratory, not an unseen-test optimum; registered identities are not human-certified.",
                  "Agent-only and explicit old-reference fallback are separate conditions; no hidden backfill.",
                  "Scores are cosine similarities, not probabilities or calibrated acceptance thresholds.",
                  "Two superseded harness runs are not algorithm comparison arms."]}
    (HERE / "evaluation.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    lines = ["# 第四集：聚类选源 → VoiceprintAgent 实验", "",
        f"最终运行状态：`{status['status']}`；本地 ready=`{registry['ready']}`；{len(registry['references'])} 条登记、{len(agent_bank)} 人；耗时 {status['elapsed_seconds']:.2f} 秒。",
        "本轮未达到11人名单严格过半的6人门槛；15条仅为未发布的诊断草稿，不能作为正式声库使用。"
        "即使本地ready通过，也只代表覆盖和提交检查，不代表身份已由人工核验。未写入生产。", "",
        "## 固定选源", "", "使用此前第四集 V9 的完整24个chunk ASR快照及WavLM-Large SV兼容实现已算好的256维向量。"
        "226条 fit，K8/seed1000；cosine距离半径0.40、至少2个独立邻居，得到151条常规候选、75条待核。"
        "每组先取cue均衡medoid，再逐轮max-min，优先不同chunk；每组展示至多3条、最小新增距离0.05，合计18条。"
        "这是展示预算，不是人物登记上限。75条待核本轮未交给Agent；C02/C06为空，不降低门槛补数。", "",
        "两句目标及其关联完整cue、重复PCM和重叠来源均未参与选源或登记。人物背景从当前生产Series只读导出，"
        "包含视觉姓名映射，不含两句目标答案；它不是历史V9背景快照。", "",
        "## 接线与运行边界", "", "真实调用现有VoiceprintAgent.InvokeVoiceprint、Canvas、标准登记/撤销/完成工具和Gateway。"
        "MCP关闭；新增实验工具只允许查看当前候选的3张原视频帧。模型不能听音频，WavLM负责声学计算。"
        "登记仅接收宿主签发的source_ref及精确缓存区间，不接收模型自行提供的向量或路径。"
        "WavLM256维登记保存在本地registry.json；正式192维schema、Server、数据库、队列均未修改。", "",
        "完成要求11人名单中至少6人有参考，并为少于目标6条的人提供原因；没有启用未经校准的WavLM相似度门禁。", "",
        "## 两条目标结果", "", "主指标逐条均值 = mean(cos(query, ref))；次指标质心分 = cos(query, mean(unit_refs))。"
        "两种口径分开报告，不根据结果切换。混合对照保留Agent新增人物，同时只为缺失的原六人物沿用旧参考。", "",
        "| 目标 | 声库 | 正确人物均值/名次 | 均值最高人物 | 正确人物质心/名次 | 质心最高人物 |",
        "|---|---|---:|---|---:|---|"]
    for c in conditions:
        if c["target_id"] not in ("v11_02", "v11_27"):
            continue
        a, b = (c["rankings"][metric] for metric in metrics)
        show = lambda r: f"{r['true_score']:.6f} / {r['true_rank']}" if r["true_score"] is not None else "缺参考"
        top = lambda r: r["ordered_speakers"][0] if r["ordered_speakers"] else "无声库"
        lines.append(f"| {c['target_id']}：{c['known_actual_speaker']} | {c['bank']} | {show(a)} | {top(a)} | {show(b)} | {top(b)} |")
    lines += ["", "旧参考补入人物：" + "、".join(result["old_fallback_speakers"]), "",
              "全人物逐条分数、名次与正确人物相对最高竞争者的差值见 [evaluation.json](evaluation.json)。", "",
              "## 本轮效果判断", "", "爱音目标在旧WavLM参考库本来就第一，本轮保持第一，不能称新增修复。"
              "爽世目标在仅5人的草稿库排第一；加入缺失的祥子、乐奈旧参考并保留海铃这一新竞争者后，"
              "均值为0.406597，乐奈0.529823，爽世仍第二；质心爽世0.539832、乐奈0.529823，仅相差0.010009。"
              "这个差距没有校准为可靠接受条件。", "",
              "上一轮按近邻支持排序的探索选源，爽世均值0.529157、质心0.593479，均高于本轮。"
              "上一轮有3条爽世参考、本轮有6条，姓名核验方式也不同，因此不是等预算算法消融；"
              "至少当前结果不支持‘多样性采样加更多参考必然更好’。不能只与较差旧库比较后宣布新方法更优。", "",
              "身份核验还发现疑点：985100–987100毫秒的原null候选（K8:C03典型片）被登记为千早爱音，"
              "依据却写‘爱音正面张口说出让爱音站中间的台词’。实际台词是在称呼爱音，多人画面也不能只凭一个张嘴者确定声音。"
              "这是需要试听/连续画面人工核验的疑点，尚未确认正确发声人；本轮未修改其标签或按目标分数删除它。"
              "具体依据写得更长不等于身份可靠。", "",
              "本轮结论：候选生成与真实Agent调用已接通；人物覆盖和声库身份质量验收均未通过。"
              "后续优先独立核验典型片身份，再测试每人物多个可信原型；保留候选间不同身份和未知，不把整簇当作人物。"
              "75条待核来源可用于补覆盖，但需要独立试验，不能本轮为了ready临时降低筛选标准。", "",
              "## 候选与登记来源", "", "[打开可试听报告](report.html)。弱标签及Agent登记都不能当作人工真值。", "",
              "| 片段 | 分组/角色 | 毫秒区间 | 原弱标签 | 最终登记 | 台词 |", "|---|---|---:|---|---|---|"]
    registered = {r["sample_id"]: r for r in registry["references"]}
    cards = []
    for c in candidates["candidates"]:
        r = registered.get(c["sample_id"])
        speaker = r["speaker"] if r else "未登记"
        lines.append(f"| [{c['sample_id']}]({os.path.relpath(c['audio_path'], HERE)}) | {c['group_id']}/{c['selection']['role']} | {c['start_ms']}–{c['end_ms']} | {c['weak_speaker'] or 'null'} | {speaker} | {c['text'].replace('|', '／')} |")
        cards.append(f"<article><h3>{html.escape(c['sample_id'])}</h3><p>{html.escape(c['group_id'])} · {html.escape(c['selection']['role'])} · {c['start_ms']/1000:.3f}–{c['end_ms']/1000:.3f}s</p>"
            f"<p>原弱标签：{html.escape(c['weak_speaker'] or 'null')} → 最终登记：<b>{html.escape(speaker)}</b></p><p>{html.escape(c['text'])}</p>"
            f"<audio controls preload='none' src='{quote(os.path.relpath(c['audio_path'], HERE))}'></audio>"
            f"<details><summary>Agent依据</summary><p>{html.escape(r['selection_basis'] if r else '未登记；不自动补入旧标签')}</p></details></article>")
    lines += ["", "## 审计与成本", "", f"工具调用：`{dict(counts)}`；{len(frame_calls)}次看帧请求、{result['distinct_frame_sources']}个不同来源。",
        "最终轮由gpt-5.6-sol完成，没有触发备用模型；声纹计算复用此前CPU WavLM向量，本轮没有重新运行编码器。",
        "用量采用Gateway记录的SDK线程累计口径，包含多轮输入和缓存重复计数，不是单个上下文长度。见 [usage.json](usage.json)。", "",
        "| 执行 | 秒 | 累计输入 tokens | 其中缓存 | 输出 tokens | 状态 |", "|---|---:|---:|---:|---:|---|"]
    for directory in (HERE / "superseded-run-01", HERE / "superseded-run-02", HERE):
        stage, calls = read(directory / "status.json"), read(directory / "usage.json")
        totals = {key: sum((call.get("usage") or {}).get(key, 0) or 0 for call in calls)
                  for key in ("input_tokens", "cached_input_tokens", "output_tokens")}
        lines.append(f"| {directory.name} | {stage['elapsed_seconds']:.2f} | {totals['input_tokens']} | {totals['cached_input_tokens']} | {totals['output_tokens']} | {stage['status']} |")
    lines += ["",
        "前两次宿主结果保留：`superseded-run-01/`把弱标签重复放入members，Agent错误地称为独立佐证；"
        "`superseded-run-02/`移除重复后，因为宿主遗漏生产人物背景而零登记。最终补齐已有背景，仍不复制members人物身份。"
        "它们是接线纠正记录，不是算法对照组。18条候选及几何参数始终不变，目标分数在最终运行结束后才计算。", "",
        "启动失败记录在startup-failure-01/02：未加载项目环境、UV缓存权限、Codex状态库沙箱限制。最终按项目环境启动，使用正常权限访问Codex自有状态库。"
        "模型名额从20改65，是匹配Gateway每次工具回调预留一个名额的计数方式；64工具/900秒时限不变。", "",
        "## 复算与范围", "", "```sh", "# 从仓库根目录执行；已存在的正式运行产物不会被自动覆盖", f".venv/bin/python {Path(__file__).relative_to(ROOT)}", "```", "",
        "prepare.py可重复复算固定候选；run_agent.py --check用真实Agent/工具、替代模型进行本地检查并实际抽帧。"
        "真实模型命令与代码SHA见run-manifest.json，完整工具参数/返回见trace.jsonl。", "",
        "本次新增三个实验脚本及本目录产物，并同步原voiceprint-trd.md的实现状态和结果。无生产代码/API/数据库schema/配置/前端改动，"
        "无服务重启、迁移、整集重解析、commit或push。只读导出一份现有Series背景。"
        "engine/experiment与design目录均被Git忽略；这些是本地维护成果，不在提交中。", "",
        "```text", "AniVAG/", "├── agent/video_parse_v2/design/",
        "│   └── [M] voiceprint-trd.md  # 更新实验状态、门禁及效果边界",
        "└── engine/experiment/speaker-clustering/20261018-mygo-ep04-voiceprint-agent/",
        "    ├── [A] prepare.py       # 179行：冻结参数、选源、来源校验",
        "    ├── [A] run_agent.py     # 384行：现有Agent与本地宿主、真实工具和自检",
        "    ├── [A] report.py        # 离线评分与可试听报告生成",
        "    ├── [A] protocol.json / candidates.json / selection.json / background.json",
        "    ├── [A] registry.json / trace.jsonl / status.json / result.json / usage.json",
        "    ├── [A] run-manifest.json / run.log / evaluation.json / verification.json",
        "    ├── [A] report.md / report.html",
        "    └── startup-failure-*/、superseded-run-*/  # 保留诊断与旧执行快照",
        "```", "",
        "没有新增测试文件；本地自检与独立复算的范围：真实Agent/工具驱动、真实帧提取、未读/未看/非精确区间拒绝、登记幂等、"
        "完成结果落盘回读、人物字段不重复、三轮18条候选一致、逐条分数与排名复核、HTML本地资源链接有效。"
        "没有运行生产租约/恢复/全量解析测试，也未证明全部参考身份正确。其他并行工作区改动未纳入本次范围。", ""]
    (HERE / "report.md").write_text("\n".join(lines))
    page = "<!doctype html><html lang='zh-CN'><meta charset='utf-8'><title>第四集声纹选源实验</title><style>body{max-width:1100px;margin:32px auto;padding:0 20px;background:#f5f6f8;color:#202430;font:16px/1.6 system-ui}h1{font-size:28px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(310px,1fr));gap:14px}article{padding:18px;background:white;border:1px solid #ddd;border-radius:10px}h3{font-size:16px}audio{width:100%}details p{font-size:14px}a{color:#284ea5}</style>"
    page += f"<h1>第四集：聚类选源 → VoiceprintAgent</h1><p>{html.escape(status['status'])} · ready={registry['ready']} · {len(registry['references'])}条 / {len(agent_bank)}人 · {status['elapsed_seconds']:.2f}秒</p>"
    page += "<p>覆盖不足，ready=false；15条为未发布草稿。以下姓名为Agent判断，尚未逐条人工核验。音频来自冻结的原视频片段。</p><p><a href='report.md'>完整报告</a> · <a href='evaluation.json'>逐条评分</a> · <a href='registry.json'>本地登记</a></p>"
    page += "<h2>两条固定目标：保留所有竞争人物</h2><p>混合对照仅为缺失的祥子、乐奈补入旧参考，同时保留海铃。相似度不是概率。</p><div class='grid'>"
    for target_id in ("v11_02", "v11_27"):
        condition = next(c for c in conditions if c["target_id"] == target_id and c["bank"] == "agent_plus_missing_old")
        target = samples[target_id]
        page += f"<article><h3>{html.escape(TARGETS[target_id])} · {target['start_ms']/1000:.1f}–{target['end_ms']/1000:.1f}s</h3><audio controls preload='none' src='{quote(os.path.relpath(FROZEN / target['audio'], HERE))}'></audio><table><tr><th>候选</th><th>均值</th><th>质心</th></tr>"
        for row in sorted(condition["candidates"], key=lambda r: -r[metrics[0]]):
            page += f"<tr><td>{html.escape(row['speaker'])}</td><td>{row[metrics[0]]:.4f}</td><td>{row[metrics[1]]:.4f}</td></tr>"
        page += "</table></article>"
    page += "</div><h2>18条候选试听与Agent依据</h2><p>需重点复核：asr_c16_i012_p00，原null被登记为爱音，但台词在称呼爱音；这是疑点，不是已确认真值。</p><div class='grid'>" + "".join(cards) + "</div></html>"
    (HERE / "report.html").write_text(page)
    print(json.dumps({"ready": registry["ready"], "references": len(registry["references"]), "speakers": len(agent_bank), "conditions": len(conditions)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
