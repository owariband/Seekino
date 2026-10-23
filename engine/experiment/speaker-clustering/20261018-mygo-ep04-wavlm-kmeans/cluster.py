"""Cluster frozen WavLM embeddings; no model, production or database calls."""
from __future__ import annotations

import hashlib
import html
import json
import os
from pathlib import Path
import platform
import resource
import statistics
import sys
import time
import warnings
from collections import Counter, defaultdict
from datetime import UTC, datetime
from urllib.parse import quote

for variable in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "VECLIB_MAXIMUM_THREADS"):
    os.environ[variable] = "4"

import numpy as np
import scipy
from scipy.cluster.vq import ClusterError, kmeans2
from scipy.optimize import linear_sum_assignment

HERE = Path(__file__).resolve().parent


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def overlap(first, second):
    return first[0] < second[1] and second[0] < first[1]


def interval(sample):
    return sample["start_ms"], sample["end_ms"]


def squared_distances(values, centers):
    return np.maximum(0, np.sum(values * values, axis=1)[:, None]
                      + np.sum(centers * centers, axis=1)[None, :] - 2 * values @ centers.T)


def silhouettes(distances, labels, k):
    sizes = np.bincount(labels, minlength=k)
    averages = np.stack([distances[:, labels == group].mean(axis=1) for group in range(k)], axis=1)
    indices = np.arange(len(labels))
    intra = averages[indices, labels] * sizes[labels] / np.maximum(sizes[labels] - 1, 1)
    averages[indices, labels] = np.inf
    inter = averages.min(axis=1)
    result = np.divide(inter - intra, np.maximum(intra, inter), out=np.zeros_like(intra),
                       where=np.maximum(intra, inter) > 0)
    result[sizes[labels] == 1] = 0
    return result


def contingency(first, second, k):
    counts = np.zeros((k, k), dtype=np.int64)
    np.add.at(counts, (first, second), 1)
    return counts


def adjusted_rand(first, second, k):
    counts = contingency(first, second, k)
    choose_two = lambda values: np.sum(values * (values - 1) / 2)
    pairs = len(first) * (len(first) - 1) / 2
    together = choose_two(counts)
    row_pairs, column_pairs = choose_two(counts.sum(axis=1)), choose_two(counts.sum(axis=0))
    expected = row_pairs * column_pairs / pairs
    denominator = (row_pairs + column_pairs) / 2 - expected
    return float((together - expected) / denominator) if denominator else 1.0


def nearest_independent(index, candidates, samples, similarities, count):
    """At most one non-overlapping source per cue, excluding self and PCM reuse."""
    current = samples[index]
    used_cues, used_pcm, selected = {current["cue_id"]}, {current["pcm_sha256"]}, []
    for other in sorted(candidates, key=lambda item: (-similarities[index, item], samples[item]["start_ms"], samples[item]["id"])):
        sample = samples[other]
        if sample["cue_id"] in used_cues or sample["pcm_sha256"] in used_pcm:
            continue
        if overlap(interval(current), interval(sample)) or any(overlap(interval(samples[item]), interval(sample)) for item in selected):
            continue
        selected.append(other)
        used_cues.add(sample["cue_id"])
        used_pcm.add(sample["pcm_sha256"])
        if len(selected) == count:
            break
    return selected


def representative_indices(candidates, samples, ranking, count):
    selected = []
    # First cover separate chunks, then separate cues; every pass preserves rank.
    for require_distinct_chunk in (True, False):
        for index in sorted(candidates, key=ranking):
            sample = samples[index]
            if index in selected or any(sample["cue_id"] == samples[item]["cue_id"]
                                        or sample["pcm_sha256"] == samples[item]["pcm_sha256"]
                                        or overlap(interval(sample), interval(samples[item])) for item in selected):
                continue
            if require_distinct_chunk and any(sample["chunk_sequence"] == samples[item]["chunk_sequence"] for item in selected):
                continue
            selected.append(index)
            if len(selected) == count:
                return selected
    return selected


def cluster_name(value):
    return f"C{value + 1:02d}"


def number(value, digits=4):
    return "—" if value is None else f"{value:.{digits}f}"


def timestamp(milliseconds):
    minutes, remainder = divmod(milliseconds, 60000)
    return f"{minutes:02d}:{remainder / 1000:06.3f}"


