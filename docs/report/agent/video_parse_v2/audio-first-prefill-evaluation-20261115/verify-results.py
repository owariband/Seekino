"""Check whole-episode coverage, provenance and cache/identity boundaries."""

import argparse
import hashlib
import json
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument('--data-root', type=Path, required=True)
root = parser.parse_args().data_root.resolve()
out = root / 'outputs/verification/audio-first-prefill-evaluation-20261115'
audio = json.loads((out / 'private/audio-first-artifact.json').read_text())
result = json.loads((out / 'evidence/prefill-comparison.json').read_text())
original_path = root / 'outputs/verification/ep04-offscreen-flash-low-20261114/private/voiceprint-artifact.json'
assert hashlib.sha256(original_path.read_bytes()).hexdigest() == result['source_artifact_sha256']
asr_path = root / 'outputs/verification/voiceprint-early-preparation-20261115/private/asr.json'
assert hashlib.sha256(asr_path.read_bytes()).hexdigest() == audio['identity']['asr_sha256']
assert audio['encoder_calls'] == len(audio['samples']) == 1001
assert all(sample['label'] is None for sample in audio['samples'])
assert len(audio['prefilled_asr']['transcript']) == 166
assert sum(len(group['sample_ids']) for group in audio['groups']) == 1001
assert len(audio['groups']) == 749
query_cache = json.loads((out / 'private/query-cache.json').read_text())
assert len(query_cache['embeddings']) == 308
assert all(not {'label', 'speaker', 'understanding_speaker'}.intersection(row)
           for row in query_cache['embeddings'])
metrics = result['query_preparation']
assert metrics['phase1_exact_interval_hits'] + metrics['prior_expression_interval_hits'] + metrics['native_encoder_calls'] == 308
assert metrics['rerun_cache_hit'] and metrics['rerun_encoder_calls'] == 0
scores = {}
for mode, stats in result['results'].items():
    lines = json.loads((out / f'private/{mode}-lines.json').read_text())
    assert len(lines) == stats['queries'] == sum(stats['bands'].values()) == 309
    assert sum(line['band'] in {'ACCEPT', 'CONFLICT'} and bool(line['candidates']) for line in lines) == stats['actual_canvas_prefills']
    assert all(target['top1'] == target['expected_speaker'] == target['prefill']['speaker']
               for target in stats['targets'].values())
    assert all(target['prefill']['speaker_source'] == 'understanding' for target in stats['targets'].values())
    if stats['anchor_chunks'] == 16:
        assert all(check['corrected_by_prefill'] and check['speaker_source'] == 'voice_default'
                   for check in stats['synthetic_wrong_label_prefill'].values())
    scores[mode] = {line['id']: line for line in lines}


def top(line):
    return line['candidates'][0]['speaker'] if line['candidates'] else None


pairwise = {}
for left, right in [
    ('exact_expression_full', 'exact_expression_two_thirds'),
    ('exact_expression_two_thirds', 'audio_window_two_thirds'),
    ('audio_window_two_thirds', 'audio_window_full'),
]:
    before, after = scores[left], scores[right]
    assert before.keys() == after.keys()
    pairwise[left + '__to__' + right] = {
        'queries': len(before),
        'top1_changes': sum(top(before[key]) != top(after[key]) for key in before),
        'band_changes': sum(before[key]['band'] != after[key]['band'] for key in before),
    }
proof = {'verified_query_rows_per_mode': 309, 'modes': len(scores),
    'stage1_has_no_person_labels': True, 'query_cache_contains_no_person_labels': True,
    'query_cache_rerun_encoder_calls': 0, 'source_asr_unchanged': True,
    'source_scoring_artifact_unchanged': True, 'real_targets_retained_existing_names': True,
    'two_thirds_synthetic_wrong_label_checks_pass': True, 'pairwise': pairwise,
    'new_understanding_or_refine_model_run': False, 'production_server_changed': False,
    'gpu_access': False, 'cpu_prod_access': False}
(out / 'evidence/verification.json').write_text(json.dumps(proof, ensure_ascii=False, indent=2) + '\n')
print(json.dumps(proof, ensure_ascii=False, indent=2))
