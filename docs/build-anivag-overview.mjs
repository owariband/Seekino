import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const docsDir = path.dirname(fileURLToPath(import.meta.url));
const outputPath = path.join(docsDir, 'anivag-system-design.html');

const diagrams = [
  {
    id: 'architecture',
    step: '01',
    label: '系统架构',
    eyebrow: 'System boundary',
    summary: 'AniVAG 是产品主仓；VideoRAG 通过 Adapter 作为可替换底座。',
    file: 'anivag-animation-m1.architecture.html',
  },
  {
    id: 'indexing',
    step: '02',
    label: '动画建库',
    eyebrow: 'Happy path',
    summary: 'Video-only 输入，经音轨、镜头、AnimeEvent 处理后原子发布版本化索引。',
    file: 'anivag-indexing.sequence.html',
  },
  {
    id: 'recovery',
    step: '03',
    label: '断点续跑',
    eyebrow: 'Failure path',
    summary: 'Job Store 保存 lease 与 checkpoint，Worker 复用产物并从失败批次恢复。',
    file: 'anivag-recovery.wide.sequence.html',
  },
  {
    id: 'search',
    step: '04',
    label: '名场面检索',
    eyebrow: 'Online query',
    summary: '语音文本、视觉、角色事件三路召回，返回可跳转的时间证据。',
    file: 'anivag-search.compact.sequence.html',
  },
].map((diagram) => ({
  ...diagram,
  html: fs.readFileSync(path.join(docsDir, 'diagrams', diagram.file), 'utf8'),
}));

const embedded = JSON.stringify(
  Object.fromEntries(diagrams.map(({ id, html }) => [id, html])),
).replaceAll('<', '\\u003c');
const manifest = JSON.stringify(diagrams.map(({ html: _html, ...diagram }) => diagram));

