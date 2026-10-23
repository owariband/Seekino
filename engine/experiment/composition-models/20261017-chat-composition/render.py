"""Render the recorded Chat response and prior image baselines without retrieval."""
import html
import json
from pathlib import Path
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parent


def esc(value):
    return html.escape(str(value if value is not None else ""), quote=True)


def link_target(value):
    """Allow local report paths and HTTP links, excluding executable URL schemes."""
    value = str(value or "")
    parsed = urlsplit(value)
    if parsed.scheme.lower() not in ("", "http", "https") or value.startswith("//"):
        return "#"
    return esc(value)


def clock(milliseconds):
    if not isinstance(milliseconds, (int, float)):
        return "未记录"
    seconds, ms = divmod(max(0, round(milliseconds)), 1000)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{ms:03d}"


def picture(path, caption, extra=""):
    if not path:
        return '<div class="empty">未保存图片</div>'
    target = link_target(path)
    return (f'<a class="picture {extra}" href="{target}" target="_blank" rel="noopener" '
            f'data-image="{target}" data-caption="{esc(caption)}">'
            f'<img src="{target}" alt="{esc(caption)}" loading="lazy"></a>')


def episode_label(value):
    return f"第{value}集" if value is not None else "未记录集数"


def preview(data):
    cards = [f'<article class="card"><h3>参考图</h3>{picture(data.get("reference_path"), "room_high · 参考图")}<p class="muted">提交给 Chat 的原始图片</p></article>']
    for rank, candidate in enumerate(data.get("chat", {}).get("candidates", [])[:2], 1):
        frames = candidate.get("frames", [])
        frame = frames[len(frames) // 2] if frames else {}
        caption = f'{episode_label(candidate.get("episode"))} · {clock(frame.get("timestamp_ms"))}'
        cards.append(f'<article class="card"><h3>Chat 候选 {rank}</h3>{picture(frame.get("path"), caption)}<p class="muted">{esc(caption)}</p></article>')
    return '<div class="preview">' + ''.join(cards) + '</div>'


def chat_candidates(candidates):
    cards = []
    origin_labels = {"chat_inspected": "Chat 取帧工具返回", "after_run_sample": "事后抽帧"}
    for candidate in candidates:
        name = f'{episode_label(candidate.get("episode"))} · {clock(candidate.get("start_ms"))} — {clock(candidate.get("end_ms"))}'
        reason = candidate.get("reason")
        if isinstance(reason, dict):
            reason = reason.get("summary")
        frames = []
        for frame in candidate.get("frames", []):
            origin = frame.get("origin", "未标注来源")
            caption = f'{name} · {clock(frame.get("timestamp_ms"))}'
            frames.append('<figure>' + picture(frame.get("path"), caption) +
                          f'<figcaption><span class="badge {"inspected" if origin == "chat_inspected" else "sampled"}">'
                          f'{esc(origin_labels.get(origin, origin))}</span> {clock(frame.get("timestamp_ms"))}</figcaption></figure>')
        frames_html = ''.join(frames) or '<div class="empty">本候选未保存可展示帧。</div>'
        cards.append(f'<article class="card chat-candidate"><h3>{esc(name)}</h3>'
                     f'<p class="muted">候选 ID：{esc(candidate.get("id"))}</p>'
                     f'<p class="reason">{esc(reason or "返回记录未提供单独理由。")}</p>'
                     f'<div class="frames">{frames_html}</div></article>')
    return ''.join(cards) or '<div class="empty">本次记录没有返回可展示的候选；请以完整 Chat 原文及运行状态为准。</div>'


def baseline_cards(baselines):
    sections = []
    for baseline in baselines:
        cards = []
        for rank, candidate in enumerate(baseline.get("candidates", [])[:3], 1):
            label = candidate.get("label") or candidate.get("id")
            score = candidate.get("score")
            score_text = f"{score:.4f}" if isinstance(score, (int, float)) else "未记录"
            cards.append(f'<article class="card"><h3>{rank}. {esc(label)}</h3>'
                         + picture(candidate.get("image_path"), f'{label} · 原图')
                         + picture(candidate.get("box_image_path"), f'{label} · 编码框图')
                         + f'<p class="score">加权分 {score_text}</p><p class="muted">{esc(candidate.get("id"))}</p></article>')
        content = ''.join(cards) or '<div class="empty">没有记录基线候选。</div>'
        sections.append(f'<section class="baseline"><h3>{esc(baseline.get("label"))}</h3>'
                        f'<p class="muted">{esc(baseline.get("weight_note"))}</p><div class="baseline-grid">{content}</div></section>')
    return ''.join(sections) or '<div class="empty">没有记录旧实验基线。</div>'


CSS = r"""
:root{font:15px/1.65 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:#27384e;background:#f3f5f8}*{box-sizing:border-box}body{margin:0}main{max-width:1400px;margin:auto;padding:30px}h1{font-size:30px;line-height:1.3;margin:0 0 10px}h2{font-size:23px;margin:32px 0 13px}h3{font-size:17px;line-height:1.5;margin:0 0 9px}p{margin:7px 0}.muted{color:#63758b;font-size:13px}.notice{padding:16px 19px;border:1px solid #e5c786;background:#fff4d9;border-radius:9px;margin:20px 0}.reference{display:grid;grid-template-columns:1.4fr 1fr;gap:18px;align-items:start}.card,.answer{background:white;border:1px solid #dce3ed;border-radius:9px;padding:17px;min-width:0}.picture{display:block;border:1px solid #e4e8ef;border-radius:6px;background:white;overflow:hidden;cursor:zoom-in;margin:7px 0}.picture img{display:block;width:100%;height:225px;object-fit:contain}.reference .picture img{height:330px}.question,.reason{white-space:pre-wrap;overflow-wrap:anywhere}.question{font-size:17px}.run-info{display:flex;flex-wrap:wrap;gap:8px 22px;font-size:13px}.answer pre{font:inherit;white-space:pre-wrap;overflow-wrap:anywhere;margin:15px 0 0;padding-top:14px;border-top:1px solid #e5eaf2}.chat-candidate{margin:15px 0}.frames{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}.frames figure{margin:0;min-width:0}figcaption{font-size:12px;color:#63758b}.badge{display:inline-block;border-radius:4px;padding:1px 7px;margin-right:5px}.inspected{background:#e3f2ed;color:#26785f}.sampled{background:#fff1d4;color:#87652d}.baseline{margin-top:19px}.baseline-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px;margin-top:10px}.baseline-grid .picture img{height:180px}.score{font-weight:600;font-variant-numeric:tabular-nums}.empty{background:#f8f9fb;border:1px dashed #cbd5e2;border-radius:7px;padding:19px;color:#68788e}.observations li{margin:8px 0}.source-list{font-size:13px;overflow-wrap:anywhere}a{color:#315b94}footer{font-size:12px;margin:25px 0;color:#65768b}dialog{border:0;border-radius:10px;padding:14px;max-width:96vw;max-height:96vh}dialog::backdrop{background:#122034df}dialog img{display:block;max-width:90vw;max-height:80vh;object-fit:contain}dialog button{float:right;border:1px solid #cbd5e2;border-radius:5px;background:white;font:inherit;padding:4px 14px;cursor:pointer}dialog p{clear:both;padding-top:8px}@media(max-width:850px){main{padding:19px}.reference{grid-template-columns:1fr}.reference .picture img{height:280px}.frames{grid-template-columns:repeat(2,minmax(0,1fr))}.baseline-grid .picture img{height:140px}}@media(max-width:540px){.frames,.baseline-grid{grid-template-columns:1fr}.picture img,.baseline-grid .picture img{height:220px}h1{font-size:25px}}
"""
CSS += ".preview{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:15px;margin:20px 0}.preview .picture img{height:255px}.answer summary{cursor:pointer;font-weight:600}.answer .run-info{margin-top:15px}@media(max-width:720px){.preview{grid-template-columns:1fr}.preview .picture img{height:260px}}"


def main():
    data = json.loads((ROOT / "comparison.json").read_text())
    chat = data.get("chat", {})
    answer = chat.get("answer") or "（本次记录没有 Chat 回答原文；请检查运行状态与原始记录。）"
    observations = ''.join(f'<li>{esc(item)}</li>' for item in data.get("observations", []))
    sources = ''.join(f'<li><a href="{link_target(source.get("path"))}" target="_blank" rel="noopener">{esc(source.get("label"))}</a></li>' for source in data.get("sources", []))
    page = f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(data.get("title"))}</title><style>{CSS}</style></head><body><main>
