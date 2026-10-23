"""Render saved retrieval evidence without any further model or network calls."""
import base64
import html
import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

OUT = Path(__file__).resolve().parent
text_run = json.loads((OUT / 'results.json').read_text())
image_run = json.loads((OUT / 'image-results.json').read_text())
queries = {q['key']: q for q in text_run['queries']}

# Manual visual inspection of the returned previews, not model labels or ground truth.
notes = {
    'content': [
        '同一来源镜头。视角、家具、人物位置一致；字幕和动作时刻不同。',
        '人物与吉他相同，但变成正面近景，构图不同。',
        '同一房间，接近水平视角，构图不同。',
        '同一人物和沙发，低机位，构图不同。',
        '同一人物与沙发，中近景，构图不同。',
        '同一房间，接近水平视角，构图不同。',
        '同一房间的低机位全景，构图不同。',
    ],
    'layout': [
        '大空间、小人物，但主要是上下分层，未满足参考图的斜俯视地面构图。',
        '从室外斜向下看室内，有俯视与斜线，但窗框遮挡和人物分布不同。',
        '同一房间的低机位，视角相反。',
        '同一房间的低机位，视角相反。',
        '较接近的其他画面：斜俯视、人物靠右、中央茶几、家具边缘形成对角线。人物比例及俯视程度不同。',
        '类别接近的其他画面：俯视客厅、环境占主导；人物位于上方中央，布局与参考图不同。',
        '参考图的同一来源镜头，寻找“其他图”时应排除。',
    ],
    'broad': [
        'ED 字幕版式，不符合目标场景。',
        '大空间、小人物，但主要是上下分层。',
        '另一集重复使用的 ED 画面，需要跨集去重。',
        '街道纵深与小人物，但不是俯视房间。',
        '俯视手部绘画，主体与环境比例不同。',
        '水面倒影，没有要求的人物。',
        '俯视夜间建筑与街道，预览中没有清晰可见的人物。',
    ],
    'image': [
        '同一来源镜头。不能把 Event 相似度解释成逐像素原图定位。',
        '同一房间和人物，但机位很低，构图不同。',
        '另一个俯视客厅：整体构图类别接近，人物位置不同。',
        '同一房间和人物，低机位，构图不同。',
        '同一人物与沙发，低机位，构图不同。',
        '人物与吉他相同，正面近景，构图不同。',
        '人物躺在沙发的近景，构图不同。',
        '同一房间和人物，接近水平视角，构图不同。',
    ],
}


def stamp(ms):
    seconds, millis = divmod(ms, 1000)
    minutes, seconds = divmod(seconds, 60)
    return f'{minutes:02d}:{seconds:02d}.{millis:03d}'


def picture(path):
    data = base64.b64encode((OUT / path).read_bytes()).decode()
    return f'data:image/jpeg;base64,{data}'


def card(p, note, route):
    title = f"{route} · 原始 Event Rank {p['rank']} · 第 {p['episode']} 集"
    times = f"Event {stamp(p['start_ms'])}–{stamp(p['end_ms'])} · 预览 {stamp(p['frame_timestamp_ms'])}"
    score = (f"图像向量 cosine = {p['cosine_similarity']:.4f}" if 'cosine_similarity' in p
             else f"visual 通道原始分数 = {p['channel_scores']['vision']:.4f}")
    return f'''<article><a href="{html.escape(p['image'])}" target="_blank"><img src="{picture(p['image'])}" alt="{html.escape(title)}"></a>
      <div class="body"><b>{title}</b><p class="small">{times}</p><p>{html.escape(note)}</p>
      <details><summary>原始描述与分数</summary><p>{html.escape(p['description'])}</p><p>{score}（不是构图概率）</p>
      <code>{p['event_id']}</code></details></div></article>'''


parts = ['''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>爱音房间 · Event 构图检索实测</title><style>
*{box-sizing:border-box}body{margin:0;background:#f3f5f8;color:#203048;font:16px/1.6 system-ui,sans-serif}main{max-width:1440px;margin:auto;padding:32px}h1{font-size:34px;margin:0}h2{margin-top:38px}.lead{font-size:19px}.notice{padding:18px;background:#fff4d7;border:1px solid #e1cc8e;border-radius:10px}.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:18px}article{background:white;border:1px solid #d8e0e9;border-radius:10px;overflow:hidden}img{display:block;width:100%;height:auto}article .body{padding:14px 18px}p{margin:10px 0}.small,summary{color:#596c83;font-size:14px}details{margin:12px 0}summary{cursor:pointer}code{overflow-wrap:anywhere;font-size:12px}a{color:#235da8}blockquote{margin:12px 0;padding:14px 18px;background:white;border-left:4px solid #8ba6c8}.ref{max-width:800px}nav a{margin-right:18px}li{margin:10px 0}@media(max-width:800px){main{padding:18px}.grid{grid-template-columns:1fr}h1{font-size:27px}}
</style><main><h1>爱音房间：Event 检索现在能搜出什么？</h1>
<p class="lead">同一来源镜头已命中；其他构图候选能够召回，但原始排名仍受场景与人物影响。</p>
<nav><a href="#key">关键对照</a><a href="#image">原图向量</a><a href="#content">内容文字</a><a href="#layout">构图文字</a><a href="#broad">更宽泛文字</a><a href="#next">接下来怎么做</a></nav>
<p class="notice">实测范围：MyGO 13 集、312 个当前 Chunk 来源。正式工具 <code>search_video_events</code> 当前只接收文字，三组文字检索实际通过其工具入口执行，并用 <code>inspect_video_frames</code> 回看原视频。原图向量一组通过底层接口单独实验，不表示正式工具已经支持图片。本页图像内嵌，无需启动服务或加载脚本。</p>''']
parts.append(f'<h2>查询参考图</h2><img class="ref" src="{picture("reference.jpg")}" alt="爱音房间俯视参考图"><p>斜俯视房间；人物靠右下；中央茶几与浅色地毯；家具边界呈对角线。以下构图判断来自助手逐图目视，尚未运行 PCP 或人物框重排。</p>')
parts.append('<h2 id="key">先看四个关键结果</h2><div class="grid">')
parts.append(card(image_run['previews'][0], notes['image'][0], '原图向量'))
parts.append(card(queries['layout']['previews'][0], notes['layout'][0], '构图文字'))
parts.append(card(queries['layout']['previews'][4], notes['layout'][4], '构图文字'))
parts.append(card(queries['layout']['previews'][5], notes['layout'][5] + ' 同时出现在原图向量 Rank 5。', '构图文字'))
parts.append('</div><p>Rank 是原始 Event 排名，不是人工挑选后的排名。第 7 集这条候选未进入原图向量 Top 40，说明本次两种召回方式可以互补。</p>')
parts.append('<h2 id="image">A · 原图直接编码 → Event 视觉向量</h2><p>使用现有索引同款 doubao-embedding-vision-250615，原图编码 1 次；当前来源范围内 exact cosine Top 40，全部通过来源和 Event 校验。前 3 条属于同一来源片段。以下展示 Top 1 和去时间重叠后的 7 个候选；完整排名保留在 <a href="image-results.json">原始结果</a>。这是候选召回对照，未把豆包作为专门构图模型。</p><div class="grid">')
for p, note in zip(image_run['previews'], notes['image'], strict=True):
    parts.append(card(p, note, '原图向量'))