const document = `<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>AniVAG · 视频事件检索工程蓝图</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #071017;
      --panel: rgba(14, 29, 39, 0.84);
      --panel-strong: #102430;
      --line: rgba(148, 181, 199, 0.18);
      --text: #ecf7fb;
      --muted: #8ca7b4;
      --cyan: #49d6de;
      --mint: #5ce5ac;
      --amber: #ffbc66;
      --violet: #aa8cff;
      --shadow: 0 24px 70px rgba(0, 0, 0, 0.34);
    }

    * { box-sizing: border-box; }

    html, body { min-height: 100%; }

    body {
      margin: 0;
      background:
        radial-gradient(circle at 12% 0%, rgba(73, 214, 222, 0.15), transparent 28rem),
        radial-gradient(circle at 88% 12%, rgba(170, 140, 255, 0.12), transparent 32rem),
        var(--bg);
      color: var(--text);
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }

    button { font: inherit; }

    .shell {
      width: min(1640px, calc(100% - 40px));
      margin: 0 auto;
      padding: 28px 0 36px;
    }

    .masthead {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 28px;
      align-items: end;
      margin-bottom: 22px;
    }

    .kicker {
      margin: 0 0 10px;
      color: var(--cyan);
      font: 700 12px/1 ui-monospace, SFMono-Regular, Menlo, monospace;
      letter-spacing: 0.16em;
      text-transform: uppercase;
    }

    h1 {
      margin: 0;
      max-width: 850px;
      font-size: clamp(30px, 4.2vw, 64px);
      line-height: 0.98;
      letter-spacing: -0.045em;
    }

    .subtitle {
      max-width: 780px;
      margin: 16px 0 0;
      color: var(--muted);
      font-size: 15px;
      line-height: 1.7;
    }

    .scope {
      display: grid;
      grid-template-columns: repeat(2, minmax(150px, 1fr));
      gap: 10px;
      min-width: 370px;
    }

    .scope-card {
      min-height: 76px;
      padding: 14px 16px;
      border: 1px solid var(--line);
      border-radius: 14px;
      background: rgba(13, 28, 37, 0.72);
      box-shadow: inset 0 1px rgba(255,255,255,0.03);
    }

    .scope-card span {
      display: block;
      margin-bottom: 7px;
      color: var(--muted);
      font: 700 10px/1 ui-monospace, SFMono-Regular, Menlo, monospace;
      letter-spacing: 0.12em;
      text-transform: uppercase;
    }

    .scope-card strong { font-size: 14px; }

    .workspace {
      overflow: hidden;
      border: 1px solid var(--line);
      border-radius: 22px;
      background: var(--panel);
      box-shadow: var(--shadow);
      backdrop-filter: blur(18px);
    }

    .rail {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      border-bottom: 1px solid var(--line);
      background: rgba(4, 15, 21, 0.76);
    }

    .tab {
      position: relative;
      display: grid;
      grid-template-columns: auto minmax(0, 1fr);
      gap: 12px;
      align-items: center;
      min-height: 84px;
      padding: 15px 18px;
      border: 0;
      border-right: 1px solid var(--line);
      color: var(--muted);
      background: transparent;
      text-align: left;
      cursor: pointer;
      transition: color 160ms ease, background 160ms ease;
    }

    .tab:last-child { border-right: 0; }
    .tab:hover { color: var(--text); background: rgba(73, 214, 222, 0.045); }
    .tab[aria-selected="true"] { color: var(--text); background: rgba(73, 214, 222, 0.08); }
    .tab[aria-selected="true"]::after {
      content: "";
      position: absolute;
      inset: auto 16px -1px;
      height: 2px;
      background: linear-gradient(90deg, var(--cyan), var(--mint));
      box-shadow: 0 0 18px rgba(73, 214, 222, 0.75);
    }

    .step {
      color: var(--cyan);
      font: 800 12px/1 ui-monospace, SFMono-Regular, Menlo, monospace;
    }

    .tab-copy small {
      display: block;
      margin-bottom: 5px;
      color: inherit;
      font: 650 9px/1 ui-monospace, SFMono-Regular, Menlo, monospace;
      letter-spacing: 0.12em;
      text-transform: uppercase;
      opacity: 0.72;
    }

    .tab-copy strong { font-size: 14px; }

    .context {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 24px;
      min-height: 62px;
      padding: 13px 20px;
      border-bottom: 1px solid var(--line);
      background: rgba(16, 36, 48, 0.62);
    }

    .context p { margin: 0; color: #bed0d8; font-size: 13px; line-height: 1.55; }

    .badges { display: flex; flex-wrap: wrap; gap: 8px; justify-content: flex-end; }

    .badge {
      padding: 6px 9px;
      border: 1px solid var(--line);
      border-radius: 999px;
      color: var(--muted);
      background: rgba(5, 15, 21, 0.45);
      font: 700 10px/1 ui-monospace, SFMono-Regular, Menlo, monospace;
      white-space: nowrap;
    }

    .viewer {
      position: relative;
      height: min(74vh, 980px);
      min-height: 650px;
      background: #f4f7f9;
    }

    iframe { display: block; width: 100%; height: 100%; border: 0; background: #f4f7f9; }

    .loading {
      position: absolute;
      inset: 0;
      display: grid;
      place-items: center;
      color: #45606c;
      background: #f4f7f9;
      font: 700 12px/1 ui-monospace, SFMono-Regular, Menlo, monospace;
      letter-spacing: 0.08em;
      transition: opacity 180ms ease;
      pointer-events: none;
    }

    .loading.hidden { opacity: 0; }

    .footnote {
      display: flex;
      justify-content: space-between;
      gap: 20px;
      margin-top: 14px;
      color: #6f8c99;
      font-size: 11px;
      line-height: 1.5;
    }

    @media (max-width: 980px) {
      .shell { width: min(100% - 24px, 1640px); padding-top: 20px; }
      .masthead { grid-template-columns: 1fr; }
      .scope { min-width: 0; }
      .rail { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .tab:nth-child(2) { border-right: 0; }
      .tab:nth-child(-n+2) { border-bottom: 1px solid var(--line); }
      .context { align-items: flex-start; flex-direction: column; gap: 10px; }
      .badges { justify-content: flex-start; }
      .viewer { height: 72vh; min-height: 560px; }
    }
  </style>
</head>
<body>
  <main class="shell">
    <header class="masthead">
      <div>
        <p class="kicker">AniVAG / Engineering blueprint</p>
        <h1>动画名场面检索，<br>从产品入口到时间证据。</h1>
        <p class="subtitle">AniVAG 面向事件级视频检索与时间证据定位；M1 以动画名场面为首个场景，VideoRAG 暂作为可替换算法底座。</p>
      </div>
      <aside class="scope" aria-label="MVP 边界">
        <div class="scope-card"><span>Primary domain</span><strong>Anime-first</strong></div>
        <div class="scope-card"><span>Input contract</span><strong>Video-only</strong></div>
        <div class="scope-card"><span>Async core</span><strong>Job Store + Worker</strong></div>
        <div class="scope-card"><span>MQ policy</span><strong>Scale trigger, not default</strong></div>
      </aside>
    </header>

    <section class="workspace" aria-label="AniVAG 工程图">
      <nav class="rail" role="tablist" aria-label="工程视图"></nav>
      <div class="context">
        <p id="diagram-summary"></p>
        <div class="badges" aria-label="设计约束">
          <span class="badge">NO EXTERNAL SUBTITLE</span>
          <span class="badge">VERSIONED INDEX</span>
          <span class="badge">TIME EVIDENCE</span>
        </div>
      </div>
      <div class="viewer">
        <iframe id="diagram-frame" title="AniVAG 工程图"></iframe>
        <div id="loading" class="loading">LOADING EMBEDDED DIAGRAM…</div>
      </div>
    </section>

    <footer class="footnote">
      <span>四张图均内嵌在本文件中，可离线打开；切换视图不会请求网络。</span>
      <span>Ownership: M0 Adapter → M1 minimum reimplementation → M2 anime event retrieval.</span>
    </footer>
  </main>

  <script>
    const manifest = ${manifest};
    const embedded = ${embedded};
    const rail = document.querySelector('.rail');
    const frame = document.querySelector('#diagram-frame');
    const summary = document.querySelector('#diagram-summary');
    const loading = document.querySelector('#loading');

    function selectDiagram(id, updateHash = true) {
      const selected = manifest.find((item) => item.id === id) || manifest[0];
      document.querySelectorAll('.tab').forEach((tab) => {
        const active = tab.dataset.id === selected.id;
        tab.setAttribute('aria-selected', String(active));
        tab.tabIndex = active ? 0 : -1;
      });
      summary.textContent = selected.summary;
      loading.classList.remove('hidden');
      frame.title = selected.label;
      frame.srcdoc = embedded[selected.id];
      if (updateHash) history.replaceState(null, '', '#' + selected.id);
    }

    manifest.forEach((item) => {
      const button = document.createElement('button');
      button.className = 'tab';
      button.type = 'button';
      button.role = 'tab';
      button.dataset.id = item.id;
      button.innerHTML = '<span class="step">' + item.step + '</span><span class="tab-copy"><small>' + item.eyebrow + '</small><strong>' + item.label + '</strong></span>';
      button.addEventListener('click', () => selectDiagram(item.id));
      rail.append(button);
    });

    frame.addEventListener('load', () => loading.classList.add('hidden'));
    selectDiagram(location.hash.slice(1), false);
  </script>
</body>
</html>`;

fs.writeFileSync(outputPath, document);
console.log(JSON.stringify({ output: outputPath, bytes: Buffer.byteLength(document), diagrams: diagrams.length }));