<h1>{esc(data.get("title") or "Chat 构图检索与 PCP＋框图对照")}</h1>
<p class="muted">真实 Chat 运行记录与旧实验候选的定性比较。点击图片可放大。</p>
<div class="notice"><strong>候选范围不同：</strong>Chat 搜索 MyGO 13 集全库；PCP＋框图基线仅在旧实验的 17 张选图范围内检索，具体筛选见各组说明。两者不能作为同库准确率对照，也不能仅凭返回结果多少判断优劣。</div>
{preview(data)}
<h2>参考图片与原始提问</h2><div class="reference"><div class="card">{picture(data.get("reference_path"), "room_high · 提交给 Chat 的参考图片")}</div><div class="card"><h3>原始提问</h3><p class="question">{esc(data.get("question"))}</p></div></div>
<h2>Chat 完整原文</h2><details class="answer"><summary>展开完整回答与运行信息（原文保留）</summary><div class="run-info"><span>状态：<strong>{esc(chat.get("status", "未记录"))}</strong></span><span>模型：{esc(chat.get("model", "未记录"))}</span><span>耗时：{esc(chat.get("elapsed_seconds", "未记录"))} 秒</span></div>
<p class="muted">会话：{esc(chat.get("conversation_title"))} · {esc(chat.get("conversation_id"))} · <a href="{link_target(chat.get("web_url"))}" target="_blank" rel="noopener">打开项目 Chat</a></p><pre>{esc(answer)}</pre></details>
<h2>Chat 实际返回的候选</h2><p class="muted"><span class="badge inspected">Chat 取帧工具返回</span>表示帧出现在本次运行的取帧工具返回中，不推断模型具体关注了哪些画面；<span class="badge sampled">事后抽帧</span>仅供事后核对，不属于本次工具返回。未返回视频卡时仍以原文和已记录候选为准。</p>{chat_candidates(chat.get("candidates", []))}
<h2>旧 17 张选图实验 · A/B Top 3</h2><p class="muted">两组沿用已保存的 PCP 分布与框图向量。以下仅供观察返回画面差异，加权分不是概率，也不能与 Chat 相关性分数直接比较。</p>{baseline_cards(data.get("baselines", []))}
<h2>本次观察</h2><ul class="observations">{observations}</ul>
<h2>原始记录</h2><ul class="source-list">{sources}<li><a href="comparison.json" target="_blank" rel="noopener">本页结构化数据</a></li></ul>
<footer>本地离线结果页 · 页面不请求模型或网络数据 · Chat 原文保留原始内容</footer>
</main><dialog id="image-viewer"><button id="close-image">关闭</button><p id="image-caption"></p><img id="full-image" alt="放大图片"></dialog>
<script>(()=>{{const dialog=document.getElementById('image-viewer');document.addEventListener('click',event=>{{const link=event.target.closest('[data-image]');if(!link)return;event.preventDefault();document.getElementById('full-image').src=link.dataset.image;document.getElementById('image-caption').textContent=link.dataset.caption;dialog.showModal();}});document.getElementById('close-image').addEventListener('click',()=>dialog.close());dialog.addEventListener('click',event=>{{if(event.target===dialog)dialog.close();}});}})();</script>
</body></html>'''
    (ROOT / "report.html").write_text(page)
    print(json.dumps({"output": str(ROOT / "report.html"), "chat_candidates": len(chat.get("candidates", [])), "baseline_groups": len(data.get("baselines", []))}))


if __name__ == "__main__":
    main()