def render(result, samples, embedding_provenance):
    mapping = {row["id"]: row for row in result["assignments"]}
    source = {sample["id"]: sample for sample in samples}
    sweep_rows = []
    for sweep in result["k_scan"]:
        sweep_rows.append(f"| {sweep['k']} | {sweep['median_cosine_silhouette']:.6f} | {sweep['final_sse']:.6f} | "
                          f"{min(row['ari_to_final'] for row in sweep['groups']):.6f} | "
                          f"{', '.join(str(item) for item in sweep['cluster_sizes'])} | {sweep['final_seed']} |")
    holdout_rows = []
    conditions = {item['id']: item for item in result['held_out_diagnostic_conditions']}
    for sample in samples:
        if sample['id'] in conditions:
            assigned = mapping[sample["id"]]
            holdout_rows.append(f"| {sample['id']} | {timestamp(sample['start_ms'])}–{timestamp(sample['end_ms'])} | "
                                f"{conditions[sample['id']]['kind']} / {conditions[sample['id']]['known_actual_speaker']} | {cluster_name(assigned['cluster'])} | "
                                f"{assigned['aligned_membership_count']}/5 | {assigned['euclidean_margin']:.6f} |")
    weak_rows = []
    for name, value in result["weak_speaker_coverage"].items():
        distribution = ' · '.join(f"{cluster_name(int(key))}:{count}" for key, count in value["cluster_asr_windows"].items())
        weak_rows.append(f"| {name} | {value['asr_windows']} / {value['asr_cues']} | {value['fit_windows']} | {distribution} |")
    embedding_times = result["embedding_resources"]
    readme = f"""# 第四集全 ASR 来源：WavLM + K-means 离线实验

由冻结 protocol 驱动；生产改动 0 行，无数据库／服务／解析任务操作。

**当前自动 K=2 是过粗的几何结果，不能作为逐人物身份簇或认证声纹库。** 两句已确认来自不同人物（爱音、爽世）的留出条件都落入 C01；拟合成员也混有爱音／爽世等多个弱标签。没有据此强行命名或调整主选法。

[打开离线试听图库](report.html) · [全部K的粒度探索](granularity.html) · [全部分组与指标](clustering.json) · [输入清单](manifest.json) · [冻结协议](protocol.json)

粒度探索仅复用已经完整预算的 K=2…12 模型做下拉试听；没有重新拟合或依据 holdout 改选主 K，也不作为额外的独立成功验收。

## 输入与排除

24 个 chunk、378 条 ASR cue 全部计入：拆成 {result['source_summary']['asr_sample_count']} 个不重叠短窗，另有 3 个 holdout，共 {len(samples)} 个向量；{result['fit_count']} 个参与拟合。长 cue 等分为不超过 3 秒的窗；没有把 ASR 匿名 speaker_id 或 Understanding 人名作为约束。一个 cue 即使被切窗也不保证单人，ASR 时间可能含两人的连续声音。

排除计数（可能一窗有多个原因）：`{json.dumps(result['source_summary']['exclusion_reason_counts'], ensure_ascii=False)}`。短于 1.5 秒、已标 OP/ED、低于 −50dBFS、重叠 cue、重复 PCM、holdout 相关 cue 不参与拟合或代表源。其余窗仍编码、分配和展示，不能把该分配解释成通过质量门禁。音乐范围来自保存的场景／歌词人工标记；RMS 只筛低能量／可能静音，二者都不是语音／音乐或多人检测器。

两个经典台词区间及与它们重叠的完整 ASR cue、重复音源在拟合、K 选择和代表源选择中均留出。3 个 holdout 包括同一爽世台词的 1.3 秒与 1.0 秒边界变体；不是 3 句独立真值。

## 算法与 K 扫描

使用 SciPy {scipy.__version__} `kmeans2`，输入 L2 单位向量，普通欧氏 K-means，**不是 spherical K-means**。不 whitening、不 PCA、不归一化簇中心。K=2…12，5 组 seeds×每组10 starts，++ 初始化、每次100轮；每组取 SSE 最低，五组胜者中 SSE 最低者作为该 K 的最终模型。K 由五组 cosine silhouette 中位数最高选择，完全并列取小 K；不读取人物名或 holdout 得分。当前选择 **K={result['selected_k']}，seed={result['selected_seed']}**，是此范围内几何指标的选择，不能当作已证明的人物数量。

| K | silhouette 中位数（cosine） | 最佳 SSE（欧氏） | 五组对最终模型的最小 ARI | 最终拟合簇大小 | 最终 seed |
|---|---:|---:|---:|---|---:|
{chr(10).join(sweep_rows)}

所有分配与 holdout 预测使用原 K-means 中心的最近欧氏平方距离；中心 cosine 另列诊断，未用于改写预测。每个来源报告5组对齐后的归属次数、同簇至多10条独立 cue 近邻的平均 cosine、次近中心减本簇的欧氏平方距离差。它们不是身份概率；5/5 仅表示同数据的随机初始化稳定性，不是泛化验证。

本机 NumPy matmul 出现浮点状态告警；没有仅依靠 clip 掩盖。原始点积全有限且有界，与非BLAS `einsum(optimize=False)`最大差 {result['numerical_diagnostics']['maximum_raw_cosine_einsum_difference']:.3g}；直接欧氏平方差与保存距离最大差 {result['numerical_diagnostics']['maximum_direct_euclidean_distance_difference']:.3g}；55个组胜者的中心与最终标签均值最大残差 {result['numerical_diagnostics']['maximum_group_center_mean_residual']:.3g}。数值断言和告警原文见JSON。

代表源按上述三项依次排序，优先不同 chunk、再不同 cue，选择至多3条；另列最靠近分界的一条未选拟合窗。不重叠、不重复、不用留出或排除窗凑数。高声学支持仍可能是同一情绪／背景或同一段错误切分，身份需要试听核验。簇小或证据不足时保留缺额，没有将小簇强塞进大簇。

## 冻结后才查看的已确认条件

| 目标 | 区间 | 来源边界 / 对应确认人物 | 匿名簇（欧氏） | 五组对齐归属 | 欧氏平方距离差 |
|---|---|---|---|---:|---:|
{chr(10).join(holdout_rows)}

列出3个历史冻结query及2个当前ASR实际cue边界；共5个条件仍然只对应两句独立确认台词。ASR相关cue和历史query从一开始均不拟合，不用它们挑K或代表源。这里只输出匿名簇；须听簇代表源才能判断身份，不会由弱标签多数票自动给它命名。

## 所有弱人物标签的覆盖

标签仅来自重叠 Understanding DIALOGUE，可能错标、多名或缺名；下面是覆盖与回查线索，**不是人物真值或认证声库**。多标签窗会在多行各计一次；另列无具名标签。

| 弱标签 | ASR 窗数 / 独立 cue 数 | 拟合窗数 | 全部 ASR 窗落簇分布 |
|---|---:|---:|---|
{chr(10).join(weak_rows)}

图库包含各弱标签至多2条回查源、每匿名簇代表／边界源，以及全部 ASR／holdout 的可检索时间映射。1.5秒规则使乐奈、睦等少台词人物没有拟合窗；这些人物的分配只是在其他窗拟合的中心上预测，不能说已形成其独立声音组。角色可分散到多个簇，同簇也可能混人；没有因为 K 较小就宣称找到所有人物。本实验没有全量人工身份／区间真值，未计算 DER 或人物准确率。

## 来源、资源与复现

编码器仍为 Python 3.12 原生 CPU 兼容路径的 WavLM-Large 专用 SV（官方 standalone WavLM、原 ECAPA 及25层 hooks）。同批47段曾与官方指定旧版本链路近似等价，但本次不是完整历史训练环境。SV checkpoint 来自第三方 UniAudio 作者镜像，SHA `51f07e3b94d9e0262a6a675ef5a087be3dd09e8c62e9d886827f44f82fe7f94b`；微软原件不可下载，不能直接证明镜像与原件逐字节一致。没有以通用 WavLM 均值代替专用 SV。

本次编码：平台 `{embedding_provenance['platform']}`，CPU／FP32／4线程／batch1；{len(samples)} 窗累计前向 {embedding_times['total_forward_ms'] / 1000:.3f}s，中位 {embedding_times['median_forward_ms']:.3f}ms，加载 {embedding_provenance['model_load_seconds']:.3f}s，预热 {embedding_provenance['warmup_ms']:.3f}ms，进程峰 RSS {embedding_provenance['peak_rss_bytes'] / 1e9:.3f}GB。编码耗时不含下载／导入／加载，数学聚类另耗时 {result['clustering_resources']['elapsed_seconds']:.3f}s；不能据此推算生产并发吞吐。[编码来源](embedding-provenance.json) · [来源准备记录](preparation.json)。

输入、协议、embedding、代码哈希和每次 start 的 seed／SSE／失败、组胜者中心与标签、ARI、最终映射均在 clustering.json。保留冻结 JSON 与音频；复算不要修改当前参数或覆盖旧输入。代码只读它们，运行后会更新本次派生 clustering.json／README／report，未写生产。

```sh
.venv/bin/python engine/experiment/speaker-clustering/20261018-mygo-ep04-wavlm-kmeans/cluster.py
```

一手算法文档：[SciPy 1.15.3 kmeans2](https://docs.scipy.org/doc/scipy-1.15.3/reference/generated/scipy.cluster.vq.kmeans2.html)、[silhouette](https://scikit-learn.org/stable/modules/generated/sklearn.metrics.silhouette_score.html)、[ARI](https://scikit-learn.org/stable/modules/generated/sklearn.metrics.adjusted_rand_score.html)。实验目录被 Git 忽略；没有安装新依赖或生成降维图。
"""
    (HERE / "README.md").write_text(readme)
    esc = lambda value: html.escape(str(value), quote=True)

    def audio_card(sample_id, reason):
        sample, assigned = source[sample_id], mapping[sample_id]
        return f"<article><b>{esc(reason)} · {esc(sample_id)}</b><p>{timestamp(sample['start_ms'])}–{timestamp(sample['end_ms'])} · chunk {sample['chunk_sequence']} · 弱标签 {esc(' / '.join(sample['weak_speakers']) or '无具名标签')}</p><p>{esc(sample.get('text') or '')}</p><p>归属 {assigned['aligned_membership_count']}/5 · 独立近邻支持 {number(assigned['same_cluster_support'])} ({assigned['support_count']} cues) · 欧氏间隔 {number(assigned['euclidean_margin'])}</p><p>{esc(' / '.join(sample['exclusion_reasons']) or '参与拟合；身份待核验')}</p><audio controls preload='none' src='{quote(sample['audio'], safe='/')}'></audio></article>"

    gallery = []
    for cluster in result["clusters"]:
        labels = ' · '.join(f"{esc(name)}:{count}" for name, count in cluster["fit_weak_label_counts"].items()) or '无具名标签'
        cards = ''.join(audio_card(sample_id, '代表源，身份未认证') for sample_id in cluster["representative_ids"])
        if cluster["boundary_example_id"]:
            cards += audio_card(cluster["boundary_example_id"], '欧氏分界最接近的未选拟合源')
        search_text = labels + ' ' + cluster_name(cluster['cluster']) + ' ' + ' '.join(
            sample_id + ' ' + (source[sample_id].get('text') or '')
            for sample_id in cluster['representative_ids'] + ([cluster['boundary_example_id']] if cluster['boundary_example_id'] else []))
        gallery.append(f"<section class='searchable' data-search='{esc(search_text)}'><h2>{cluster_name(cluster['cluster'])} · 拟合{cluster['fit_count']}窗／{cluster['fit_cue_count']} cues／{cluster['fit_chunk_count']} chunks</h2><p>弱标签诊断：{labels}。不作为簇的认证姓名。</p><div class='cards'>{cards}</div></section>")
    weak_cards = ''.join(f"<section class='searchable' data-search='{esc(name)}'><h3>{esc(name)} · {value['asr_windows']}窗/{value['asr_cues']} cues，拟合{value['fit_windows']}窗</h3><div class='cards'>{''.join(audio_card(sample_id, '按弱姓名回查，未认证') for sample_id in value['listening_hint_ids'])}</div></section>" for name, value in result["weak_speaker_coverage"].items())
    table = []
    for sample in samples:
        row = mapping[sample["id"]]
        labels = ' / '.join(sample["weak_speakers"]) or '无具名标签'
        search = ' '.join([sample['id'], str(sample['cue_id']), sample.get('text') or '', labels,
                           cluster_name(row['cluster']), sample['role'], ' '.join(sample['exclusion_reasons'])])
        table.append(f"<tr class='searchable' data-search='{esc(search)}'><td>{esc(sample['id'])}<br>{esc(sample['cue_id'])}</td><td>{sample['chunk_sequence']}</td><td>{timestamp(sample['start_ms'])}<br>{timestamp(sample['end_ms'])}</td><td>{cluster_name(row['cluster'])}</td><td>{esc(sample['role'])}<br>{esc(' / '.join(sample['exclusion_reasons']) or 'fit')}</td><td>{row['aligned_membership_count']}/5<br>{number(row['same_cluster_support'])}<br>{number(row['euclidean_margin'])}</td><td>{esc(labels)}</td><td>{esc(sample.get('text') or '')}</td><td><audio controls preload='none' src='{quote(sample['audio'], safe='/')}'></audio></td></tr>")
    sweep_html = ''.join(f"<tr><td>{item['k']}</td><td>{item['median_cosine_silhouette']:.6f}</td><td>{item['final_sse']:.4f}</td><td>{min(group['ari_to_final'] for group in item['groups']):.4f}</td><td>{esc(item['cluster_sizes'])}</td></tr>" for item in result['k_scan'])
    condition_cards = ''.join(audio_card(item['id'], item['kind'] + '；对应确认人物 ' + item['known_actual_speaker'])
                              for item in result['held_out_diagnostic_conditions'])
    page = f"""<!doctype html><html lang='zh-CN'><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'><title>第四集 WavLM 匿名声音簇</title><style>body{{font:15px/1.6 system-ui;margin:24px auto;max-width:1500px;padding:0 18px;background:#f6f7fa;color:#17202a}}header,section{{background:white;padding:18px;margin:16px 0;border:1px solid #dbe0e7;border-radius:10px}}h1,h2,h3{{line-height:1.3}}.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:12px}}article{{background:#f4f6f9;padding:12px;border-radius:6px;overflow-wrap:anywhere}}audio{{max-width:100%;width:270px}}input{{width:min(700px,90%);padding:12px;font:inherit}}.filter{{position:sticky;top:0;background:#e9eef5;padding:12px;z-index:2}}table{{border-collapse:collapse;width:100%;font-size:13px}}th,td{{border:1px solid #dbe0e7;padding:7px;text-align:left;vertical-align:top}}.scroll{{overflow:auto}}.notice{{color:#8c3f00}}[hidden]{{display:none!important}}</style><header><h1>第四集：WavLM + K-means 匿名声音簇</h1><p>24 chunks / 378 ASR cues → 436 ASR windows + 3 holdout；{result['fit_count']}窗拟合，选 K={result['selected_k']}。</p><p class='notice'><strong>自动K2过粗，不能作为逐人物身份簇：已确认不同人的爱音／爽世留出条件都落C01。</strong> 几何分组与来源候选，不是认证人物声库。弱姓名可能错标；簇数不是已确认人数；没有全量真值、DER或身份准确率。短/歌曲等排除窗虽可预测，不代表质量通过。</p><p><a href='README.md'>完整方法与限制</a> · <a href='granularity.html'>全部K粒度探索（复用预扫，不改主选法）</a> · <a href='clustering.json'>所有分数/映射/来源</a> · <a href='manifest.json'>冻结输入</a></p></header><div class='filter'><label>检索簇、人物弱标签、台词、cue、排除原因：<br><input id='search' placeholder='例如 C01 / 长崎爽世 / asr_c00 / holdout'></label> <span id='count'></span></div><section><h2>全部K扫描</h2><table><tr><th>K</th><th>cosine silhouette中位数</th><th>欧氏SSE</th><th>最小ARI</th><th>拟合簇大小</th></tr>{sweep_html}</table><p>550 starts，先欧氏SSE选组胜者，再按silhouette中位数选K；下方所有人物名仅事后弱标签诊断。</p></section><section><h2>留出目标：冻结选择后预测</h2><p>两句独立用户真值；3个历史query与2个当前ASR cue边界，共5条件。全部从拟合、挑K和挑来源中排除。</p><div class='cards'>{condition_cards}</div></section>{''.join(gallery)}<section><h2>所有弱标签来源覆盖</h2><p>包括稀少NPC及无具名标签。每名至多2条回查线索；可能被排除拟合，或仍是错标。不能当已核验参考。</p></section>{weak_cards}<section><h2>全部ASR/query时间映射（{len(samples)}条）</h2><p>指标顺序：5组同归属次数／同簇独立近邻cos支持／欧氏平方距离差；不是概率。完整中心距离与cos诊断见JSON。</p><div class='scroll'><table><thead><tr><th>样本/cue</th><th>chunk</th><th>起/止</th><th>匿名簇</th><th>角色/排除</th><th>稳定/支持/间隔</th><th>弱标签</th><th>原ASR文本</th><th>音频</th></tr></thead><tbody>{''.join(table)}</tbody></table></div></section><script>const search=document.getElementById('search');const items=[...document.querySelectorAll('.searchable')];function filter(){{const value=search.value.trim().toLowerCase();let shown=0;items.forEach(item=>{{item.hidden=!item.dataset.search.toLowerCase().includes(value);if(!item.hidden)shown++;}});document.getElementById('count').textContent=`${{shown}} / ${{items.length}} 项`;}}search.addEventListener('input',filter);filter();document.addEventListener('play',event=>{{if(event.target.tagName==='AUDIO')document.querySelectorAll('audio').forEach(item=>{{if(item!==event.target)item.pause();}});}},true);</script></html>"""
    (HERE / "report.html").write_text(page)


