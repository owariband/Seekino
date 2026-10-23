"""Render the local composition experiment as a dependency-free offline HTML report."""

import json
from pathlib import Path


TEMPLATE = r'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>构图模型实验 · 解析与检索对照</title>
<style>
:root{font-family:ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:#24344a;background:#f4f6f9;line-height:1.55;font-size:14px}*{box-sizing:border-box}body{margin:0}main{max-width:1440px;margin:auto;padding:28px}h1{font-size:29px;letter-spacing:-.6px;margin:0 0 8px}h2{font-size:20px;margin:28px 0 12px}h3{font-size:15px;margin:0 0 8px}p{margin:5px 0}a{color:#315bab}button,select{font:inherit}button{cursor:pointer}small,.muted{color:#6a7789}.subtitle{color:#65748a}.warning{margin:18px 0;padding:13px 16px;background:#fff5df;border:1px solid #efd6a1;border-radius:10px;color:#765119}.toolbar{position:sticky;top:0;z-index:2;display:flex;gap:18px;align-items:center;padding:12px 16px;background:#ffffffed;backdrop-filter:blur(8px);border:1px solid #dce2ea;border-radius:10px;box-shadow:0 3px 12px #22304408}.toolbar label{display:flex;gap:8px;align-items:center;min-width:0}.toolbar select{max-width:330px;padding:7px;border:1px solid #ccd5e0;border-radius:6px;background:white}.toolbar .count{margin-left:auto;color:#64748b}.grid{display:grid;grid-template-columns:1.15fr 1fr 1fr;gap:14px}.card{background:white;border:1px solid #dce2ea;border-radius:10px;padding:16px;min-width:0}.original{grid-row:span 2}.picture{display:block;border:0;padding:0;background:#182333;width:100%;border-radius:7px;overflow:hidden}.picture img{width:100%;height:190px;object-fit:contain;display:block}.original .picture img{height:300px}.tags{display:flex;flex-wrap:wrap;gap:5px;margin:10px 0}.tag{display:inline-block;background:#edf2fb;color:#365789;border-radius:5px;padding:2px 7px;font-size:12px}.score{font-size:27px;font-weight:650;color:#223d65;line-height:1.2;margin:6px 0}.score span{font-size:13px;font-weight:400;color:#68788e}.bar{display:grid;grid-template-columns:120px 1fr 40px;align-items:center;gap:7px;font-size:11px;margin:5px 0}.bar .track{height:9px;border-radius:2px;background:#eef1f6;overflow:hidden}.bar i{display:block;height:100%;background:#6183b8}.bar .num{text-align:right;font-variant-numeric:tabular-nums}.distribution{display:flex;align-items:end;gap:5px;height:55px;margin-top:10px}.bucket{flex:1;min-width:0;position:relative;text-align:center;font-size:10px}.bucket i{display:block;background:#91aad0;min-height:1px;border-radius:2px 2px 0 0}.bucket span{display:block;color:#6a7789}.native-details{font-size:11px;margin-top:8px}pre{overflow:auto;max-height:260px;white-space:pre-wrap;word-break:break-word;background:#f4f6f9;padding:9px;border-radius:5px;font-size:11px}summary{cursor:pointer}.model-info{margin-top:14px;display:flex;align-items:baseline;gap:12px;flex-wrap:wrap}.model-info strong{font-size:16px}.rank-row{background:white;border:1px solid #dce2ea;border-radius:10px;padding:14px;margin-bottom:12px}.rank-row.selected{border-color:#5c81bc;box-shadow:0 0 0 2px #e7eef9}.rank-header{display:flex;gap:15px;align-items:baseline;margin-bottom:10px;flex-wrap:wrap}.rank-header strong{font-size:15px}.rank-header .stat{margin-left:auto}.rank-cards{display:grid;grid-template-columns:repeat(5,1fr);gap:10px}.candidate{min-width:0}.candidate button{display:block;width:100%;border:0;background:#f1f3f7;padding:0;border-radius:5px;overflow:hidden}.candidate img{width:100%;height:115px;object-fit:contain;display:block;background:#182333}.candidate p{font-size:12px;overflow-wrap:anywhere}.candidate .relevant{color:#23735b}.candidate .unknown{color:#8a6a3c}.table-wrap{overflow:auto;border:1px solid #dce2ea;background:white;border-radius:9px}table{border-collapse:collapse;width:100%;font-size:12px}th,td{padding:9px;border-bottom:1px solid #e6eaf0;text-align:left;vertical-align:top;white-space:nowrap}th{background:#edf2f8;font-weight:600}td.win{background:#edf7f1;color:#25724c}td.loss{background:#fff0ed;color:#a64b39}td.tie{background:#f5f4e9;color:#7d7445}td.selected,th.selected{box-shadow:inset 3px 0 #5a7fb8}td small{display:block}.table-link{border:0;background:none;color:#315bab;padding:0;font-size:12px;text-align:left}.footer{font-size:12px;color:#68788e;margin:24px 0 6px}dialog{padding:12px;border:0;border-radius:10px;max-width:96vw;max-height:94vh;background:#162334;color:white}dialog::backdrop{background:#091422d9}dialog img{display:block;max-width:90vw;max-height:82vh;object-fit:contain}dialog button{float:right;border:0;border-radius:4px;margin-bottom:8px;padding:5px 14px}dialog p{clear:both}@media(max-width:1000px){.grid{grid-template-columns:1fr 1fr}.original{grid-row:span 1;grid-column:span 2}.rank-cards{gap:6px}.candidate img{height:90px}.toolbar{flex-wrap:wrap}}@media(max-width:640px){main{padding:16px}.grid{grid-template-columns:1fr}.original{grid-column:span 1}.rank-cards{grid-template-columns:repeat(2,1fr)}.toolbar label{width:100%}.toolbar select{flex:1;max-width:none}.toolbar .count{margin-left:0}.original .picture img{height:220px}.bar{grid-template-columns:105px 1fr 35px}}
</style></head><body><main>
<h1>构图模型实验 · 解析与检索对照</h1>
<p class="subtitle">同一批动画画面，比较原生解析结果与跨内容构图检索。点击图片可放大；点击候选可将它设为查询图。</p>
<div class="warning"><strong>小样本探索：</strong>参考标签来自助手目视判断；模型得分不是经过校准的置信概率。PCP 类别得分、SAMP 构图质量、MUSIQ 美学分、embedding 余弦分别表达不同含义。设备与预处理不同，耗时不能直接横比。<br><strong>本批观察：</strong><span id="observations"></span>这些胜场不是分类准确率，也不足以宣布通用优胜者。</div>
<div class="toolbar"><label>查询图片 <select id="query"></select></label><label>重点模型 <select id="model"></select></label><span class="count" id="count"></span></div>
<h2>这张图被解析成什么</h2><div id="native" class="grid"></div>
<div id="model-info" class="card model-info"></div>
<h2>各模型的 Top 5</h2><p class="muted">绿色提示仅表示与目视参考有共同标签，不是人工判定每个候选都匹配。余弦数值只在同一模型内解释。并列项按 manifest 顺序展示，不能把展示顺序解释为模型偏好；零向量不参与排名。</p><div id="rankings"></div>
<h2>主测试：同构图异内容，能否排在语义干扰之前</h2><p class="muted">每格为 正例相似度 − 负例相似度；胜／负／平只描述有效对比。LSD 的 stage_wide 为零向量，涉及该样本的三元组标为不可用；其余平局保留。完整三元组和原始读数已内嵌本页。</p><div class="table-wrap" id="triplets"></div>
<details class="footer"><summary>边界样本与实验元数据</summary><div id="boundary" class="table-wrap"></div><pre id="metadata"></pre></details>
<p class="footer">离线查看器 · 无外部脚本、字体或网络请求 · 原始数据：<a href="comparison.json">comparison.json</a> · 模型原始结果见各模型链接。参考类别为本地粗类别，不是 PICD／CADB 标准基准成绩。</p>
</main><dialog id="viewer"><button id="close-viewer">关闭</button><p id="viewer-label"></p><img id="viewer-image" alt="放大图片"></dialog>
<script id="experiment-data" type="application/json">__DATA__</script>
<script>
const data=JSON.parse(document.getElementById('experiment-data').textContent);
const samples=new Map(data.samples.map(s=>[s.id,s]));
const querySelect=document.getElementById('query'), modelSelect=document.getElementById('model');
const escapeHtml=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const display=value=>typeof value==='string'?value:JSON.stringify(value??null);
const number=(value,digits=3)=>Number.isFinite(Number(value))&&value!==null?Number(value).toFixed(digits):'—';
const names={diagonal:'对角／倾斜',horizontal:'水平',horizontal_layers:'水平分层',vertical:'垂直',centered:'居中',symmetry:'对称',symmetric:'对称',perspective:'透视',subject_left:'主体偏左',subject_left_negative_space:'左主体＋右留白',centered_closeup:'居中近景'};
const tags=items=>(Array.isArray(items)?items:[]).map(x=>`<span class="tag">${escapeHtml(names[x]||x)}</span>`).join('');
const picture=(path,label)=>path?`<button class="picture" data-zoom="${escapeHtml(path)}" data-caption="${escapeHtml(label)}" aria-label="放大${escapeHtml(label)}"><img src="${escapeHtml(path)}" alt="${escapeHtml(label)}"></button>`:'<p class="muted">未提供叠加图</p>';
function entries(value){
  if(Array.isArray(value))return value.map((item,i)=>typeof item==='number'?[String(i+1),item]:Array.isArray(item)?item:[item.label||item.class||item.name||String(i+1),item.probability??item.score??item.value??0]);
  return Object.entries(value||{});
}
function bars(value){
  const rows=entries(value), max=Math.max(...rows.map(x=>Number(x[1])),0.000001);
  return rows.map(([label,value])=>`<div class="bar"><span title="${escapeHtml(label)}">${escapeHtml(label)}</span><span class="track"><i style="width:${Math.max(0,Number(value))/max*100}%"></i></span><span class="num">${number(value,3)}</span></div>`).join('')||'<p class="muted">无类别读数</p>';
}
function distribution(value){
  const rows=entries(value),max=Math.max(...rows.map(x=>Number(x[1])),0.000001);
  return `<div class="distribution">${rows.map(([label,value])=>`<div class="bucket" title="${escapeHtml(label)}: ${number(value,5)}"><i style="height:${Math.max(0,Number(value))/max*38}px"></i><span>${escapeHtml(label)}</span></div>`).join('')}</div>`;
}
function renderNative(){
  const sample=samples.get(querySelect.value), native=data.native_outputs?.[sample.id]||{},pcp=native.pcp||{},samp=native.samp||{},musiq=native.musiq||{},grounding=native.grounding||{},lines=native.lines||{};
  document.getElementById('native').innerHTML=`
  <article class="card original"><h3>${escapeHtml(sample.id)} · 原图</h3>${picture(sample.image_path,sample.id)}<div class="tags">${tags(sample.layouts)}</div><p class="muted">目视依据：${escapeHtml(display(sample.evidence))}</p><small>参考允许多标签，包含主次与歧义。</small></article>
  <article class="card"><h3>PCP · 原生 9 类构图得分</h3><p class="muted">最高类别：${escapeHtml(display(pcp.top_class))}</p>${bars(pcp.class_probabilities)}<small>条形按本图最大值缩放；不是检索向量的直接解释。</small></article>
  <article class="card"><h3>质量与美学 · 原生输出</h3><p>SAMP-Net 构图质量</p><div class="score">${number(samp.mean_composition_quality,2)} <span>/ 5</span></div>${distribution(samp.score_distribution_1_to_5)}<p style="margin-top:13px">MUSIQ AVA 美学</p><div class="score">${number(musiq.aesthetic_score_1_to_10,2)} <span>/ 10</span></div>${distribution(musiq.score_distribution)}<details class="native-details"><summary>SAMP 模式权重（非类别概率）</summary><pre>${escapeHtml(JSON.stringify(samp.pattern_importance_weights??null,null,2))}</pre></details></article>
  <article class="card"><h3>Grounding DINO · 目标框</h3>${picture(grounding.overlay_path,sample.id+' Grounding DINO')}<small>${Array.isArray(grounding.detections)?grounding.detections.length:'—'} 个检测；文字条件的物件定位。</small></article>
  <article class="card"><h3>LSD · 原生几何线段</h3>${picture(lines.overlay_path,sample.id+' LSD')}<small>${lines.selected_count??'—'} 条入选；固定阈值、长度排序，不判断语义主线。</small></article>`;
}
function renderRankings(){
  const query=querySelect.value;
  document.getElementById('rankings').innerHTML=data.models.map(model=>{
    const valid=model.main_valid_count??model.main_count,missing=model.main_unavailable_count??0;
    const cards=(model.rankings?.[query]||[]).slice(0,5).map((rank,i)=>{
      const sample=samples.get(rank.id),order=rank.tie_group_size>1?`并列 ${rank.tie_group_size} 项`:`${i+1}.`;
      return `<div class="candidate"><button data-query="${escapeHtml(rank.id)}" aria-label="查询${escapeHtml(rank.id)}"><img src="${escapeHtml(sample?.image_path||'')}" alt="${escapeHtml(rank.id)}"></button><p><strong>${order} ${escapeHtml(rank.id)}</strong> · ${number(rank.similarity)}</p><small class="${rank.shared_reference_layout?'relevant':'unknown'}">${rank.shared_reference_layout?'共同参考标签':'无共同参考标签'}</small></div>`;
    }).join('');
    return `<article class="rank-row ${model.id===modelSelect.value?'selected':''}"><div class="rank-header"><strong>${escapeHtml(model.label)}</strong><small>${escapeHtml(model.dimension)} 维 · ${escapeHtml(model.device)} · ${number(model.median_ms,1)} ms</small><span class="stat">主测试 ${model.main_wins} 胜 / ${valid} 有效${model.main_ties?`，${model.main_ties} 平`:''}${missing?`，${missing} 不可用`:''}</span></div><div class="rank-cards">${cards||'<p class="muted">本查询没有可用排名：无有效特征（例如零向量），不视为检索失败或平局。</p>'}</div></article>`;
  }).join('');
  const selected=data.models.find(model=>model.id===modelSelect.value);
  document.getElementById('model-info').innerHTML=`<strong>${escapeHtml(selected.label)}</strong><span>${escapeHtml(selected.description)}</span><a href="${escapeHtml(selected.source_file)}">原始结果</a><details style="width:100%"><summary>当前图片的原生输出摘要</summary><pre>${escapeHtml(JSON.stringify(selected.records?.[query]??null,null,2))}</pre></details>`;
}
function tripletTable(split){
  const rows=new Map();
  for(const model of data.models)for(const row of model.triplets||[])if((split==='main'&&row.split==='main')||(split==='boundary'&&row.split!=='main'))rows.set(row.id,row);
  return `<table><thead><tr><th>查询</th><th>正例／负例</th>${data.models.map(model=>`<th class="${model.id===modelSelect.value?'selected':''}">${escapeHtml(model.label)}</th>`).join('')}</tr></thead><tbody>${[...rows.values()].map(row=>`<tr><td><button class="table-link" data-query="${escapeHtml(row.query)}" title="${escapeHtml(row.rationale)}">${escapeHtml(row.id)}<br>${escapeHtml(row.query)}</button></td><td><span style="color:#25724c">＋ ${escapeHtml(row.positive)}</span><br><span style="color:#a64b39">− ${escapeHtml(row.negative)}</span></td>${data.models.map(model=>{
    const item=(model.triplets||[]).find(t=>t.id===row.id),unavailable=item?.valid===false,tie=item?.tie===true;
    const kind=!item||unavailable?'':tie?'tie':item.win?'win':'loss';
    const result=!item?'—':unavailable?`<span title="${escapeHtml(item.unavailable_reason)}">不可用</span>`:`${tie?'平':item.win?'胜':'负'} ${number(item.margin)}<small>${number(item.positive_similarity)} / ${number(item.negative_similarity)}</small>`;
    return `<td class="${kind} ${model.id===modelSelect.value?'selected':''}">${result}</td>`;
  }).join('')}</tr>`).join('')}</tbody></table>`;
}
function render(){
  renderNative();renderRankings();
  document.getElementById('triplets').innerHTML=tripletTable('main');
  document.getElementById('boundary').innerHTML=tripletTable('boundary');
  location.hash=new URLSearchParams({query:querySelect.value,model:modelSelect.value});
}
querySelect.innerHTML=data.samples.map(sample=>`<option value="${escapeHtml(sample.id)}">${escapeHtml(sample.id)}</option>`).join('');
modelSelect.innerHTML=data.models.map(model=>`<option value="${escapeHtml(model.id)}">${escapeHtml(model.label)}</option>`).join('');
const initial=new URLSearchParams(location.hash.slice(1));
if(samples.has(initial.get('query')))querySelect.value=initial.get('query');
modelSelect.value=data.models.reduce((best,model)=>model.main_wins>best.main_wins?model:best).id;
if(data.models.some(model=>model.id===initial.get('model')))modelSelect.value=initial.get('model');
document.getElementById('count').textContent=`${data.samples.length} 张图 · ${data.models.length} 组特征`;
document.getElementById('metadata').textContent=JSON.stringify(data.metadata||{},null,2);
document.getElementById('observations').textContent=['pcp_fc7','clip_fullframe','pcp_classes','lsd'].map(id=>data.models.find(model=>model.id===id)).filter(Boolean).map(model=>`${model.label}：${model.main_wins} 胜 / ${model.main_valid_count??model.main_count} 有效${model.main_ties?`、${model.main_ties} 平`:''}${model.main_unavailable_count?`、${model.main_unavailable_count} 不可用`:''}`).join('；')+'。';
querySelect.addEventListener('change',render);modelSelect.addEventListener('change',render);
document.addEventListener('click',event=>{
  const queryButton=event.target.closest('[data-query]'),zoom=event.target.closest('[data-zoom]');
  if(queryButton){querySelect.value=queryButton.dataset.query;render();document.getElementById('native').scrollIntoView({behavior:'smooth',block:'start'});}
  if(zoom){document.getElementById('viewer-image').src=zoom.dataset.zoom;document.getElementById('viewer-label').textContent=zoom.dataset.caption;document.getElementById('viewer').showModal();}
});
document.getElementById('close-viewer').addEventListener('click',()=>document.getElementById('viewer').close());
document.getElementById('viewer').addEventListener('click',event=>{if(event.target===event.currentTarget)event.currentTarget.close();});
render();
</script></body></html>'''


def main():
    root = Path(__file__).resolve().parent
    data = json.loads((root / "comparison.json").read_text())
    assert data["samples"] and data["models"], "Cannot render an empty experiment"
    embedded = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")
    report = TEMPLATE.replace("__DATA__", embedded)
    (root / "report.html").write_text(report)
    print(f"Rendered report.html: {len(data['samples'])} samples, {len(data['models'])} models, {len(report.encode())} bytes")


if __name__ == "__main__":
    main()
