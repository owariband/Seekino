"""Offline checks for the saved experiment. No model calls or database writes."""
import hashlib
import json
from pathlib import Path

from experiment import DURATION_MS, DirectRefineResult, VideoParseResult, validate_result_time_range

HERE = Path(__file__).resolve().parent


def read(path):
    return json.loads(path.read_text())


checks = {}
arms = ('understanding-control', 'understanding-candidate', 'refine-canvas', 'refine-json')
for arm in arms:
    summary = read(HERE / arm / 'summary.json')
    assert summary['status'] == 'completed'
    raw = read(HERE / arm / 'result.json')
    if arm.startswith('refine'):
        result = DirectRefineResult.model_validate(raw)
        assert sorted(m.sequence_no for m in result.members) == [0, 1, 2]
        members = [VideoParseResult(persons=m.persons, timeline=m.timeline) for m in result.members]
    else:
        members = [VideoParseResult.model_validate(raw)]
    for member in members:
        validate_result_time_range(member, start_seconds=0, end_seconds=DURATION_MS)
    checks[arm + '_schema_source_range_members'] = True

started = {arm: read(HERE / arm / 'started.json') for arm in arms}
assert all(s['binding'] == started[arms[0]]['binding'] for s in started.values())
checks['model_bindings_equal'] = True
data = read(HERE / 'refine-input.json')
digest = hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
assert started['refine-canvas']['refine_input_sha256'] == started['refine-json']['refine_input_sha256'] == digest
checks['refine_input_hash_equal'] = True

source_id = started['understanding-control']['source_call_id']
source_path = HERE.parent / 'model-trace' / source_id / 'events.jsonl'
candidate_path = next((HERE / 'understanding-candidate' / 'model-trace').glob('*/events.jsonl'))
def request(path):
    with path.open() as f:
        return json.loads(f.readline())['payload']['request']
def image_refs(req):
    return [(c['image_url']['url']['sha256'], c['image_url'].get('detail'))
            for m in req['messages'] for c in m.get('content', [])
            if isinstance(c, dict) and c.get('type') == 'image_url']
old, new = request(source_path), request(candidate_path)
assert len(image_refs(old)) == 189 and image_refs(old) == image_refs(new)
checks['understanding_189_images_order_detail_equal'] = True
old_labels = [c['text'] for m in old['messages'] for c in m['content'] if c.get('type') == 'text' and len(c['text']) < 100]
new_labels = [c['text'] for m in new['messages'] for c in m['content'] if c.get('type') == 'text' and len(c['text']) < 100]
assert len(old_labels) == 189 and old_labels == new_labels
checks['understanding_frame_labels_equal'] = True
assert 'whisper' not in (HERE / 'understanding-candidate.txt').read_text().lower()
checks['candidate_omits_asr_implementation_name'] = True

analysis = read(HERE / 'analysis.json')
assert analysis['understanding-candidate']['speech_diff']['changes'] == []
checks['candidate_preserves_all_197_asr_words'] = True
for arm in ('refine-canvas', 'refine-json'):
    a = analysis[arm]
    assert sum(x['unchanged_except_person_labels'] for x in a['visual_diff'].values()) == 29
    assert all(not x['removed_or_changed'] and not x['added_or_changed'] for x in a['visual_diff'].values())
    assert sum(a['event_counts'].values()) == 45
    assert [(e['start_seconds'], e['end_seconds']) for e in a['boundary_dialogues']] == [(50400, 61180), (113520, 121660), (178450, 181440)]
    all_text = ' '.join(v['text'] for m in read(HERE / arm / 'result.json')['members'] for e in m['timeline'] if e['event_type'] == 'DIALOGUE' for v in e['text_variants'] if v['role'] == 'ORIGINAL').lower()
    assert all_text.count('now, this is just one example') == 1
    assert all_text.count('completes the sequence with the outcome') == 1
    checks[arm + '_29_visual_events_and_boundary_repairs'] = True
after = read(HERE / 'environment-after.json')
assert after['paused'] and after['asr_new_calls'] == 0
assert not any(after['active_leases'].values())
assert all(c['unchanged'] for c in after['source_content_checks'])
checks['task_asr_source_content_unchanged'] = True
(HERE / 'validation.json').write_text(json.dumps({'checks': checks, 'scope': 'Offline schema/input/result checks; not human audio accuracy or formal API acceptance.'}, ensure_ascii=False, indent=2) + '\n')
print(json.dumps(checks, ensure_ascii=False))
