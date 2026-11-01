from collections import Counter
from difflib import SequenceMatcher
import hashlib
import json
from pathlib import Path
import re

HERE = Path(__file__).resolve().parent


def load(p):
    return json.loads(Path(p).read_text())


def words(text):
    return re.findall(r"[a-z]+(?:'[a-z]+)?|\d+", text.casefold().replace('’', "'"))


def dialogue_text(events):
    return ' '.join(v['text'] for e in events if e['event_type']=='DIALOGUE'
                    for v in e['text_variants'] if v['role']=='ORIGINAL')


def speech_diff(asr, events):
    source = words(' '.join(c['text'] for c in asr))
    output = words(dialogue_text(events))
    changes = []
    for tag,a,b,c,d in SequenceMatcher(a=source,b=output,autojunk=False).get_opcodes():
        if tag!='equal':
            changes.append({'op':tag,'asr':' '.join(source[a:b]),'output':' '.join(output[c:d])})
    return {'asr_word_count':len(source),'output_word_count':len(output),'changes':changes}


def trace_metrics(directory):
    calls=[]
    for p in sorted(directory.glob('*/events.jsonl')):
        es=[json.loads(l) for l in p.read_text().splitlines()]
        t=es[0]['time_ns'];req=es[0]['payload']['request']
        c={'call_id':p.parent.name,'last_event':es[-1]['event'],'elapsed_seconds':(es[-1]['time_ns']-t)/1e9,
           'request_chars':len(json.dumps(req,ensure_ascii=False)),'message_count':len(req.get('messages',[])),
           'tools_offered':[x.get('function',{}).get('name') for x in req.get('tools',[])],
           'first_sse_seconds':None,'reasoning_seconds':None,'output_seconds':None,'reasoning_chars':0}
        sse=[e for e in es if e['event']=='sse']
        if sse: c['first_sse_seconds']=(sse[0]['time_ns']-t)/1e9
        for key,label in [('reasoning_content','reasoning'),('content','output'),('tool_calls','tool_args')]:
            ps=[e for e in sse if any(x.get('delta',{}).get(key) for x in e['payload'].get('choices',[]))]
            if ps: c[label+'_seconds']=(ps[-1]['time_ns']-ps[0]['time_ns'])/1e9
            if key=='reasoning_content':
                c['reasoning_chars']=sum(len(x.get('delta',{}).get(key) or '') for e in ps for x in e['payload'].get('choices',[]))
        responses=[e['payload'] for e in es if e['event']=='response']
        if responses:
            response=responses[-1]
            c['usage']=response.get('usage')
            c['tools_returned']=[x['function']['name'] for ch in response.get('choices',[]) for x in ch.get('message',{}).get('tool_calls',[]) or []]
        calls.append(c)
    return calls


def tool_feedback(directory):
    seen = {}
    for p in directory.glob('*/events.jsonl'):
        with p.open() as f:
            request = json.loads(f.readline())['payload']['request']
        for m in request.get('messages', []):
            if m.get('role') != 'tool':
                continue
            content = m.get('content', '')
            text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
            key = m.get('tool_call_id') or hashlib.sha256(text.encode()).hexdigest()
            if 'Invalid arguments for ' in text or '"error"' in text:
                seen[key] = text[:1500]
    return list(seen.values())


def compare_visual(before,after):
    def key(e):
        return json.dumps({k:v for k,v in e.items() if k not in ('participants','visible_persons','speaker_source')},ensure_ascii=False,sort_keys=True)
    a=Counter(key(e) for e in before if e['event_type']!='DIALOGUE')
    b=Counter(key(e) for e in after if e['event_type']!='DIALOGUE')
    return {'unchanged_except_person_labels':sum((a & b).values()),
            'removed_or_changed':[json.loads(k) for k in (a-b).elements()],
            'added_or_changed':[json.loads(k) for k in (b-a).elements()]}


def analyze():
    old=load(HERE/'refine-source.json')['members']
    by_sequence={m['sequence_no']:m for m in old}
    raw=load(HERE.parent/'speaker-context-20261101/first-chunk.json')['detail']['evidence']['asr']['transcript']
    unique={}
    for m in old:
        for c in m['detail']['evidence']['asr']['transcript']:
            unique[(c['start_seconds'],c['end_seconds'],c['text'])]=c
    all_asr=[unique[k] for k in sorted(unique)]
    report={}
    for arm in ('understanding-control','understanding-candidate','refine-canvas','refine-json'):
        path=HERE/arm
        if not (path/'summary.json').exists(): continue
        s=load(path/'summary.json');us=[u for u in s.get('usage',[]) if u.get('granularity')=='provider_request']
        metrics={k:s.get(k) for k in ('status','elapsed_seconds','error_type','error')}
        metrics['provider_requests']=len(us) if us else None
        metrics['tools']=dict(Counter(t['name'] for t in s.get('tools',[])))
        metrics['usage']={k:sum(u['usage'][k] for u in us if u.get('usage') is not None and u['usage'].get(k) is not None)
                          if any(u.get('usage') is not None and u['usage'].get(k) is not None for u in us) else None
                          for k in ('input_tokens','output_tokens','reasoning_tokens','cached_input_tokens')}
        metrics['usage_missing_requests']=sum(u.get('usage') is None for u in us) if us else None
        metrics['calls']=trace_metrics(path/'model-trace')
        metrics['tool_calls_returned']=dict(Counter(t for c in metrics['calls'] for t in c.get('tools_returned',[])))
        metrics['tool_errors']=tool_feedback(path/'model-trace')
        if not metrics['calls']:
            metrics['trace_gap']='This control invocation omitted task-id context required by diagnostics; its usage observer also could not deepcopy MappingProxyType. Wall time and result retained; SSE/CoT/usage unavailable. Both observers corrected for subsequent arms; no rerun.'
        if (path/'result.json').exists():
            result=load(path/'result.json')
            if arm.startswith('understanding'):
                events=result['timeline']
                metrics['speech_diff']=speech_diff(raw,events)
                metrics['persons']=result['persons']
            else:
                events=[e for m in sorted(result['members'],key=lambda m:m['sequence_no']) for e in m['timeline']]
                speech=sorted((e for e in events if e['event_type']=='DIALOGUE'),key=lambda e:e['start_seconds'])
                metrics['speech_diff']=speech_diff(all_asr,speech)
                metrics['member_event_counts']={m['sequence_no']:len(m['timeline']) for m in result['members']}
                metrics['visual_diff']={m['sequence_no']:compare_visual(by_sequence[m['sequence_no']]['content']['timeline'],m['timeline']) for m in result['members']}
            metrics['event_counts']=dict(Counter(e['event_type'] for e in events))
            metrics['speakers']=dict(Counter(e.get('speaker') or 'null' for e in events if e['event_type']=='DIALOGUE'))
            metrics['boundary_dialogues']=[e for e in events if e['event_type']=='DIALOGUE' and any(e['start_seconds']<t<e['end_seconds'] for t in (60000,120000,180000))]
        report[arm]=metrics
    (HERE/'analysis.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    for arm,metrics in report.items():
        print(arm,json.dumps({k:v for k,v in metrics.items() if k not in ('calls','speech_diff','visual_diff','boundary_dialogues')},ensure_ascii=False))


if __name__=='__main__':
    analyze()
