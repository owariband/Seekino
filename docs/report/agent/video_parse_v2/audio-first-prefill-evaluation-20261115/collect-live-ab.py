"""Collect actual live A/B calls, stage results and the two known target windows."""

import argparse
from collections import Counter
from datetime import datetime
import hashlib
import json
from pathlib import Path


TARGETS = {'opening_soyo': (48000, 49800, '長崎そよ'),
           'anon_introduction': (292500, 295499, '千早愛音')}


def load(path):
    return json.loads(path.read_text())


def near(content):
    return {name: [{key: event.get(key) for key in (
        'start_seconds', 'end_seconds', 'speaker', 'speaker_source', 'description', 'text_variants')}
        for event in content.get('timeline', []) if event.get('event_type') == 'DIALOGUE'
        and event['start_seconds'] < end and start < event['end_seconds']]
        for name, (start, end, _) in TARGETS.items()}


def merge(items):
    return {name: [event for item in items for event in item[name]] for name in TARGETS}


parser = argparse.ArgumentParser()
parser.add_argument('--data-root', type=Path, required=True)
parser.add_argument('--b-run-name', default='audio-first-prefill-live-ab-20261115')
args = parser.parse_args()
root = args.data_root.resolve()
run = root / 'outputs/verification/audio-first-prefill-live-ab-20261115'
private = run / 'private'
manifest = load(private / 'manifest.json')
result = {'manifest': manifest, 'cases': {},
          'expected_targets': {name: {'start_ms': start, 'end_ms': end, 'speaker': truth}
                               for name, (start, end, truth) in TARGETS.items()},
          'limits': ['One live run per variant; no identity-error probability estimate.',
                     'Only two target identities are human-confirmed; no full-episode accuracy claim.',
                     'Real Agent workflows and configured model calls; file-backed experimental host, no Server/DB/indexing verification.',
                     'Shared model concurrency and file-host member locking differ from independent Server task timing.',
                     'Host argument-order and scope errors required recovery. All48 Understanding answers and the original16-chunk horizon were retained; final Refine was restarted. No early-release latency claim.',
                     'Call counts describe instrumented generation entries; provider tokens/fees were not collected.']}
