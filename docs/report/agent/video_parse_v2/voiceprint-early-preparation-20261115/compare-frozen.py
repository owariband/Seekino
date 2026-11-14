import argparse
from collections import Counter
from pathlib import Path
import hashlib
import json
import time

from engine.parsev2.voiceprint_matching import classify_voiceprint_result, score_voiceprint_line

parser = argparse.ArgumentParser()
parser.add_argument('--data-root', type=Path, required=True, help='Shared workspace containing the private outputs/verification data.')
root = parser.parse_args().data_root.resolve()
baseline = root / 'outputs/verification/ep04-offscreen-flash-low-20261114/private'
out = root / 'outputs/verification/voiceprint-early-preparation-20261115'
raw = (baseline / 'voiceprint-artifact.json').read_bytes()
artifact = json.loads(raw)
rows = sorted((json.loads(p.read_text()) for p in (baseline / 'first-understanding').glob('*.json')),
              key=lambda row: (row['create_time'], row['chunk_task_id']))
assert len(rows) == 24
early_ids = {row['chunk_task_id'] for row in rows[:16]}
all_samples = artifact['samples']
early_samples = [sample for sample in all_samples if sample['chunk_task_id'] in early_ids]
samples = {sample['id']: sample for sample in all_samples}
parameters = artifact['parameters']

def score(line, references):
    query = samples.get(line['source_id'], line)
    result = score_voiceprint_line(
        query, references, model=query['model'], encoder_revision=query['encoder_revision'],
        **{key: parameters[key] for key in ('dimensions', 'trim_fraction', 'min_duration_ms')},
    )
    decision = classify_voiceprint_result(
        result, current_label=line['understanding_speaker'], song=line['song'], overlap=line['overlap'],
        **{key: parameters[key] for key in ('accept_gap', 'accept_duration_ms', 'accept_min_cues',
                                           'conflict_gap', 'no_voice_duration_ms')},
    )
    return {**result, **decision}

def top(result):
    return result['candidates'][0]['speaker'] if result['candidates'] else None

started = time.perf_counter()
full = [score(line, all_samples) for line in artifact['lines']]
full_seconds = time.perf_counter() - started
for line, result in zip(artifact['lines'], full):
    for key in ('candidates', 'gap', 'band', 'conflict', 'reason'):
        assert result[key] == line[key], (line['id'], key)
started = time.perf_counter()
early = [score(line, early_samples) for line in artifact['lines']]
early_seconds = time.perf_counter() - started
changes = []
for line, before, after in zip(artifact['lines'], full, early):
    if top(before) != top(after) or before['band'] != after['band']:
        changes.append({'source_id': line['source_id'], 'start_ms': line['start_ms'],
            'end_ms': line['end_ms'], 'understanding_speaker': line['understanding_speaker'],
            'full': {'top1': top(before), 'gap': before['gap'], 'band': before['band']},
            'two_thirds': {'top1': top(after), 'gap': after['gap'], 'band': after['band']}})
targets = {}
for name, bounds, expected in [('opening_soyo', (48000, 49800), '長崎そよ'),
                              ('anon_introduction', (292500, 295499), '千早愛音')]:
    index = next(i for i, line in enumerate(artifact['lines'])
                 if (line['start_ms'], line['end_ms']) == bounds)
    targets[name] = {'start_ms': bounds[0], 'end_ms': bounds[1], 'expected_speaker': expected,
                    'full': full[index], 'two_thirds': early[index]}

asr = json.loads((out / 'private/asr.json').read_text())
words = [word for cue in asr['transcript'] for word in cue.get('words', [])]
cue_map = {}
for cue in asr['transcript']:
    identity = [cue['start_seconds'], cue['end_seconds'], cue['text']]
    cue_id = 'asr:' + hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()
    cue_map[cue_id] = cue
groups = {}
multi_id = 0
unmatched = 0
exact_intervals = {(cue['start_seconds'], cue['end_seconds']) for cue in asr['transcript']}
cue_examples = {}
for sample in all_samples:
    cues = [cue_map[cue] for cue in sample['cue_ids'] if cue in cue_map]
    if not cues:
        unmatched += 1
    ids = {cue['speaker_id'] for cue in cues if cue.get('speaker_id')}
    multi_id += len(ids) > 1
    for speaker in ids:
        group = groups.setdefault(speaker, {'sample_memberships': 0, 'understanding_labels': Counter()})
        group['sample_memberships'] += 1
        group['understanding_labels'][sample.get('label')] += 1
    if (sample['start_ms'], sample['end_ms']) in {(48000, 49800), (292500, 295499)}:
        cue_examples[str(sample['start_ms'])] = [{key: cue.get(key) for key in
            ('start_seconds', 'end_seconds', 'speaker_id', 'text')} for cue in cues]

