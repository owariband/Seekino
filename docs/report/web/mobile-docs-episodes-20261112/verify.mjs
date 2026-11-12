// Isolated browser acceptance: no real API, database, model calls or media playback.
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import { resolve, extname, sep } from 'node:path';
const [repo, buildPath, outputPath, mode = 'baseline'] = process.argv.slice(2);
const { chromium } = createRequire(resolve(repo, 'web/package.json'))('@playwright/test');
const dist = resolve(buildPath), out = resolve(outputPath);
await mkdir(out, { recursive: true });
const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="640" height="360"><defs><linearGradient id="a"><stop stop-color="#19584d"/><stop offset="1" stop-color="#94c6ac"/></linearGradient></defs><rect width="640" height="360" fill="url(#a)"/><path d="M0 300L150 120 320 270 470 80 640 230V360H0Z" fill="#163d35" opacity=".65"/><circle cx="520" cy="90" r="35" fill="#e1d6a7"/><text x="28" y="325" fill="#fff" font-size="20" font-family="sans-serif">Seekino · isolated UI sample</text></svg>`;
const identity = { userId: 'mobile-guest', email: null, isGuest: true, tenantId: null,
  publicTenantId: 'public', mode: 'cloud', csrfToken: '', emailLoginEnabled: false,
  permissions: { canUpload: false, canManage: false, canConfigure: false } };
const videos = Array.from({ length: 13 }, (_, i) => ({
  videoId: `ep-${i + 1}`, tenantId: 'public', seriesId: 'mobile-series', episodeOrder: i + 1,
  uploadSequence: i + 1, originalFilename: null, totalBytes: null, durationSeconds: 1430000,
  uploadStatus: 'COMPLETED', parseStatus: 'SUCCEEDED', uploadProgress: 100, parseProgress: 100,
  indexStatus: 'READY', playbackStatus: i === 12 ? 'PREPARING' : 'READY',
  playbackUrl: i === 12 ? null : `/api/videos/content?videoId=ep-${i + 1}`, playbackUrlExpiresAt: null,
  thumbnail: i === 12 ? null : { imageId: `sample-${i}`, url: '/fixture-frame.svg', expiresAt: null },
  displayName: [ '羽丘の不思議ちゃん', '沿着河岸寻找那些在清晨悄悄出现又消失的声音与记忆',
    'A very long episode title about friends finding their way through a wonderful world',
    'AnExtremelyLongUnbrokenEnglishEpisodeTitleThatMustNeverExpandTheViewport',
    undefined, 'もう誘わない', '暂时的停顿与下一次练习', '在午后相遇', '走向舞台', '新的开始',
    'Another quiet afternoon', '从这里再次出发', '等待准备完成' ][i], lastError: null,
}));
const series = { visibility: 'public', access: 'public', publisher: null,
  seriesId: 'mobile-series', tenantId: 'public', name: '移动端选集验证', description: null,
  status: 'READY', videoCount: 13, readyVideoCount: 12, failedVideoCount: 0, pendingUploadCount: 0,
  cover: { imageId: 'sample-cover', url: '/fixture-frame.svg', expiresAt: null }, lastSearch: null,
  createdAt: 0, updatedAt: 0, transcriptLanguageHint: null, importantExtraData: null,
  characterBible: [], pendingUploads: [], videos };
const server = createServer(async (req, res) => {
  const path = new URL(req.url, 'http://fixture').pathname;
  const requested = resolve(dist, `.${decodeURIComponent(path)}`);
  if (!requested.startsWith(dist + sep) && requested !== dist) { res.writeHead(404).end(); return; }
  const file = extname(requested) ? requested : resolve(dist, 'index.html');
  try {
    const body = await readFile(file);
    res.writeHead(200, { 'content-type': { '.html':'text/html','.js':'text/javascript','.css':'text/css',
      '.svg':'image/svg+xml','.woff2':'font/woff2','.png':'image/png' }[extname(file)] ?? 'application/octet-stream' });
    res.end(body);
  } catch { res.writeHead(404).end(); }
});
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
const origin = `http://127.0.0.1:${server.address().port}`;
const browser = await chromium.launch({ headless: true, channel: 'chrome' });
const results = [];
try {
  const cases = [[320,'zh','light'],[390,'zh','light'],[430,'zh','light'],[760,'zh','light'],
    [761,'zh','light'],[1280,'zh','light'],[390,'en','dark'],[390,'zh','dark'],[390,'en','light']];
  for (const [width, locale, theme] of cases) {
    const context = await browser.newContext({ viewport: { width, height: 844 }, reducedMotion: 'reduce' });
    await context.addInitScript(({ locale, theme }) => {
      localStorage.setItem('seekino-locale', locale); localStorage.setItem('seekino-theme', theme);
      localStorage.setItem('seekino-watch-progress:v3:primary%3Asame-origin:mobile-guest:public', JSON.stringify([
        { seriesId:'mobile-series', videoId:'ep-2', positionSeconds:45125, durationSeconds:1430000, updatedAt:1 }]));
    }, { locale, theme });
    const page = await context.newPage(), errors = [], unexpected = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.route('**/fixture-frame.svg', route => route.fulfill({ contentType:'image/svg+xml', body:svg }));
    await page.route('**/healthz', route => route.fulfill({ json:{ status:'ok' } }));
    await page.route('**/api/**', route => {
      const path = new URL(route.request().url()).pathname;
      if (path === '/api/auth/me') return route.fulfill({ json:identity });
      if (path === '/api/system/runtime-config') return route.fulfill({ json:{ displayMode:'release' } });
      if (path === '/api/series/get') return route.fulfill({ json:series });
      if (['/api/series/list','/api/series/public'].includes(path)) return route.fulfill({ json:{ items:[series], total:1, hasMore:false, nextCursor:null } });
      if (path === '/api/videos/content') return route.fulfill({ status:204 });
      unexpected.push(path); return route.fulfill({ status:404, json:{ message:'Unexpected isolated acceptance API' } });
    });
    await page.goto(`${origin}/series/mobile-series?tenantId=public`);
    await page.locator('#series-episodes ol > li').nth(12).waitFor();
    await page.evaluate(() => document.fonts.ready);
    const docs = page.locator('header').filter({ has: page.locator('a[href="/docs"]') }).first().locator('a[href="/docs"]');
    const docsVisible = await docs.isVisible();
    const docsBox = await docs.boundingBox();
    const docsTextVisible = await docs.evaluate(el => [...el.querySelectorAll('span')].some(span => {
      const r=span.getBoundingClientRect(), s=getComputedStyle(span); return r.width>10 && r.height>10 && s.clipPath==='none'; }));
    const metrics = await page.locator('#series-episodes ol').evaluate(el => {
      const rows=[...el.children].map(li=>{
        const row=li.firstElementChild, r=row.getBoundingClientRect(), p=row.firstElementChild.getBoundingClientRect();
        return { height:r.height,width:r.width,top:r.top,previewWidth:p.width,previewHeight:p.height,
          href:row.getAttribute('href'),disabled:row.getAttribute('aria-disabled')==='true' };
      });
      return { listHeight:el.getBoundingClientRect().height, columns:getComputedStyle(el).gridTemplateColumns.split(' ').length,
        rows, completeIn600:rows.filter(r=>r.top+ r.height<=rows[0].top+600).length,
        overflow:document.documentElement.scrollWidth>innerWidth+1 };
    });
    await page.locator('#series-episodes').evaluate(el => {
      el.scrollIntoView({ block:'start',behavior:'instant' });
      const parent=document.querySelector('[data-app-shell] > div');
      parent.scrollTop+=el.querySelector('ol').getBoundingClientRect().top-74;
    });
    const stem=`${width}-${locale}-${theme}`;
    await page.screenshot({ path:resolve(out, `${stem}.png`) });
    let navigated=false;
    if (docsVisible) { await docs.click(); await page.waitForURL('**/docs'); navigated=true; }
    const result={ width,locale,theme,docsVisible,docsTextVisible,docsBox,navigated,...metrics,errors,unexpected };
    results.push(result);
    if (mode==='fixed') {
      assert.equal(metrics.overflow,false,`${stem}: no overflow`);
      assert.deepEqual(errors,[],`${stem}: no JS errors`); assert.deepEqual(unexpected,[],`${stem}: no unexpected APIs`);
      if (width<=760) {
        assert(docsVisible && docsTextVisible && navigated,`${stem}: visible textual docs link navigates`);
        assert(docsBox.width>=44 && docsBox.height>=44,`${stem}: docs touch target`);
        assert.equal(metrics.columns,1,`${stem}: one list column`);
        assert(metrics.rows.every(r=>r.height<=100),`${stem}: compact row <=100px`);
        assert(metrics.rows.every(r=>r.previewWidth>=111 && r.previewWidth<=129),`${stem}: small preview`);
        assert(metrics.rows.every(r=>Math.abs(r.previewWidth/r.previewHeight-16/9)<0.03),`${stem}: 16:9`);
        assert(metrics.completeIn600>=5,`${stem}: at least five complete rows in 600px`);
      } else assert.equal(metrics.columns,width>1100?4:3,`${stem}: desktop grid`);
      assert.equal(metrics.rows.filter(r=>r.href).length,12,`${stem}: playable links preserved`);
      assert.equal(metrics.rows[12].disabled,true,`${stem}: unavailable episode disabled`);
    }
    await context.close();
  }
} finally {
  await writeFile(resolve(out,'results.json'),JSON.stringify({ mode,dist,browser:browser.version(),results },null,2)+'\n');
  await browser.close(); await new Promise(resolve=>server.close(resolve));
}
console.log(JSON.stringify({ mode,cases:results.length,headline:results.find(r=>r.width===390&&r.locale==='zh'&&r.theme==='light') }));
