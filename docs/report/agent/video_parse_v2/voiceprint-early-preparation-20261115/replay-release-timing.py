import argparse
from datetime import datetime, timezone
from pathlib import Path
import json

parser = argparse.ArgumentParser()
parser.add_argument('--data-root', type=Path, required=True, help='Shared workspace containing the private outputs/verification data.')
root = parser.parse_args().data_root.resolve()
baseline = root / 'outputs/verification/ep04-offscreen-flash-low-20261114/private'
out = root / 'outputs/verification/voiceprint-early-preparation-20261115'
artifact = json.loads((baseline / 'voiceprint-artifact.json').read_text())
sequences = {}
for path in (baseline / 'refine-calls').glob('*.input.json'):
    capture = json.loads(path.read_text())
    for ref, seq in capture['member_sequence_nos'].items():
        assert ref not in sequences or sequences[ref] == seq
        sequences[ref] = seq
rows = [json.loads(p.read_text()) for p in (baseline / 'first-understanding').glob('*.json')]
ready = {sequences[row['chunk_task_id']]: row['create_time'] for row in rows}
assert set(ready) == set(range(24))
ordered = sorted(ready.values())
timing = artifact['timing']
full_ready = timing['preparation_started_at_ms'] + timing['preparation_elapsed_ms']
partial_proxy = ordered[15] + timing['preparation_elapsed_ms']

def epoch(value):
    if value is None:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return round(parsed.timestamp() * 1000)

intervals = json.loads((baseline / 'final-intervals.json').read_text())
results = []
for interval in intervals:
    start = interval['start_sequence_no']
    count = interval['chunk_count']
    member_ready = max(ready[seq] for seq in range(start, start + count))
    proxy_release = max(partial_proxy, member_ready)
    results.append({'start_sequence_no': start, 'chunk_count': count,
        'member_understanding_ready_at_ms': member_ready,
        'actual_last_attempt_start_at_ms': epoch(interval['start_time']),
        'actual_completed_at_ms': epoch(interval['finish_time']),
        'optimistic_partial_pool_release_at_ms': proxy_release,
        'optimistic_release_advance_ms': full_ready - proxy_release})
results.sort(key=lambda value: value['start_sequence_no'])
latest = max(results, key=lambda value: value['actual_completed_at_ms'])
summary = {'understanding_ready_by_sequence_ms': ready,
    'full_score_ready_proxy_at_ms': full_ready,
    'two_thirds_reference_ready_proxy_at_ms': partial_proxy,
    'reference_preparation_proxy_cost_ms': timing['preparation_elapsed_ms'],
    'previous_75_percent_gate_at_ms': ordered[17],
    'two_thirds_to_previous_75_percent_ms': ordered[17] - ordered[15],
    'minimum_optimistic_release_advance_ms': min(row['optimistic_release_advance_ms'] for row in results),
    'maximum_optimistic_release_advance_ms': max(row['optimistic_release_advance_ms'] for row in results),
    'latest_completing_actual_window': latest, 'windows': results,
    'limits': ['Counterfactual release times only; NOT a simulated or measured end-to-end speedup.',
               'Assumes early pool costs the same 39.462 seconds as the old full preparation.',
               'Assumes later query embeddings are instantly available and ignores source-flag changes, reference coverage, resource contention, retries and changed model results.',
               'Actual start_time is the final attempt and can omit earlier retries.']}
(out / 'evidence/release-timing.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n')
print(json.dumps({key: value for key, value in summary.items()
                  if key not in {'windows', 'understanding_ready_by_sequence_ms'}}, ensure_ascii=False, indent=2))