def main():
    started = time.perf_counter()
    protocol = json.loads((HERE / "protocol.json").read_text())
    manifest = json.loads((HERE / "manifest.json").read_text())
    assert json.loads((HERE / "embedding-status.json").read_text())["status"] == "complete"
    embedding_provenance = json.loads((HERE / "embedding-provenance.json").read_text())
    embedded = json.loads((HERE / "embeddings.json").read_text())
    samples = manifest["samples"]
    assert embedding_provenance['manifest_sha256'] == sha(HERE / 'manifest.json')
    assert embedding_provenance['protocol_sha256'] == sha(HERE / 'protocol.json')
    assert manifest['inputs']['asr_snapshot']['sha256'] == protocol['source_sha256']
    assert [row['id'] for row in embedded] == [row['id'] for row in samples]
    assert len(set(row['id'] for row in samples)) == len(samples)
    values = np.array([row['vector'] for row in embedded], dtype=np.float64)
    assert values.shape == (len(samples), 256) and np.isfinite(values).all()
    norms = np.linalg.norm(values, axis=1)
    assert np.max(np.abs(norms - 1)) < 1e-5
    values /= norms[:, None]
    for sample, row in zip(samples, embedded):
        assert sample['pcm_sha256'] == row['pcm_sha256']
        assert (HERE / sample['audio']).resolve().is_relative_to(HERE)
    asr = [sample for sample in samples if sample['role'] == 'asr']
    assert len({row['cue_id'] for row in asr}) == 378
    assert len({row['chunk_sequence'] for row in asr}) == 24
    fit_indices = np.array([index for index, row in enumerate(samples) if row['fit_eligible']], dtype=int)
    fit_samples = [samples[index] for index in fit_indices]
    assert len(fit_indices) == manifest['summary']['fit_sample_count']
    assert len({row['pcm_sha256'] for row in fit_samples}) == len(fit_samples)
    holdout_pcm = {row['pcm_sha256'] for row in samples if row['role'] == 'holdout'}
    for sample in fit_samples:
        assert sample['role'] == 'asr' and not sample['exclusion_reasons']
        assert sample['end_ms'] - sample['start_ms'] >= protocol['segments']['minimum_fit_ms']
        assert sample['pcm_sha256'] not in holdout_pcm
        assert not any(overlap((sample['cue_start_ms'], sample['cue_end_ms']), held) for held in protocol['held_out_intervals_ms'])
        assert not any(overlap(interval(sample), music) for music in protocol['known_music_intervals_ms'])
        assert sample['rms_dbfs'] >= protocol['segments']['silence_below_dbfs']
    fitting = values[fit_indices]
    fit_cos = np.clip(fitting @ fitting.T, -1, 1)
    fit_distances = np.clip(1 - fit_cos, 0, 2)
    np.fill_diagonal(fit_distances, 0)
    scans = []
    for k in protocol['k_values']:
        assert 2 <= k < len(fitting)
        groups, starts = [], []
        for group in protocol['seed_groups']:
            winner = None
            for start in range(protocol['starts_per_group']):
                seed = 1000 * group + start
                try:
                    centers, _ = kmeans2(fitting, k, iter=protocol['lloyd_iterations'], minit=protocol['initialization'],
                                         missing='raise', rng=np.random.default_rng(seed))
                    distances = squared_distances(fitting, centers)
                    labels = distances.argmin(axis=1)
                    assert len(np.unique(labels)) == k, 'Empty cluster after final prediction'
                    sse = float(distances[np.arange(len(fitting)), labels].sum())
                    starts.append({'group': group, 'start': start, 'seed': seed, 'sse': sse, 'status': 'success'})
                    if winner is None or sse < winner['sse']:
                        winner = {'group': group, 'seed': seed, 'sse': sse, 'centers': centers, 'labels': labels}
                except (ClusterError, AssertionError) as error:
                    starts.append({'group': group, 'start': start, 'seed': seed, 'status': 'failed', 'error': str(error)})
            if winner is None:
                raise RuntimeError(f'No successful starts for K={k}, group={group}')
            winner['silhouette'] = float(silhouettes(fit_distances, winner['labels'], k).mean())
            groups.append(winner)
        final = min(groups, key=lambda item: item['sse'])
        # Canonical display IDs follow first fitted occurrence, never weak names.
        order = sorted(range(k), key=lambda label: min((fit_samples[index]['start_ms'], index) for index in np.flatnonzero(final['labels'] == label)))
        inverse = np.argsort(order)
        final_centers, final_labels = final['centers'][order], inverse[final['labels']]
        encoded_groups = []
        for group in groups:
            counts = contingency(group['labels'], final_labels, k)
            left, right = linear_sum_assignment(-counts)
            alignment = np.empty(k, dtype=int)
            alignment[left] = right
            encoded_groups.append({'group': group['group'], 'seed': group['seed'], 'sse': group['sse'],
                                   'cosine_silhouette': group['silhouette'], 'ari_to_final': adjusted_rand(group['labels'], final_labels, k),
                                   'label_alignment': alignment.tolist(), 'labels': group['labels'].tolist(), 'centers': group['centers'].tolist()})
        scans.append({'k': k, 'median_cosine_silhouette': statistics.median(group['silhouette'] for group in groups),
                      'final_sse': final['sse'], 'final_seed': final['seed'], 'centers': final_centers.tolist(),
                      'labels': final_labels.tolist(), 'cluster_sizes': np.bincount(final_labels, minlength=k).tolist(),
                      'groups': encoded_groups, 'starts': starts})
        print(json.dumps({'k': k, 'median_silhouette': scans[-1]['median_cosine_silhouette'],
                          'sse': final['sse'], 'cluster_sizes': scans[-1]['cluster_sizes']}), flush=True)
    selected = min(scans, key=lambda item: (-item['median_cosine_silhouette'], item['k']))
    k = selected['k']
    centers = np.array(selected['centers'])
    distances = squared_distances(values, centers)
    labels = distances.argmin(axis=1)
    np.testing.assert_array_equal(labels[fit_indices], selected['labels'])
    group_predictions = []
    for group in selected['groups']:
        predicted = squared_distances(values, np.array(group['centers'])).argmin(axis=1)
        group_predictions.append(np.array(group['label_alignment'])[predicted])
    stable = (np.stack(group_predictions) == labels).sum(axis=0)
    sorted_distances = np.sort(distances, axis=1)
    margins = sorted_distances[:, 1] - sorted_distances[:, 0]
    center_norms = np.linalg.norm(centers, axis=1)
    assert np.all(center_norms > 0)
    center_cosines = values @ (centers / center_norms[:, None]).T
    similarities = np.clip(values @ values.T, -1, 1)
    supports, support_ids = [], []
    for index in range(len(samples)):
        candidates = [int(other) for other in fit_indices if labels[other] == labels[index]]
        near = nearest_independent(index, candidates, samples, similarities, 10)
        support_ids.append([samples[item]['id'] for item in near])
        supports.append(float(similarities[index, near].mean()) if near else None)
    def rank(index):
        return (-int(stable[index]), -supports[index] if supports[index] is not None else float('inf'),
                -float(margins[index]), samples[index]['start_ms'], samples[index]['id'])
    assignments = []
    for index, sample in enumerate(samples):
        assignments.append({'id': sample['id'], 'role': sample['role'], 'cue_id': sample['cue_id'],
                            'chunk_sequence': sample['chunk_sequence'], 'start_ms': sample['start_ms'], 'end_ms': sample['end_ms'],
                            'audio': sample['audio'], 'pcm_sha256': sample['pcm_sha256'], 'text': sample.get('text'),
                            'weak_speakers': sample['weak_speakers'], 'known_actual_speaker': sample.get('known_actual_speaker'),
                            'fit_eligible': sample['fit_eligible'], 'exclusion_reasons': sample['exclusion_reasons'],
                            'cluster': int(labels[index]), 'aligned_membership_count': int(stable[index]),
                            'same_cluster_support': supports[index], 'support_count': len(support_ids[index]), 'support_ids': support_ids[index],
                            'euclidean_margin': float(margins[index]), 'squared_center_distances': distances[index].tolist(),
                            'normalized_center_cosines_diagnostic_only': center_cosines[index].tolist()})
    clusters = []
    for cluster in range(k):
        indices = [int(index) for index in fit_indices if labels[index] == cluster]
        representatives = representative_indices(indices, samples, rank, protocol['representatives']['count_per_cluster'])
        remaining = [index for index in indices if index not in representatives]
        boundary = min(remaining, key=lambda index: (margins[index], supports[index] if supports[index] is not None else -2, samples[index]['start_ms'])) if remaining else None
        clusters.append({'cluster': cluster, 'fit_count': len(indices), 'fit_cue_count': len({samples[index]['cue_id'] for index in indices}),
                         'fit_chunk_count': len({samples[index]['chunk_sequence'] for index in indices}),
                         'representative_ids': [samples[index]['id'] for index in representatives],
                         'boundary_example_id': samples[boundary]['id'] if boundary is not None else None,
                         'boundary_selection': 'Smallest Euclidean squared-distance margin among unselected fitted members',
                         'fit_weak_label_counts': dict(Counter(name for index in indices for name in (samples[index]['weak_speakers'] or ['[无具名标签]'])))})
    weak = defaultdict(list)
    for index, sample in enumerate(samples):
        if sample['role'] == 'asr':
            for name in sample['weak_speakers'] or ['[无具名标签]']:
                weak[name].append(index)
    weak_coverage = {}
    for name in sorted(weak):
        indices = weak[name]
        fit = [index for index in indices if samples[index]['fit_eligible']]
        hints = representative_indices(fit or indices, samples, rank, 2)
        weak_coverage[name] = {'asr_windows': len(indices), 'asr_cues': len({samples[index]['cue_id'] for index in indices}),
                               'chunks': sorted({samples[index]['chunk_sequence'] for index in indices}), 'fit_windows': len(fit),
                               'cluster_asr_windows': {str(label): sum(labels[index] == label for index in indices).item() for label in range(k) if any(labels[index] == label for index in indices)},
                               'listening_hint_ids': [samples[index]['id'] for index in hints],
                               'hint_basis': 'Weak-name lookup only; prefer fitted members, excluded sources possible if no fitted evidence; not certified identity references'}
    times = [row['embedding_ms'] for row in embedded]
    conditions = []
    frozen_queries = [sample for sample in samples if sample['role'] == 'holdout']
    for sample in samples:
        if sample['role'] == 'holdout':
            conditions.append({'id': sample['id'], 'kind': '历史冻结query', 'known_actual_speaker': sample['known_actual_speaker']})
        elif 'holdout_related_cue' in sample['exclusion_reasons']:
            confirmed = {query['known_actual_speaker'] for query in frozen_queries if overlap(interval(sample), interval(query))}
            assert len(confirmed) == 1
            conditions.append({'id': sample['id'], 'kind': '当前ASR cue边界', 'known_actual_speaker': next(iter(confirmed))})
    # Check raw BLAS results before clipping against independent scalar reductions.
    # Some native Accelerate calls set FP status flags although output is finite.
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter('always', RuntimeWarning)
        raw_cosines = values @ values.T
        raw_center_cosines = values @ (centers / center_norms[:, None]).T
    independent_cosines = np.einsum('id,jd->ij', values, values, optimize=False)
    independent_center_cosines = np.einsum('id,jd->ij', values, centers / center_norms[:, None], optimize=False)
    direct_distances = np.sum((values[:, None, :] - centers[None, :, :]) ** 2, axis=2)
    cosine_error = float(np.max(np.abs(raw_cosines - independent_cosines)))
    center_cosine_error = float(np.max(np.abs(raw_center_cosines - independent_center_cosines)))
    distance_error = float(np.max(np.abs(distances - direct_distances)))
    assert np.isfinite(raw_cosines).all() and np.isfinite(raw_center_cosines).all()
    assert raw_cosines.min() >= -1 - 1e-12 and raw_cosines.max() <= 1 + 1e-12
    assert np.isfinite(distances).all() and np.all(distances >= 0)
    assert cosine_error < 1e-12 and center_cosine_error < 1e-12 and distance_error < 1e-12
    np.testing.assert_array_equal(labels, direct_distances.argmin(axis=1))
    center_residual = 0.0
    for scan in scans:
        for group in scan['groups']:
            group_labels = np.array(group['labels'])
            actual_centers = np.array(group['centers'])
            means = np.stack([fitting[group_labels == label].mean(axis=0) for label in range(scan['k'])])
            center_residual = max(center_residual, float(np.max(np.abs(actual_centers - means))))
            assert np.isfinite(actual_centers).all() and np.max(np.linalg.norm(actual_centers, axis=1)) <= 1 + 1e-12
    assert center_residual < 1e-12, '100-round result has not reached consistent centers/labels'
    diagnostics = {'raw_cosines_finite': True, 'raw_cosine_min': float(raw_cosines.min()), 'raw_cosine_max': float(raw_cosines.max()),
                   'maximum_raw_cosine_einsum_difference': cosine_error, 'maximum_center_cosine_einsum_difference': center_cosine_error,
                   'maximum_direct_euclidean_distance_difference': distance_error, 'direct_euclidean_labels_identical': True,
                   'maximum_group_center_mean_residual': center_residual, 'checked_group_winners': sum(len(scan['groups']) for scan in scans),
                   'comparison_absolute_tolerance': 1e-12, 'captured_raw_matmul_warnings': [str(item.message) for item in caught],
                   'note': 'Raw output validated before clip; warnings do not substitute for finite/bounded/equivalence checks.'}
    result = {'created_at': datetime.now(UTC).isoformat(), 'algorithm': protocol['algorithm'], 'protocol': protocol,
              'inputs': {name: sha(HERE / name) for name in ['protocol.json', 'manifest.json', 'embeddings.json', 'embedding-provenance.json', 'cluster.py']},
              'source_summary': manifest['summary'], 'sample_count': len(samples), 'fit_count': len(fit_indices),
              'fit_sample_ids': [samples[index]['id'] for index in fit_indices], 'input_max_unit_norm_error': float(np.max(np.abs(norms - 1))),
              'selected_k': k, 'selected_seed': selected['final_seed'], 'selection_uses_weak_labels_or_holdouts': False,
              'k_scan': scans, 'assignments': assignments, 'clusters': clusters, 'weak_speaker_coverage': weak_coverage,
              'held_out_diagnostic_conditions': conditions,
              'numerical_diagnostics': diagnostics,
              'embedding_resources': {'total_forward_ms': sum(times), 'median_forward_ms': statistics.median(times),
                                      'provenance': 'embedding-provenance.json'},
              'clustering_resources': {'elapsed_seconds': time.perf_counter() - started, 'peak_rss_bytes': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                                       'python': sys.version, 'numpy': np.__version__, 'scipy': scipy.__version__, 'platform': platform.platform(), 'blas_thread_limit': 4},
              'limits': ['Weak labels are not identity ground truth.', 'K is a geometric selection, not a certified number of people.',
                         'K-means forces a nearest cluster even for excluded/noisy/short sources; no such assignment certifies identity.',
                         'Manual OP/ED intervals and RMS do not detect all music, speech or mixed speakers.',
                         'Two confirmed utterances with three boundary conditions are held out; no episode accuracy or DER.',
                         'Same third-party mirrored WavLM SV weights; Microsoft original equivalence remains unverified.']}
    (HERE / 'clustering.json').write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + '\n')
    render(result, samples, embedding_provenance)
    print(json.dumps({'selected_k': k, 'selected_seed': selected['final_seed'], 'sample_count': len(samples),
                      'fit_count': len(fit_indices), 'outputs': ['clustering.json', 'README.md', 'report.html']}), flush=True)


if __name__ == '__main__':
    main()