under_inputs = {}
for name in ('A_audio_only', 'B_two_thirds'):
    case_run = run if name == 'A_audio_only' else root / 'outputs/verification' / args.b_run_name
    directory = case_run / 'private' / name
    case_manifest = load(case_run / 'private/manifest.json')
    assert all(case_manifest[key] == manifest[key] for key in
               ('source_sha256', 'asr_sha256', 'audio_artifact_sha256', 'parse_config', 'model_config_sha256'))
    understanding = [load(path) for path in sorted((directory / 'understanding').glob('*.json'))]
    final_path = directory / 'final-contents.json'
    final = load(final_path) if final_path.exists() else {}
    record = {'run_name': case_run.name, 'progress': load(directory / 'progress.json'),
              'initial_understanding_targets': merge([near(row['content']) for row in understanding]),
              'final_targets': merge([near(row['content']) for row in final.values()]),
              'calls_by_schema': {}, 'refine_target_inputs': [], 'voice_targets': []}
    calls = []
    under_inputs[name] = Counter()
    for path in sorted((directory / 'calls').glob('*.input.json')):
        call = load(path)
        outcome_path = path.with_name(path.name.replace('.input.json', '.outcome.json'))
        outcome = load(outcome_path) if outcome_path.exists() else {}
        calls.append({'call_id': call['call_id'], 'schema': call['schema_name'],
                      'status': outcome.get('status', 'running'),
                      'elapsed_seconds': outcome.get('elapsed_seconds')})
        assert call['model'] == 'deepseek-flash' and call['reasoning_effort'] == 'low'
        texts = [item['text'] for item in call['inputs'] if item['type'] == 'TextInput']
        if call['schema_name'] == 'video_parse_v2_chunk':
            identity = [{'type': item['type'], **({'text': item['text']} if item['type'] == 'TextInput'
                        else {'sha256': item['sha256']})} for item in call['inputs']]
            under_inputs[name][hashlib.sha256(json.dumps(identity, ensure_ascii=False,
                                                        sort_keys=True).encode()).hexdigest()] += 1
        if call['schema_name'] != 'IntervalRefinementResult':
            continue
        assert all(key not in texts[-1] for key in ('"vector":', '"reference_ids":', '"samples":'))
        body, _ = json.JSONDecoder().raw_decode(texts[-1].split('Input: ', 1)[1])
        members = []
        for member in body['members']:
            targets = near(member['content'])
            if any(targets.values()):
                members.append({'sequence_no': member['sequence_no'], 'targets': targets})
        if members:
            record['refine_target_inputs'].append({'call_id': call['call_id'],
                'status': outcome.get('status', 'running'), 'members': members,
                'output_targets': merge([near(member) for member in
                                        (outcome.get('result') or {}).get('members', [])])})
    record['calls_by_schema'] = dict(Counter(call['schema'] for call in calls))
    record['call_statuses'] = dict(Counter(call['status'] for call in calls))
    record['actual_calls'] = calls
    refine_starts = [load(path)['started_utc'] for path in (directory / 'calls').glob('*.input.json')
                     if load(path)['schema_name'] == 'IntervalRefinementResult']
    if refine_starts:
        first_refine = min(datetime.fromisoformat(value) for value in refine_starts)
        record['understanding_committed_at_first_refine'] = sum(
            datetime.fromisoformat(row['committed_utc']) <= first_refine for row in understanding)
        last_understanding = max(datetime.fromisoformat(row['committed_utc']) for row in understanding)
        record['first_refine_started_utc'] = first_refine.isoformat()
        record['last_understanding_committed_utc'] = last_understanding.isoformat()
        record['refine_start_before_last_understanding_seconds'] = (last_understanding - first_refine).total_seconds()
    if (directory / 'named-timing.json').exists():
        record['named_timing'] = load(directory / 'named-timing.json')
    archive = directory / 'attempt-before-scope-fix'
    record['archived_refine_attempt_entries'] = len(list(archive.glob('*.input.json')))
    record['recovery'] = load(directory / 'recovery.json') if (directory / 'recovery.json').exists() else None
    frozen_path = directory / 'frozen-sixteen.json'
    if frozen_path.exists():
        frozen = load(frozen_path)
        assert len(frozen['chunk_refs']) == len(set(frozen['chunk_refs'])) == 16
        actual_order = sorted(understanding, key=lambda row: row['commit_order'])
        assert frozen['chunk_refs'] == [row['chunk_task_id'] for row in actual_order[:16]]
        assert all(frozen['content_sha256'][row['chunk_task_id']] == hashlib.sha256(json.dumps(
            row['content'], ensure_ascii=False, sort_keys=True).encode()).hexdigest() for row in actual_order[:16])
        record['frozen_sixteen_receipt_verified'] = True
        record['frozen_sixteen_sequence_nos'] = frozen['sequence_nos']
        anchors = load(directory / 'frozen-anchors.json')
        assert all(anchor['chunk_task_id'] in frozen['chunk_refs'] for anchor in anchors)
        record['frozen_anchor_expressions'] = len(anchors)
        record['frozen_named_anchor_expressions'] = sum(bool(anchor.get('label')) for anchor in anchors)
    lines_path = directory / 'named-lines.json'
    if lines_path.exists():
        named = load(lines_path)
        record['named_line_count'] = sum(len(lines) for lines in named.values())
        record['named_bands'] = dict(Counter(str(line['band']) for lines in named.values() for line in lines))
        for ref, lines in named.items():
            for line in lines:
                for target, (start, end, _) in TARGETS.items():
                    if line['start_ms'] < end and start < line['end_ms']:
                        record['voice_targets'].append({'target': target,
                            **{key: line.get(key) for key in ('start_ms', 'end_ms', 'understanding_speaker', 'band', 'gap')},
                            'candidates': [{key: candidate[key] for key in ('speaker', 'similarity')}
                                           for candidate in line['candidates']]})
    complete_path = directory / 'complete.json'
    record['complete'] = complete_path.exists()
    receipts = [load(path) for path in (directory / 'intervals').glob('*.json')]
    record['committed_interval_receipts'] = len(receipts)
    if complete_path.exists():
        record['completion'] = load(complete_path)
        assert len(understanding) == len(final) == 24
        assert record['progress']['refine_complete'] == 22
        assert sorted(receipt['start_sequence_no'] for receipt in receipts) == list(range(22))
    result['cases'][name] = record

result['initial_understanding_request_multisets_equal'] = under_inputs['A_audio_only'] == under_inputs['B_two_thirds']
result['primary_case_selection'] = load(run / 'evidence/primary-case-selection.json')
asr = root / 'outputs/verification/voiceprint-early-preparation-20261115/private/asr.json'
result['original_asr_unchanged'] = hashlib.sha256(asr.read_bytes()).hexdigest() == manifest['asr_sha256']
assert result['original_asr_unchanged']
(run / 'evidence').mkdir(exist_ok=True)
(run / 'evidence/comparison.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
print(json.dumps({'cases': {name: {key: record[key] for key in (
    'progress', 'initial_understanding_targets', 'final_targets', 'voice_targets', 'complete')}
    for name, record in result['cases'].items()},
    'initial_understanding_request_multisets_equal': result['initial_understanding_request_multisets_equal'],
    'original_asr_unchanged': result['original_asr_unchanged']}, ensure_ascii=False, indent=2))