summary = {
    'source_task_id': artifact['video_parse_task_id'], 'source_artifact_sha256': hashlib.sha256(raw).hexdigest(),
    'queries': len(full), 'full_reference_samples': len(all_samples),
    'early_reference_chunks': 16, 'early_reference_samples': len(early_samples),
    'full_named_reference_samples': sum(bool(s.get('label')) for s in all_samples),
    'early_named_reference_samples': sum(bool(s.get('label')) for s in early_samples),
    'early_ready_at_ms': rows[15]['create_time'], 'all_ready_at_ms': rows[-1]['create_time'],
    'gate_difference_ms': rows[-1]['create_time'] - rows[15]['create_time'],
    'measured_original_preparation_ms': artifact['timing']['preparation_elapsed_ms'],
    'full_rescore_seconds': full_seconds, 'two_thirds_rescore_seconds': early_seconds,
    'full_reproduction': {'lines_matched': len(full), 'candidates_gap_band_reason_exact': True},
    'full_reference_names': sorted({sample['label'] for sample in all_samples if sample.get('label')}),
    'early_reference_names': sorted({sample['label'] for sample in early_samples if sample.get('label')}),
    'missing_reference_names': sorted({s['label'] for s in all_samples if s.get('label')}
                                    - {s['label'] for s in early_samples if s.get('label')}),
    'top1_changed_count': sum(top(a) != top(b) for a, b in zip(full, early)),
    'band_changed_count': sum(a['band'] != b['band'] for a, b in zip(full, early)),
    'full_bands': dict(Counter(result['band'] for result in full)),
    'two_thirds_bands': dict(Counter(result['band'] for result in early)),
    'targets': {name: {key: value for key, value in target.items() if key not in {'full', 'two_thirds'}}
                        | {mode: {'top1': top(target[mode]), 'gap': target[mode]['gap'],
                                  'band': target[mode]['band'], 'candidate_count': target[mode]['candidate_count'],
                                  'candidates': [{k: v for k, v in c.items() if k != 'reference_ids'}
                                                 for c in target[mode]['candidates']]}
                           for mode in ('full', 'two_thirds')}
                for name, target in targets.items()},
    'asr': {'cue_count': len(asr['transcript']), 'word_count': len(words),
            'word_keys': sorted({key for word in words for key in word}),
            'speaker_id_counts': dict(Counter(
                str(cue.get('speaker_id')) for cue in asr['transcript'])),
            'embedded_dialogues_matching_exact_asr_bounds': sum(
                (s['start_ms'], s['end_ms']) in exact_intervals for s in all_samples),
            'embedded_dialogues_with_multiple_overlapping_speaker_ids': multi_id,
            'samples_without_matched_asr_cues': unmatched,
            'id_memberships_and_initial_label_proxy': {key: {**value,
                'understanding_labels': dict(value['understanding_labels'])} for key, value in groups.items()},
            'target_cues': cue_examples},
    'limits': ['Reference-only counterfactual holds full-episode interval/song/overlap flags fixed.',
               'Initial Understanding labels are proxies, not whole-episode identity truth.',
               'Cached-vector scoring is not early-stage encoding time or end-to-end acceleration.',
               'ASR cue vectors cannot be assumed equivalent to different Understanding intervals.',
               'No new generative model call or business-state write.'],
}
assert (baseline / 'voiceprint-artifact.json').read_bytes() == raw
(out / 'evidence/frozen-comparison.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n')
(out / 'private/changed-lines.json').write_text(json.dumps(changes, ensure_ascii=False, indent=2) + '\n')
print(json.dumps({key: value for key, value in summary.items() if key != 'asr'}, ensure_ascii=False, indent=2))
print('ASR summary', json.dumps({key: value for key, value in summary['asr'].items()
                               if key not in {'id_memberships_and_initial_label_proxy', 'target_cues'}}, ensure_ascii=False))
