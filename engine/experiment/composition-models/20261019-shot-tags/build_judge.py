"""Build an offline blinded judging page from pool.json; creates no labels."""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent

HTML = r'''<!doctype html>
<html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>MyGO composition judging</title>
<style>
:root{color-scheme:dark;font:16px system-ui,sans-serif;background:#10141c;color:#edf1f7}*{box-sizing:border-box}
body{max-width:1500px;margin:auto;padding:20px}h1{font-size:24px;margin:0 0 8px}p{line-height:1.5;margin:8px 0}
header,section,.panel{background:#1b2230;border:1px solid #344054;border-radius:12px;padding:18px;margin:12px 0}
.muted{color:#acb9cf}.controls{display:flex;align-items:center;gap:10px;flex-wrap:wrap}button,select,input,textarea{font:inherit}
button,select{background:#28364d;color:inherit;border:1px solid #5a6c88;border-radius:6px;padding:9px 13px;cursor:pointer}
button:hover{background:#344763}button[aria-pressed=true]{background:#2456a7;border-color:#9bbfff}button:focus-visible,select:focus-visible,textarea:focus-visible{outline:3px solid #97c1ff}
.pair{display:grid;grid-template-columns:1fr 1fr;gap:16px}.pair figure{margin:0;min-width:0}.pair img{width:100%;height:auto;display:block;background:#000}
figcaption{padding:8px 0;color:#c6d0df}.ratings{display:grid;grid-template-columns:1fr 1fr;gap:14px}.rating{border:2px solid transparent;border-radius:8px;padding:12px;background:#121b29}
.rating.active{border-color:#9bbfff}.rating h2{font-size:18px;margin:0 0 10px}.choices{display:flex;gap:8px}.choices button{flex:1}
textarea{width:100%;height:65px;background:#111925;border:1px solid #64748b;border-radius:6px;color:inherit;padding:10px}
#storageWarning{color:#ffcc85}kbd{padding:1px 5px;border:1px solid #64748b;border-radius:3px;background:#263247}#status{min-height:24px}
@media(max-width:800px){body{padding:8px}.pair,.ratings{grid-template-columns:1fr}.controls button{padding:8px}}
</style>
<body><header><h1>MyGO composition judging</h1>
<p><b>Category</b>: the same organising pattern — angle, shot size and composition type — regardless of content.<br>
<b>Layout</b>: people or subjects in similar positions, sizes and counts.</p>
<p class="muted">Rate each independently: <b>0</b> no match · <b>1</b> partial match · <b>2</b> strong match. Method identities and ranks are hidden. Unanswered fields stay unjudged.</p>
<div class="controls"><label>Query <select id="querySelect"></select></label><button id="previous">← Previous</button><button id="next">Next →</button><button id="nextUnjudged">Next unjudged</button><button id="export">Export judgments.jsonl</button><span id="progress"></span></div>
<p id="storageWarning" hidden>Browser storage is unavailable. Keep this page open and export your judgments before closing it.</p></header>
<main><section><div class="pair"><figure><img id="queryImage" alt="Query keyframe"><figcaption id="queryCaption"></figcaption></figure><figure><img id="candidateImage" alt="Candidate keyframe"><figcaption id="candidateCaption"></figcaption></figure></div></section>
<section><div class="ratings"><div class="rating active" data-field="category"><h2>Category match</h2><div class="choices"><button data-field="category" data-value="0">0 · No</button><button data-field="category" data-value="1">1 · Partial</button><button data-field="category" data-value="2">2 · Strong</button><button data-field="category" data-value="clear">Clear</button></div></div>
<div class="rating" data-field="layout"><h2>Layout match</h2><div class="choices"><button data-field="layout" data-value="0">0 · No</button><button data-field="layout" data-value="1">1 · Partial</button><button data-field="layout" data-value="2">2 · Strong</button><button data-field="layout" data-value="clear">Clear</button></div></div></div>
<div class="controls" style="margin:15px 0"><button id="sameLocation">Same location / scene: unanswered</button><button id="duplicate">Duplicate / reused shot: unanswered</button></div>
<label>Optional note<textarea id="note" placeholder="Your observation"></textarea></label><p class="muted">Keyboard: <kbd>C</kbd> category, <kbd>L</kbd> layout; <kbd>0</kbd>/<kbd>1</kbd>/<kbd>2</kbd> rate selected field; <kbd>S</kbd> same scene and <kbd>D</kbd> duplicate cycle unanswered → yes → no; <kbd>←</kbd>/<kbd>→</kbd> move; <kbd>U</kbd> next unjudged; <kbd>E</kbd> export.</p><div id="status" aria-live="polite"></div></section></main>
<script id="poolData" type="application/json">__POOL__</script>
<script>
'use strict';
const pool=JSON.parse(document.getElementById('poolData').textContent), key='mygo-shot-tags-__DIGEST__';
let state={labels:{},query:0,candidate:0}, activeField='category', storageOkay=true;
try{const saved=localStorage.getItem(key);if(saved){const value=JSON.parse(saved);if(value&&value.labels&&typeof value.labels==='object'&&!Array.isArray(value.labels))state=value;}}catch(error){storageOkay=false;}
const el=id=>document.getElementById(id), queries=pool.queries;
state.query=Math.min(Math.max(0,Number(state.query)||0),queries.length-1);
function query(){return queries[state.query];}function candidate(){return query().candidates[state.candidate];}
function pairKey(){return query().query_shot_id+'|'+candidate().candidate_shot_id;}
function current(){return state.labels[pairKey()]||{query_shot_id:query().query_shot_id,candidate_shot_id:candidate().candidate_shot_id,labels:{category:null,layout:null,same_location:null,duplicate:null},note:'',timestamp:null};}
function persist(){try{localStorage.setItem(key,JSON.stringify(state));}catch(error){storageOkay=false;}el('storageWarning').hidden=storageOkay;}
function changed(edit){const record=current();edit(record);record.timestamp=new Date().toISOString();state.labels[pairKey()]=record;persist();renderControls();el('status').textContent=storageOkay?'Saved in this browser.':'Saved in memory. Export to preserve progress.';}
function setActive(field){activeField=field;document.querySelectorAll('.rating').forEach(node=>node.classList.toggle('active',node.dataset.field===field));}
function renderControls(){const record=current();document.querySelectorAll('[data-value]').forEach(button=>button.setAttribute('aria-pressed',record.labels[button.dataset.field]===Number(button.dataset.value)&&button.dataset.value!=='clear'));
  for(const [id,field,label]of[['sameLocation','same_location','Same location / scene'],['duplicate','duplicate','Duplicate / reused shot']]){const value=record.labels[field];el(id).textContent=label+': '+(value===null?'unanswered':value?'yes':'no');el(id).setAttribute('aria-pressed',value===true);}
  const records=Object.values(state.labels),complete=records.filter(row=>row.labels.category!==null&&row.labels.layout!==null).length,flagsComplete=records.filter(row=>row.labels.same_location!==null&&row.labels.duplicate!==null).length,total=queries.reduce((n,q)=>n+q.candidates.length,0);el('progress').textContent=complete+' / '+total+' pairs rated · '+flagsComplete+' / '+total+' flags complete';el('storageWarning').hidden=storageOkay;
}
function render(){state.candidate=Math.min(Math.max(0,Number(state.candidate)||0),query().candidates.length-1);el('querySelect').value=state.query;el('queryImage').src=query().image_path;el('candidateImage').src=candidate().image_path;el('queryCaption').textContent='Query '+(state.query+1)+' of '+queries.length;el('candidateCaption').textContent='Candidate '+(state.candidate+1)+' of '+query().candidates.length+' · blinded order';el('note').value=current().note||'';el('status').textContent='';renderControls();persist();}
function move(delta){state.candidate+=delta;if(state.candidate>=query().candidates.length){state.query=(state.query+1)%queries.length;state.candidate=0;}else if(state.candidate<0){state.query=(state.query-1+queries.length)%queries.length;state.candidate=query().candidates.length-1;}render();}
function nextUnjudged(){const total=queries.reduce((n,q)=>n+q.candidates.length,0);for(let n=0;n<total;n++){state.candidate++;if(state.candidate>=query().candidates.length){state.query=(state.query+1)%queries.length;state.candidate=0;}const v=current().labels;if(v.category===null||v.layout===null||v.same_location===null||v.duplicate===null){render();return;}}render();el('status').textContent='Every pair has both ratings and both flags.';}
function cycle(field){changed(row=>{const value=row.labels[field];row.labels[field]=value===null?true:value===true?false:null;});}
function exportData(){const rows=Object.values(state.labels).filter(row=>row.timestamp);const text=rows.map(row=>JSON.stringify(row)).join('\n')+(rows.length?'\n':'');const url=URL.createObjectURL(new Blob([text],{type:'application/x-ndjson'}));const link=document.createElement('a');link.href=url;link.download='judgments.jsonl';document.body.appendChild(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);el('status').textContent='Exported '+rows.length+' judgment records (including any cleared records).';}
queries.forEach((q,index)=>{const option=document.createElement('option');option.value=index;option.textContent='Query '+(index+1);el('querySelect').appendChild(option);});
el('querySelect').addEventListener('change',event=>{state.query=Number(event.target.value);state.candidate=0;render();});
document.querySelectorAll('[data-value]').forEach(button=>button.addEventListener('click',()=>{setActive(button.dataset.field);changed(row=>row.labels[button.dataset.field]=button.dataset.value==='clear'?null:Number(button.dataset.value));}));
document.querySelectorAll('.rating').forEach(node=>node.addEventListener('click',()=>setActive(node.dataset.field)));
el('sameLocation').onclick=()=>cycle('same_location');el('duplicate').onclick=()=>cycle('duplicate');el('previous').onclick=()=>move(-1);el('next').onclick=()=>move(1);el('nextUnjudged').onclick=nextUnjudged;el('export').onclick=exportData;
el('note').addEventListener('input',event=>changed(row=>row.note=event.target.value));
document.addEventListener('keydown',event=>{if(event.target.matches('textarea,input,select')||event.ctrlKey||event.metaKey||event.altKey)return;const k=event.key.toLowerCase();if(['0','1','2','c','l','s','d','arrowleft','arrowright','u','e'].includes(k))event.preventDefault();if(['0','1','2'].includes(k))changed(row=>row.labels[activeField]=Number(k));else if(k==='c'||k==='l')setActive(k==='c'?'category':'layout');else if(k==='s')cycle('same_location');else if(k==='d')cycle('duplicate');else if(k==='arrowleft')move(-1);else if(k==='arrowright')move(1);else if(k==='u')nextUnjudged();else if(k==='e')exportData();});
render();
</script></body></html>'''


def main():
    raw = (ROOT / "pool.json").read_text()
    pool = json.loads(raw)
    assert pool["queries"] and all(row["candidates"] for row in pool["queries"])
    # Only blinded presentation fields enter the page. Method ownership remains
    # in rankings and a separate provenance artifact outside the judging UI.
    public = {"queries": [{"query_shot_id": q["query_shot_id"], "image_path": q["image_path"],
                            "candidates": [{"candidate_shot_id": c["candidate_shot_id"], "image_path": c["image_path"]}
                                           for c in q["candidates"]]} for q in pool["queries"]]}
    encoded = json.dumps(public, ensure_ascii=False).replace("<", "\\u003c")
    output = HTML.replace("__POOL__", encoded).replace("__DIGEST__", hashlib.sha256(raw.encode()).hexdigest()[:16])
    (ROOT / "judge.html").write_text(output)
    print(json.dumps({"queries": len(public["queries"]), "pairs": sum(len(q["candidates"]) for q in public["queries"]), "page": str(ROOT / "judge.html")}))


if __name__ == "__main__":
    main()
