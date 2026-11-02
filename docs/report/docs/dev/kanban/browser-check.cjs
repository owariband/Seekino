const path=require('node:path'),os=require('node:os'),fs=require('node:fs'),assert=require('node:assert/strict');
const {pathToFileURL}=require('node:url');
const root=process.env.KANBAN_REPO_ROOT||path.resolve(__dirname,'../../../../..');
const {chromium}=require(process.env.KANBAN_PLAYWRIGHT_PATH||path.join(root,'web/node_modules/playwright'));
const source=path.join(root,'docs/dev/kanban.html'),output=process.argv[2]||path.join(os.tmpdir(),'seekino-kanban-readonly-check');
fs.mkdirSync(output,{recursive:true});
const sourceHTML=fs.readFileSync(source,'utf8'),jsonPattern=/(<script id="kanban-data" type="application\/json">)([\s\S]*?)(<\/script>)/;
const data=JSON.parse(sourceHTML.match(jsonPattern)[2]),checks=[],errors=[],requests=[];
const ok=(name,condition=true)=>{assert.ok(condition,name);checks.push(name);console.log('PASS '+name)};
const observe=page=>{page.on('pageerror',e=>errors.push(e.message));page.on('request',r=>{if(/^https?:/.test(r.url()))requests.push(r.url())})};
(async()=>{
 const browser=await chromium.launch({channel:'chrome',headless:true});
 try{
  const context=await browser.newContext({viewport:{width:1440,height:900}});await context.setOffline(true);
  // Seed the exact legacy draft key with a newer revision: the repository must still win.
  const stale={...data,revision:data.revision+1000,cards:data.cards.map(c=>({...c,title:'OLD-DRAFT '+c.title}))};
  await context.addInitScript(draft=>{localStorage.setItem('seekino-kanban-draft-v1:'+location.pathname,JSON.stringify(draft))},stale);
  const page=await context.newPage();observe(page);await page.goto(pathToFileURL(source).href);
  ok('Repository data renders all 73 cards',(await page.locator('#result-label').textContent()).includes('73'));
  ok('Legacy draft cannot override repository cards',!(await page.locator('#board').textContent()).includes('OLD-DRAFT'));
  ok('Legacy draft is not silently deleted',await page.evaluate(()=>JSON.parse(localStorage.getItem('seekino-kanban-draft-v1:'+location.pathname)).cards[0].title.startsWith('OLD-DRAFT')));
  ok('Page explains shared repository maintenance',(await page.locator('#board-version').textContent()).includes('项目统一维护'));
  ok('No save, export, import or editing controls',await page.locator('[data-action="save"],[data-action="add"],[data-action="edit"],[data-action="import"],[data-action="export-json"],input[type="file"],#editor-dialog').count()===0);
  ok('Cards cannot be dragged to change status',await page.locator('[draggable="true"],[data-drop-category],[data-add-category]').count()===0);
  ok('Exactly three status columns',await page.locator('#lane-parse .column').count()===3);
  ok('Desktop has no horizontal overflow',await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
  await page.screenshot({path:output+'/desktop.png',fullPage:true});
  await page.locator('[data-category-filter="quality"]').click();
  ok('Category filtering opens chosen category',await page.locator('#lane-quality').isVisible()&&await page.locator('.lane').count()===1);
  await page.locator('[data-toggle-category="quality"]').click();
  ok('Category can collapse',!await page.locator('#lane-quality').isVisible());
  await page.locator('[data-category-filter="all"]').click();
  for(const [type,count] of [['module',7],['requirement',19],['task',47]]){await page.locator('[data-type-filter="'+type+'"]').click();ok(type+' type filter preserves '+count+' records',await page.locator('.work-card:visible').count()===count)}
  await page.locator('[data-type-filter="all"]').click();await page.locator('#search').fill('PR-25');
  ok('Search finds PR and relationships',await page.locator('[data-card="PR-25"]').isVisible());
  await page.locator('[data-open="PR-25"]').click();
  ok('Detail status is read-only',await page.locator('#detail-dialog select,#detail-dialog input').count()===0&&await page.locator('.detail-status').textContent()==='PROGRESSING');
  ok('PR and TRD links remain available',await page.locator('#detail-content a[href="https://github.com/owariband/Seekino/pull/25"]').count()===1&&(await page.locator('#detail-content a').allTextContents()).some(t=>t.includes('TRD')));
  await page.screenshot({path:output+'/detail.png'});
  await page.locator('[data-related="PR-18"]').click();ok('Upstream card navigation works',await page.locator('#detail-id').textContent()==='PR-18');
  await page.keyboard.press('Escape');ok('Keyboard dismisses detail dialog',!await page.locator('#detail-dialog').isVisible());
  await page.locator('#search').fill('QA-NO-MATCH');ok('Empty results provide recovery',await page.locator('.empty-results').isVisible());
  await page.locator('[data-action="clear-filters"]').click();ok('Clear filters restores project board',(await page.locator('#result-label').textContent()).includes('73'));
  await page.locator('[data-action="help"]').first().click();ok('Help explains Agent updates to the repository',(await page.locator('.help-content').textContent()).includes('由 Agent 更新仓库中的 docs/dev/kanban.html'));await page.keyboard.press('Escape');
  await page.emulateMedia({colorScheme:'dark'});ok('Dark OS preference keeps the single light theme',await page.evaluate(()=>getComputedStyle(document.documentElement).backgroundColor==='rgb(228, 234, 229)'));
  ok('Viewing interactions preserve embedded records',JSON.stringify(JSON.parse(await page.locator('#kanban-data').textContent()))===JSON.stringify(data));
  // Simulate an Agent changing the canonical file, in a temporary directory only.
  const temp=path.join(output,'repository-update.html');fs.writeFileSync(temp,sourceHTML);await page.goto(pathToFileURL(temp).href);
  const updated=JSON.parse(JSON.stringify(data));updated.revision++;updated.cards[0].title='QA-REPO-UPDATE';
  fs.writeFileSync(temp,sourceHTML.replace(jsonPattern,(_,open,body,close)=>open+JSON.stringify(updated).replace(/</g,'\\u003c')+close));
  await page.reload();await page.locator('#search').fill('QA-REPO-UPDATE');
  ok('Refreshing after repository edit shows updated card',await page.locator('.card-title').textContent()==='QA-REPO-UPDATE');
  ok('Newer legacy browser draft still cannot mask file updates',!(await page.locator('#board').textContent()).includes('OLD-DRAFT'));
  const mobileContext=await browser.newContext({viewport:{width:375,height:812},isMobile:true,hasTouch:true});await mobileContext.setOffline(true);
  const mobile=await mobileContext.newPage();observe(mobile);await mobile.goto(pathToFileURL(source).href);
  ok('Mobile has no page overflow',await mobile.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
  ok('Mobile preserves scrollable three-column lanes',await mobile.locator('#lane-parse').evaluate(e=>e.scrollWidth>e.clientWidth&&e.children.length===3));
  await mobile.screenshot({path:output+'/mobile.png',fullPage:true});
  await mobile.locator('#mobile-category').selectOption('quality');await mobile.locator('#search').fill('PM-01');await mobile.locator('[data-open="PM-01"]').click();
  ok('Mobile detail and category navigation work',await mobile.locator('#detail-dialog').evaluate(e=>e.getBoundingClientRect().width<=innerWidth));
  ok('Browser reports no script errors',errors.length===0);ok('Board loads without network requests',requests.length===0);
  fs.writeFileSync(output+'/readonly-results.json',JSON.stringify({checkedAt:new Date().toISOString(),browser:await browser.version(),checks,errors,requests,limitations:['Chrome desktop/mobile viewports only; historical business acceptance was not rerun.','File update test writes a temporary copy; repository card records are unchanged by the test.']},null,2)+'\n');
  console.log(JSON.stringify({passed:checks.length,output}));
 }finally{await browser.close()}
})().catch(e=>{console.error(e);process.exitCode=1});
