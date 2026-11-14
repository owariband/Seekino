import argparse
from collections import Counter, defaultdict
from copy import deepcopy
from pathlib import Path
import hashlib
import json
import time

from engine.parsev2.voiceprint_matching import score_clusters

parser = argparse.ArgumentParser()
parser.add_argument('--data-root', type=Path, required=True, help='Shared workspace containing the private outputs/verification data.')
root = parser.parse_args().data_root.resolve()
baseline = root / 'outputs/verification/ep04-offscreen-flash-low-20261114/private'
out = root / 'outputs/verification/voiceprint-early-preparation-20261115'
artifact = json.loads((baseline / 'voiceprint-artifact.json').read_text())
asr = json.loads((out / 'private/asr.json').read_text())
rows = sorted((json.loads(p.read_text()) for p in (baseline / 'first-understanding').glob('*.json')),
              key=lambda r: (r['create_time'], r['chunk_task_id']))
early_ids = {r['chunk_task_id'] for r in rows[:16]}
cues = {}
for cue in asr['transcript']:
    identity = [cue['start_seconds'], cue['end_seconds'], cue['text']]
    key = 'asr:' + hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()
    cues[key] = cue
members = defaultdict(list)
unassigned = []
for sample in artifact['samples']:
    weights = Counter()
    for cue_id in sample['cue_ids']:
        cue = cues.get(cue_id)
        if cue and cue.get('speaker_id'):
            overlap = min(sample['end_ms'], cue['end_seconds']) - max(sample['start_ms'], cue['start_seconds'])
            weights[cue['speaker_id']] += max(0, overlap)
    ranked = weights.most_common()
    if not ranked or ranked[0][1] <= 0 or len(ranked) > 1 and ranked[0][1] == ranked[1][1]:
        unassigned.append(sample['id'])
    else:
        members[ranked[0][0]].append(sample['id'])
groups = [{'cluster_id': key, 'sample_ids': sorted(ids)} for key, ids in sorted(members.items())]
anchors = deepcopy(artifact['samples'])
for sample in anchors:
    if sample['chunk_task_id'] not in early_ids:
        sample['label'] = None
started = time.perf_counter()
results = {}
for name, references in [('full_understanding_names', artifact['samples']), ('two_thirds_understanding_names', anchors)]:
    values = []
    for query in artifact['samples']:
        value = score_clusters(query, references, groups, min_cues=3, min_purity=0.6)
        values.append((query, value))
    named = [value['candidates'][0]['speaker'] for _, value in values if value['candidates']]
    targets = {}
    for key, bounds in [('opening_soyo', (48000, 49800)), ('anon_introduction', (292500, 295499))]:
        query, value = next((query, result) for query, result in values
                            if (query['start_ms'], query['end_ms']) == bounds)
        targets[key] = {'top1': value['candidates'][0]['speaker'] if value['candidates'] else None,
            'gap': value['gap'], 'named_groups': [{k: v for k, v in cluster.items() if k != 'sample_ids'}
                                                 for cluster in value['clusters']],
            'candidates': [{k: v for k, v in candidate.items() if k != 'reference_ids'}
                           for candidate in value['candidates']]}
    results[name] = {'queries': len(values), 'queries_with_named_candidate': len(named),
                     'top1_name_counts': dict(Counter(named)), 'targets': targets}

summary = {'source_task_id': artifact['video_parse_task_id'],
    'method': 'Fixed ASR dominant-overlap ID groups; existing score_clusters names by independent cue votes after target exclusion.',
    'parameters': {'minimum_independent_cues': 3, 'minimum_initial_label_vote_purity': 0.6},
    'group_member_counts': {group['cluster_id']: len(group['sample_ids']) for group in groups},
    'unassigned_samples': len(unassigned), 'evaluation_seconds': time.perf_counter() - started,
    'results': results,
    'limits': ['Uses cached Understanding-interval vectors, not raw ASR-cue vectors: not an ASR-stage implementation.',
               'Only reference names available at the indicated Understanding horizon are used; unknown future names remain null.',
               'Naming purity is relative to initial model labels, not acoustic identity truth.',
               'The short unembedded 309th line is not scored. No new generative call or DB write.',
               'This tests one hard-ID grouping method and does not reject acoustic clustering independent of WhisperX IDs.']}
(out / 'evidence/asr-id-grouping.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n')
print(json.dumps({'group_member_counts': summary['group_member_counts'], 'unassigned_samples': len(unassigned),
    'evaluation_seconds': summary['evaluation_seconds'], 'results': {key: {
        'queries_with_named_candidate': value['queries_with_named_candidate'],
        'top1_name_counts': value['top1_name_counts'],
        'target_top1': {name: target['top1'] for name, target in value['targets'].items()}}
        for key, value in results.items()}}, ensure_ascii=False, indent=2))
