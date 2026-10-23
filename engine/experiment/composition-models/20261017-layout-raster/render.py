"""Render layout-raster embeddings as an offline report and inspection sheets."""
import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

ROOT = Path(__file__).resolve().parent
TEMPLATE = r'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>结构图 → DINOv2 Small · 本地实验</title>
<style>
:root{font:14px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:#26374e;background:#f3f5f8}*{box-sizing:border-box}body{margin:0}main{max-width:1480px;margin:auto;padding:26px}h1{margin:0;font-size:29px}h2{font-size:20px;margin:26px 0 12px}h3{font-size:15px;margin:0 0 9px}p{margin:6px 0}.muted,small{color:#63738a}.notice{background:#fff5df;border:1px solid #ecd9af;padding:13px 16px;border-radius:8px;margin:17px 0}.toolbar{position:sticky;top:0;z-index:2;background:#fffffff2;display:flex;align-items:center;gap:20px;padding:12px 15px;border:1px solid #dce3ed;border-radius:8px}select,button{font:inherit}select{padding:6px 10px;border:1px solid #cbd5e2;border-radius:5px;background:white}.grid{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}.card,.rank{background:white;border:1px solid #dce3ed;border-radius:8px;padding:13px;min-width:0}.rank{margin:12px 0}.rank-head{display:flex;justify-content:space-between;gap:12px;align-items:baseline;flex-wrap:wrap}.candidates{display:grid;gap:11px}.picture{display:block;padding:0;width:100%;border:1px solid #e3e7ed;border-radius:5px;background:white;cursor:zoom-in;overflow:hidden}.picture img{width:100%;height:164px;object-fit:contain;display:block}.candidate .picture{margin-bottom:5px}.candidate .picture img{height:130px}.candidate p{font-size:12px;overflow-wrap:anywhere}.link{padding:0;border:0;background:none;color:#315b94;cursor:pointer;text-align:left}.good{color:#267957}.bad{color:#a95044}.tags{font-size:12px;color:#4d6484}.table{overflow:auto;background:white;border:1px solid #dce3ed;border-radius:7px}table{border-collapse:collapse;width:100%;font-size:12px}td,th{text-align:left;padding:9px;border-bottom:1px solid #e6eaf0;white-space:nowrap}th{background:#eaf0f7}td small{display:block}.win{background:#edf8f1}.loss{background:#fff0ec}.tie{background:#fff9e6}details{margin:18px 0}summary{cursor:pointer}pre{font-size:11px;background:#eaf0f7;padding:12px;overflow:auto;max-height:350px;white-space:pre-wrap}dialog{border:0;border-radius:9px;padding:12px;max-width:96vw;max-height:96vh}dialog::backdrop{background:#132033db}dialog img{max-width:90vw;max-height:82vh;display:block;object-fit:contain}dialog button{float:right;padding:5px 14px}dialog p{clear:both}.sensitivity{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}footer{font-size:12px;color:#64758b;margin:25px 0}a{color:#315b94}@media(max-width:950px){.grid,.sensitivity{grid-template-columns:repeat(2,1fr)}.candidate .picture img{height:100px}}@media(max-width:600px){main{padding:15px}.grid,.sensitivity{grid-template-columns:1fr}.toolbar{flex-wrap:wrap;gap:8px}.candidates{grid-template-columns:repeat(2,1fr)!important}.picture img{height:190px}}
</style></head><body><main>
<div class="notice"><strong>要调 PCP / 检测框权重？</strong> <a href="../20261017-pcp-box-fusion/report.html"><strong>打开「PCP ＋ 检测框」可调权重实验 →</strong></a><p>当前页是上一轮四种输入对比；权重滑杆位于新页面顶部，默认 PCP 80% / 检测框 20%。</p></div>
<h1>结构图 → DINOv2 Small</h1><p class="muted">同一个冻结的视觉编码器，比较原图、全部检测框、自动长线段、框与线合成图。点击图片放大，点击候选名称切换查询。</p>
<div class="notice"><strong>本轮检验：</strong>把框／线画到空白画布后，现成 embedding 是否能检索粗构图相近的画面？<br>框保留 Grounding DINO 全部阈值后结果，无类别文字、无 NMS；线来自 LSD 自动筛选的最长最多 12 条，长度至少为画面对角线的 15%，不等同人工判断的主要构图线。余弦相似度不是概率。参考标签沿用助手目视标签，17 张图的探索结果不能视为标准基准成绩。</div>
<div class="toolbar"><label>查询图片 <select id="query"></select></label><label>显示候选 <select id="top"><option value="3">Top 3</option><option value="5">Top 5</option></select></label><span class="muted">17 张 · 同一 DINOv2 Small · 384 维 CLS</span></div>
<h2>编码前的统一画布（448 → 224）</h2><div id="inputs" class="grid"></div><p id="evidence" class="muted"></p>
<h2>检索结果</h2><p class="muted">每个候选上方是原画面，下方是该组编码输入。无框／无线的空结构图不参与排名；查询本身被排除。共同参考标签只是粗类别重叠提示。</p><div id="rankings"></div>
<h2>固定参考对照</h2><div id="summary" class="table"></div><p class="muted">胜：正例相似度高于负例；平：差值绝对值 ≤ 1e−8。优先比较共同有效的 9 组主测试。自动线段图库只有 16 张，其余组 17 张，Top3 比例的候选范围不同，不作严格横向胜负判断。</p>
<details><summary>查看主测试及边界三元组</summary><div id="triplets" class="table"></div></details>
<div id="extras"></div><details><summary>笔画与位置敏感性检查</summary><p class="muted">与同一输入的未变换版本比较；分数只反映编码器对这些变换的响应，不代表人类判定的构图一致性。</p><div id="sensitivity" class="sensitivity"></div></details>
<details><summary>实验元数据</summary><pre id="metadata"></pre></details>
<footer>本地离线查看器 · 无外部资源 · <a href="results.json">原始结果</a> · <a href="input_comparison.jpg">全部输入拼图</a> · <a href="README.md">实验说明</a></footer>
</main><dialog id="viewer"><button id="close">关闭</button><p id="caption"></p><img id="large" alt="放大图片"></dialog>
<script id="experiment-data" type="application/json">__DATA__</script><script>
(() => {
const data=JSON.parse(document.getElementById('experiment-data').textContent), samples=new Map(data.samples.map(s=>[s.id,s]));
const variants=Object.entries(data.variants), records=Object.fromEntries(variants.map(([k,v])=>[k,new Map(v.records.map(r=>[r.id,r]))]));
const esc=x=>String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const num=x=>typeof x==='number'&&Number.isFinite(x)?x.toFixed(3):'—';
const pic=(path,label)=>`<button class="picture" data-zoom="${esc(path)}" data-caption="${esc(label)}"><img src="${esc(path)}" alt="${esc(label)}"></button>`;
const select=document.getElementById('query'), topSelect=document.getElementById('top');
function cards(ranks,key){return ranks.slice(0,Number(topSelect.value)).map((r,i)=>{const s=samples.get(r.id),rec=records[key].get(r.id);return `<div class="candidate">${pic(s.original_path,r.id+' 原图')}${pic(rec.image_path,r.id+' '+data.variants[key].label)}<p><button class="link" data-query="${esc(r.id)}">${r.tie_group_size>1?'并列 '+r.tie_group_size+' 项':i+1+'.'} ${esc(r.id)}</button> · ${num(r.similarity)}</p><small class="${r.shared_reference_layout?'good':'muted'}">${r.shared_reference_layout?'共同参考标签':'无共同参考标签'}</small></div>`;}).join('');}
function render(){
  const q=select.value,s=samples.get(q);
  document.getElementById('inputs').innerHTML=variants.map(([key,v])=>{const r=records[key].get(q);return `<article class="card"><h3>${esc(v.label)}</h3>${pic(r.image_path,q+' '+v.label)}<p>${key==='original'?'原图保留完整画幅':key==='boxes'?s.box_count+' 个检测框':key==='lines'?s.line_count+' 条自动长线段':s.box_count+' 框 ＋ '+s.line_count+' 线'}</p><small>${r.valid?'有效输入 · 384 维':'空结构图：无可用特征，不参与检索'}</small></article>`;}).join('');
  document.getElementById('evidence').textContent='旧参考标签：'+s.layouts.join('、')+'；目视依据：'+(typeof s.evidence==='string'?s.evidence:JSON.stringify(s.evidence));
  document.getElementById('rankings').innerHTML=variants.map(([key,v])=>`<article class="rank"><div class="rank-head"><h3>${esc(v.label)}</h3><small>有效查询 ${v.summary.valid_queries} / ${data.samples.length}</small></div><div class="candidates" style="grid-template-columns:repeat(${topSelect.value},1fr)">${cards(v.rankings[q]||[],key)||'<p class="muted">本查询缺少该种几何信息，无法排名。</p>'}</div></article>`).join('');
  document.getElementById('extras').innerHTML=(data.extra_queries||[]).length?'<h2>人工结构图补充查询</h2><p class="muted">仅作定性检查，不纳入上方固定参考统计；输入来源与简化规则见每组说明。</p>'+data.extra_queries.map(x=>`<article class="rank"><h3>${esc(x.label)}</h3><p class="muted">${esc(x.note)}</p><div class="grid"><div>${pic(x.image_path,x.label)}</div></div><div class="candidates" style="grid-template-columns:repeat(${topSelect.value},1fr)">${cards(x.rankings||[],x.variant)}</div></article>`).join(''):'';
  location.hash=new URLSearchParams({query:q,top:topSelect.value});
}
select.innerHTML=data.samples.map(s=>`<option value="${esc(s.id)}">${esc(s.id)}</option>`).join('');
const initial=new URLSearchParams(location.hash.slice(1));select.value=samples.has(initial.get('query'))?initial.get('query'):'aquarium';if(['3','5'].includes(initial.get('top')))topSelect.value=initial.get('top');
document.getElementById('summary').innerHTML='<table><thead><tr><th>输入</th><th>共同有效组胜场</th><th>全部主测试胜／平／负</th><th>有效／不可用</th><th>Top3 共同标签比例</th><th>编码耗时中位数</th></tr></thead><tbody>'+variants.map(([k,v])=>{const s=v.summary;return `<tr><td>${esc(v.label)}</td><td><strong>${s.common_main_wins} / ${s.common_main_count}</strong></td><td>${s.main_wins} / ${s.main_ties} / ${s.main_losses}</td><td>${s.main_valid} / ${s.main_unavailable}</td><td>${num(s.reference_overlap_at_3)}</td><td>${num(s.median_pipeline_ms)} ms</td></tr>`;}).join('')+'</tbody></table>';
document.getElementById('triplets').innerHTML='<table><thead><tr><th>组／查询</th><th>正例／负例</th>'+variants.map(([k,v])=>`<th>${esc(v.label)}</th>`).join('')+'</tr></thead><tbody>'+variants[0][1].triplets.map(t=>`<tr><td>${esc(t.split)} · ${esc(t.id)}<br><button class="link" data-query="${esc(t.query)}">${esc(t.query)}</button></td><td>＋ ${esc(t.positive)}<br>− ${esc(t.negative)}</td>${variants.map(([k,v])=>{const r=v.triplets.find(x=>x.id===t.id);return `<td class="${!r.valid?'':r.tie?'tie':r.win?'win':'loss'}">${!r.valid?'不可用':(r.tie?'平':r.win?'胜':'负')+' '+num(r.margin)}<small>${num(r.positive_similarity)} / ${num(r.negative_similarity)}</small></td>`;}).join('')}</tr>`).join('')+'</tbody></table>';
document.getElementById('sensitivity').innerHTML=(data.sensitivity||[]).map(x=>`<div class="card"><h3>${esc(x.id)} · ${esc(x.variant)}</h3>${pic(x.image_path,x.change)}<p>${esc(x.change)}</p><small>与原版本余弦 ${num(x.similarity)}</small></div>`).join('');
document.getElementById('metadata').textContent=JSON.stringify(data.metadata,null,2);select.addEventListener('change',render);topSelect.addEventListener('change',render);
document.addEventListener('click',event=>{const q=event.target.closest('[data-query]'),z=event.target.closest('[data-zoom]');if(q){select.value=q.dataset.query;render();document.getElementById('inputs').scrollIntoView({behavior:'smooth'});}if(z){document.getElementById('large').src=z.dataset.zoom;document.getElementById('caption').textContent=z.dataset.caption;document.getElementById('viewer').showModal();}});
document.getElementById('close').addEventListener('click',()=>document.getElementById('viewer').close());document.getElementById('viewer').addEventListener('click',e=>{if(e.target===e.currentTarget)e.currentTarget.close();});render();
})();
</script></body></html>'''


def paste_image(sheet, path, rectangle):
    image = ImageOps.contain(Image.open(ROOT / path).convert("RGB"), rectangle[2:])
    sheet.paste(image, (rectangle[0] + (rectangle[2] - image.width) // 2,
                        rectangle[1] + (rectangle[3] - image.height) // 2))


def main():
    data = json.loads((ROOT / "results.json").read_text())
    samples = {s["id"]: s for s in data["samples"]}
    variants = data["variants"]
    assert list(variants) == ["original", "boxes", "lines", "combined"]
    records = {key: {r["id"]: r for r in value["records"]} for key, value in variants.items()}
    embedded = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")
    (ROOT / "report.html").write_text(TEMPLATE.replace("__DATA__", embedded))
    font = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial.ttf", 18)
    small = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial.ttf", 15)
    sheet = Image.new("RGB", (1320, 65 + len(samples) * 214), "#f3f5f8")
    draw = ImageDraw.Draw(sheet)
    draw.text((12, 8), "Actual encoder inputs | frozen DINOv2 Small | white canvas, no labels", font=font, fill="#26374e")
    for column, key in enumerate(variants):
        draw.text((column * 330 + 10, 37), key, font=font, fill="#26374e")
    for row, sid in enumerate(samples):
        y = 65 + row * 214
        for column, key in enumerate(variants):
            r = records[key][sid]
            paste_image(sheet, r["image_path"], (column * 330 + 5, y, 320, 184))
            draw.text((column * 330 + 8, y + 187), sid + ("" if r["valid"] else " [unavailable]"), font=small, fill="#26374e")
    sheet.save(ROOT / "input_comparison.jpg", quality=94)
    sheet = Image.new("RGB", (1320, 390), "#f3f5f8")
    draw = ImageDraw.Draw(sheet)
    draw.text((12, 8), "Aquarium | same frozen DINOv2 Small encoder; four different inputs", font=font, fill="#26374e")
    for column, key in enumerate(variants):
        x = column * 330 + 5
        draw.text((x + 5, 39), key, font=font, fill="#26374e")
        paste_image(sheet, records[key]["aquarium"]["image_path"], (x, 67, 320, 290))
        label = {"original": "aspect preserved", "boxes": f"{samples['aquarium']['box_count']} automatic boxes",
                 "lines": f"{samples['aquarium']['line_count']} automatic LSD lines", "combined": "all boxes + all selected lines"}[key]
        draw.text((x + 5, 367), label, font=small, fill="#26374e")
    sheet.save(ROOT / "aquarium_inputs.jpg", quality=94)
    for query in ["aquarium", "interior", "stage_left"]:
        sheet = Image.new("RGB", (1320, 1810), "#f3f5f8")
        draw = ImageDraw.Draw(sheet)
        draw.text((12, 8), f"Query: {query} | columns: query, rank 1, rank 2, rank 3 | cosine is not probability", font=font, fill="#26374e")
        for row, (key, variant) in enumerate(variants.items()):
            y = 45 + row * 438
            draw.text((10, y), f"{key} | top: source frame; bottom: encoder input", font=font, fill="#26374e")
            selected = [(query, None)] + [(r["id"], r["similarity"]) for r in variant["rankings"].get(query, [])[:3]]
            for column, (sid, similarity) in enumerate(selected):
                x = column * 330 + 5
                paste_image(sheet, samples[sid]["original_path"], (x, y + 28, 320, 178))
                paste_image(sheet, records[key][sid]["image_path"], (x, y + 214, 320, 184))
                label = sid if similarity is None else f"{sid}: {similarity:.3f}"
                if not records[key][sid]["valid"]:
                    label += " [unavailable]"
                draw.text((x + 3, y + 404), label, font=small, fill="#26374e")
        sheet.save(ROOT / f"retrieval_{query}.jpg", quality=94)
    print(json.dumps({"samples": len(samples), "variants": len(variants), "outputs": 6}))


if __name__ == "__main__":
    main()