parts.append('</div>')
titles = {'content': 'B · 按参考图内容写文字', 'layout': 'C · 去掉人物身份和具体物件，只写构图', 'broad': 'D · 进一步放宽场景限制'}
for key, q in queries.items():
    parts.append(f'<h2 id="{key}">{titles[key]}</h2><blockquote>{html.escape(q["arguments"]["query"])}</blockquote><p>search_video_events：top_k=10，lexical=0 / semantic=0 / visual=1；返回 {len(q["candidates"])} 个 Event、{q["chunk_groups"]} 个 Chunk 分组。visual=1 仍然是文字查询向量匹配图像索引。<a href="{key}-tool.json">工具原始返回</a> · <a href="{key}-trace.json">检索 trace</a></p><div class="grid">')
    for p, note in zip(q['previews'], notes[key], strict=True):
        parts.append(card(p, note, titles[key].split(' · ')[0]))
    parts.append('</div>')
parts.append('''<h2 id="next">获取“构图类似的其他图”的下一步</h2><ol>
<li>用原图向量与构图文字分别召回候选，再合并 Event。当前正式工具只支持后者；前者已有本轮底层实验结果。</li>
<li>依据 Event 的来源和时间抽帧，去掉原镜头、时间重叠、跨集重复画面。对跨镜头 Event 应取多帧检查，不能默认一个 Event 就是一个镜头。</li>
<li>需要人物时，先验证人物存在；用已有 PCP 类别表示与 person 框画布 → DINOv2 的相似度，在同一候选集合里做可调权重重排。俯视/仰视另外核验，不能假定 PCP 的类别分数覆盖机位。</li>
<li>先对本轮实际候选试验：让不同内容但构图相似的第 7 集画面排前，让同房间低机位及 ED 字幕画面排后。记录人工相似性判断，再评估权重，避免用模型分数自证。</li>
</ol><p class="notice">本轮只验证召回和预览，未测试新候选上的 PCP＋人物框融合效果。文字检索每通道默认候选数为 20，top_k 指 Chunk 分组数，单独提高 top_k 不会自动扩充通道候选池。原图组取 40 条且采用 exact 检索，不能用两组的分数或命中比例作严格横向比较。</p>
<p>预览仅为各 Event 中点的一帧；时间是请求的抽帧位置。时间去重阈值为 1.5 秒，仅用于减少重复展示，不代表完成了镜头切分。每条 Event 的完整 ID、来源、合同和抽帧回执见原始 JSON。</p>
<p><a href="README.md">实验说明与复现方式</a> · <a href="results.json">全部文字实验结果</a> · <a href="image-results.json">全部原图实验结果</a></p></main></html>''')
(OUT / 'report.html').write_text('\n'.join(parts))

# Small contact sheet for inline discussion; only resize and label actual source frames.
sheet = Image.new('RGB', (1280, 800), '#f3f5f8')
draw = ImageDraw.Draw(sheet)
font = ImageFont.truetype('/System/Library/Fonts/Supplemental/Arial.ttf', 22)
tiles = [('reference.jpg', 'Reference'),
         (image_run['previews'][0]['image'], 'Image Hit 1 | EP01 09:03.300'),
         (queries['layout']['previews'][4]['image'], 'Layout Rank 5 | EP07 04:08.850'),
         (queries['layout']['previews'][5]['image'], 'Layout Rank 6 / Image Rank 5 | EP10 15:32.950')]
for i, (path, label) in enumerate(tiles):
    x, y = (i % 2) * 640, (i // 2) * 400
    with Image.open(OUT / path) as im:
        sheet.paste(im.convert('RGB').resize((640, 360)), (x, y))
    draw.text((x + 10, y + 365), label, fill='#203048', font=font)
sheet.save(OUT / 'key-comparison.jpg', quality=92)
print(f'Rendered report with {len(text_run["queries"])} text runs and {len(image_run["candidates"])} image candidates')
