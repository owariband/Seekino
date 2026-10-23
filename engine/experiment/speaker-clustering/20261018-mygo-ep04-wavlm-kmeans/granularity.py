"""Inspect every already fitted K without changing the frozen model selection."""
from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
import html
import json
from pathlib import Path
from urllib.parse import quote

import cluster as core
import numpy as np

HERE = Path(__file__).resolve().parent


def main():
    manifest = json.loads((HERE / "manifest.json").read_text())
    primary = json.loads((HERE / "clustering.json").read_text())
    embeddings = json.loads((HERE / "embeddings.json").read_text())
    samples = manifest["samples"]
    assert primary["inputs"]["manifest.json"] == core.sha(HERE / "manifest.json")
    assert primary["inputs"]["embeddings.json"] == core.sha(HERE / "embeddings.json")
    assert [row["id"] for row in embeddings] == [row["id"] for row in samples]
    by_id = {sample["id"]: index for index, sample in enumerate(samples)}
    values = np.array([row["vector"] for row in embeddings], dtype=np.float64)
    values /= np.linalg.norm(values, axis=1)[:, None]
    similarities = np.clip(np.einsum("id,jd->ij", values, values, optimize=False), -1, 1)
    assert np.isfinite(similarities).all()
    fit = [by_id[identifier] for identifier in primary["fit_sample_ids"]]
    queries = primary["held_out_diagnostic_conditions"]
    query_indices = [by_id[row["id"]] for row in queries]
    assert not set(fit) & set(query_indices)

    def distances(centers):
        return np.sum((values[:, None, :] - np.asarray(centers)[None, :, :]) ** 2, axis=2)

    views = []
    for model in primary["k_scan"]:
        k = model["k"]
        squared = distances(model["centers"])
        labels = squared.argmin(axis=1)
        np.testing.assert_array_equal(labels[fit], model["labels"])
        stability = sum(np.array(group["label_alignment"])[distances(group["centers"]).argmin(axis=1)] == labels
                        for group in model["groups"])
        ordered = np.sort(squared, axis=1)
        margins = ordered[:, 1] - ordered[:, 0]
        evidence = {}
        for index in fit + query_indices:
            members = [other for other in fit if labels[other] == labels[index]]
            near = core.nearest_independent(index, members, samples, similarities, 10)
            evidence[index] = {
                "id": samples[index]["id"], "cluster": int(labels[index]),
                "aligned_membership_count": int(stability[index]),
                "same_cluster_support": float(similarities[index, near].mean()) if near else None,
                "support_count": len(near), "support_ids": [samples[other]["id"] for other in near],
                "euclidean_margin": float(margins[index]),
            }

        def rank(index):
            support = evidence[index]["same_cluster_support"]
            return (-int(stability[index]), -support if support is not None else float("inf"),
                    -float(margins[index]), samples[index]["start_ms"], samples[index]["id"])

        clusters = []
        for label in range(k):
            members = [index for index in fit if labels[index] == label]
            representatives = core.representative_indices(members, samples, rank, 3)
            remaining = [index for index in members if index not in representatives]
            boundary = min(remaining, key=lambda index: (margins[index], samples[index]["start_ms"])) if remaining else None
            clusters.append({
                "cluster": label, "fit_count": len(members),
                "fit_cue_count": len({samples[index]["cue_id"] for index in members}),
                "fit_chunk_count": len({samples[index]["chunk_sequence"] for index in members}),
                "member_ids": [samples[index]["id"] for index in members],
                "weak_label_counts": dict(Counter(name for index in members
                                                for name in samples[index]["weak_speakers"] or ["无具名标签"])),
                "representatives": [evidence[index] for index in representatives],
                "boundary_example": evidence[boundary] if boundary is not None else None,
            })
        query_rows = []
        for condition, index in zip(queries, query_indices):
            comparisons = []
            for group in clusters:
                individual = [{"reference_id": row["id"], "cosine": float(similarities[index, by_id[row["id"]]])}
                              for row in group["representatives"]]
                comparisons.append({"cluster": group["cluster"], "reference_count": len(individual),
                                    "mean_pairwise_cosine": sum(row["cosine"] for row in individual) / len(individual),
                                    "individual_scores": individual})
            query_rows.append({**condition, **evidence[index], "squared_center_distances": squared[index].tolist(),
                               "representative_comparisons": comparisons})
        views.append({"k": k, "seed": model["final_seed"],
                      "median_cosine_silhouette": model["median_cosine_silhouette"],
                      "clusters": clusters, "queries": query_rows})
    result = {
        "created_at": datetime.now(UTC).isoformat(),
        "purpose": "Exploratory display of every precomputed K; preserves automatic K selection and every fitted model",
        "primary_selected_k": primary["selected_k"], "models_refitted": 0,
        "inputs": {name: core.sha(HERE / name) for name in
                   ("manifest.json", "embeddings.json", "clustering.json", "protocol.json", "cluster.py", "granularity.py")},
        "limits": ["No secondary K is selected by holdout or weak identity labels.",
                   "Cluster IDs only refer to the current K; they are not aligned identities across K.",
                   "Weak-name counts are unverified diagnostics, not identity accuracy.",
                   "Representative mean cosine is a separate diagnostic; it does not replace Euclidean K-means assignment. These cluster pools differ from the old named registry.",
                   "All representatives are fitted, independent non-overlapping cue sources; short or held-out targets are not anchors."],
        "views": views,
    }
    (HERE / "granularity.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    esc = lambda value: html.escape(str(value), quote=True)

    def card(evidence, title):
        sample = samples[by_id[evidence["id"]]]
        support = evidence["same_cluster_support"]
        support_text = "不足" if support is None else f"{support:.4f}"
        return (f"<article><b>{esc(title)} · {esc(sample['id'])}</b>"
                f"<p>{core.timestamp(sample['start_ms'])}–{core.timestamp(sample['end_ms'])} · chunk {sample['chunk_sequence']}</p>"
                f"<p>弱标签：{esc(' / '.join(sample['weak_speakers']) or '未命名')}；{esc(sample.get('text') or '')}</p>"
                f"<p>随机起点归属 {evidence['aligned_membership_count']}/5 · 独立近邻支持 {support_text}"
                f"（{evidence['support_count']} cues）· 欧氏间隔 {evidence['euclidean_margin']:.4f}</p>"
                f"<audio controls preload='none' src='{quote(sample['audio'], safe='/')}'></audio></article>")

    panels = []
    for view in views:
        sections = [f"<section><h2>K={view['k']}：已保存模型，seed {view['seed']}</h2>"
                    f"<p>cosine silhouette 中位数 {view['median_cosine_silhouette']:.6f}。此页展示分组粒度，未重新挑选模型。</p></section>"]
        sections.append("<section><h2>留出的两句台词及边界变体</h2><div class='cards'>" +
                        "".join(card(row, f"{row['kind']} · 确认人物 {row['known_actual_speaker']} · 分到 {core.cluster_name(row['cluster'])}")
                                for row in view["queries"]) + "</div></section>")
        comparison_rows = []
        for query in view["queries"]:
            ranked = sorted(query["representative_comparisons"], key=lambda row: (-row["mean_pairwise_cosine"], row["cluster"]))
            comparison_rows.append(f"<tr><td>{esc(query['id'])}</td><td>{core.cluster_name(ranked[0]['cluster'])} / {ranked[0]['mean_pairwise_cosine']:.6f}</td>"
                                   f"<td>{core.cluster_name(ranked[1]['cluster'])} / {ranked[1]['mean_pairwise_cosine']:.6f}</td></tr>")
        sections.append("<section><h2>目标与已选代表源的均值对照</h2><p>每簇至多3条代表源的逐条 cosine 等权均值，"
                        "另列为诊断，不改写上面的 K-means 欧氏分配。此候选池不同于旧人物登记库，"
                        "乐奈、睦等没有拟合源，不能据此宣称全人物识别成功。</p><table><tr><th>条件</th><th>代表源均值第1</th><th>代表源均值第2</th></tr>" +
                        "".join(comparison_rows) + "</table></section>")
        for group in view["clusters"]:
            weak = " · ".join(f"{name}:{count}" for name, count in group["weak_label_counts"].items())
            cards = "".join(card(row, "代表源；身份待核验") for row in group["representatives"])
            if group["boundary_example"]:
                cards += card(group["boundary_example"], "边界例；不作为代表源")
            sections.append(f"<section><h2>{core.cluster_name(group['cluster'])} · {group['fit_count']} 窗 / "
                            f"{group['fit_cue_count']} cues / {group['fit_chunk_count']} chunks</h2>"
                            f"<p>原 Understanding 弱标签：{esc(weak)}。这是分布线索，不是簇的认证姓名。</p>"
                            f"<div class='cards'>{cards}</div></section>")
        panels.append(f"<div class='panel' data-k='{view['k']}' hidden>{''.join(sections)}</div>")
    options = "".join(f"<option value='{view['k']}'>K={view['k']}"
                      f"{'（自动几何选法）' if view['k'] == primary['selected_k'] else ''}</option>" for view in views)
    page = f"""<!doctype html><html lang='zh-CN'><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
<title>第四集：比较 K-means 分组粒度</title><style>body{{font:15px/1.6 system-ui;max-width:1450px;margin:24px auto;padding:0 18px;background:#f5f6fa;color:#17202a}}header,section{{background:white;padding:18px;margin:16px 0;border:1px solid #dbe0e7;border-radius:9px}}.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:12px}}article{{background:#f2f5f8;padding:12px;border-radius:6px;overflow-wrap:anywhere}}audio{{width:270px;max-width:100%}}table{{border-collapse:collapse;width:100%}}th,td{{padding:8px;border:1px solid #dbe0e7;text-align:left}}.controls{{position:sticky;top:0;background:#e8eef5;padding:14px;z-index:2}}select{{font:inherit;padding:8px}}[hidden]{{display:none!important}}</style>
<header><h1>第四集：K=2～12 的声音分组与代表源</h1><p>同一批 226 个拟合窗、同一个 WavLM；只展示已经算好的模型，未重新拟合。</p>
<p>自动指标选择 K=2，但两个大簇混有多个人物，不能用作人物声库。较细分组供探索；每个 K 的 C01 等编号只在该 K 内有效，不是人物身份。切换 K 会暂停音频。</p>
<p>代表源按随机起点稳定性、独立近邻 cosine 支持、欧氏间隔依次排序，并优先跨 chunk。分数不是身份概率；姓名仅为原 Understanding 的弱标签。所有代表源都不含留出目标、过短或已排除的片段。</p>
<p><a href='README.md'>主实验报告</a> · <a href='report.html'>全部439段时间映射</a> · <a href='granularity.json'>本页全部指标</a></p></header>
<div class='controls'><label>选择分组粒度：<select id='k'>{options}</select></label></div>{''.join(panels)}
<script>const picker=document.getElementById('k');const panels=[...document.querySelectorAll('.panel')];
function show(){{document.querySelectorAll('audio').forEach(a=>a.pause());panels.forEach(p=>p.hidden=p.dataset.k!==picker.value);history.replaceState(null,'','#k='+picker.value);}}
const requested=new URLSearchParams(location.hash.slice(1)).get('k');picker.value=[...picker.options].some(o=>o.value===requested)?requested:'{primary['selected_k']}';picker.addEventListener('change',show);show();
document.addEventListener('play',e=>{{if(e.target.tagName==='AUDIO')document.querySelectorAll('audio').forEach(a=>{{if(a!==e.target)a.pause();}});}},true);</script></html>"""
    (HERE / "granularity.html").write_text(page)
    print(json.dumps({"k_views": len(views), "models_refitted": 0, "primary_selected_k": primary["selected_k"]}))


if __name__ == "__main__":
    main()
