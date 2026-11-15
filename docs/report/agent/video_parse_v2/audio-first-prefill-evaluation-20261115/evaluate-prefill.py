"""Replay named prefill after audio-only preparation; never call ASR or a server."""

import argparse
from collections import Counter, defaultdict
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import time
from types import SimpleNamespace
import wave

from agent.video_parse_v2.timelineEvent_canvas import ParseContextCanvas
from engine.parsev2.voiceprint_matching import classify_voiceprint_result, score_voiceprint_line
from gateway.local.campplus import CAMPPLUS_MODEL, CampPlusSpeakerEmbedder
from server.service.video_parse import VideoParseRuntimeService

parser = argparse.ArgumentParser()
parser.add_argument('--data-root', type=Path, required=True)
args = parser.parse_args()
root = args.data_root.resolve()
out = root / 'outputs/verification/audio-first-prefill-evaluation-20261115'
private = out / 'private'
baseline = root / 'outputs/verification/ep04-offscreen-flash-low-20261114/private'
audio = json.loads((private / 'audio-first-artifact.json').read_text())
original_raw = (baseline / 'voiceprint-artifact.json').read_bytes()
original = json.loads(original_raw)
rows = sorted((json.loads(path.read_text()) for path in (baseline / 'first-understanding').glob('*.json')),
              key=lambda row: (row['create_time'], row['chunk_task_id']))
assert len(rows) == 24
early_ids = {row['chunk_task_id'] for row in rows[:16]}
order = {row['chunk_task_id']: i for i, row in enumerate(rows)}
parameters = original['parameters']
query_path = private / 'query-cache.json'
query_intervals_sha = hashlib.sha256(json.dumps([
    [s['start_ms'], s['end_ms']] for s in original['samples']
]).encode()).hexdigest()
query_started = time.perf_counter()
native_calls = 0
phase1_hits = 0
expression_hits = 0

if query_path.exists():
    saved = json.loads(query_path.read_text())
    assert saved['identity'] == audio['identity']
    assert saved['query_intervals_sha256'] == query_intervals_sha
    cache = {(s['start_ms'], s['end_ms']): s for s in saved['embeddings']}
    queries = [{**sample, **{key: cache[(sample['start_ms'], sample['end_ms'])][key]
        for key in ('vector', 'pcm_sha256', 'model', 'encoder_revision')}} for sample in original['samples']]
    query_metrics = {**saved['metrics'], 'rerun_cache_hit': True, 'rerun_encoder_calls': 0}
else:
    with wave.open(str(private / 'source-16k.wav'), 'rb') as stream:
        pcm = stream.readframes(stream.getnframes())

    class LocalAudio:
        @contextmanager
        def prepare_speaker_audio(self, identity, *, source_video, start_ms, end_ms, check_active):
            check_active()
            path = private / 'expression-query.wav'
            with wave.open(str(path), 'wb') as stream:
                stream.setnchannels(1)
                stream.setsampwidth(2)
                stream.setframerate(16000)
                stream.writeframes(pcm[start_ms * 32:end_ms * 32])
            try:
                yield path
            finally:
                path.unlink(missing_ok=True)
                path.with_name('b0-window.wav').unlink(missing_ok=True)

    host = object.__new__(VideoParseRuntimeService)
    host._media_service = LocalAudio()
    host._artifact_store = SimpleNamespace(resolve=lambda value: Path(value))
    source = SimpleNamespace(duration_seconds=len(pcm) // 32, original_source_uri=str(private / 'source-16k.wav'))
    lease = SimpleNamespace(video_parse_task_id=original['video_parse_task_id'], lease_version=1)
    encoder = CampPlusSpeakerEmbedder(
        CAMPPLUS_MODEL, checkpoint_path=root / 'outputs/local-models/campplus/campplus_cn_common.bin',
        request_timeout_seconds=120, cpu_threads=8,
    )
    encode = encoder.embed_audio

    def counted(path):
        global native_calls
        native_calls += 1
        return encode(path)

    encoder.embed_audio = counted
    cache = {(s['start_ms'], s['end_ms']): s for s in audio['samples']}
    phase1_bounds = set(cache)
    queries = []
    try:
        for sample in sorted(original['samples'], key=lambda s: (order[s['chunk_task_id']], s['position'])):
            bounds = sample['start_ms'], sample['end_ms']
            cached = cache.get(bounds)
            if cached is None:
                result, digest = host._embed_voiceprint_sample(
                    encoder, source, lease, start_ms=bounds[0], end_ms=bounds[1], check_active=lambda: None,
                )
                cached = {'vector': result.vector, 'pcm_sha256': digest, 'model': result.model,
                          'encoder_revision': result.encoder_revision}
                cache[bounds] = cached
            elif bounds in phase1_bounds:
                phase1_hits += 1
            else:
                expression_hits += 1
            queries.append({**sample, **{key: cached[key] for key in
                ('vector', 'pcm_sha256', 'model', 'encoder_revision')}})
    finally:
        encoder.close()
    query_metrics = {
        'exact_expression_queries': len(queries), 'phase1_exact_interval_hits': phase1_hits,
        'prior_expression_interval_hits': expression_hits, 'native_encoder_calls': native_calls,
        'query_preparation_seconds': time.perf_counter() - query_started,
        'same_pcm_as_original': sum(sample['pcm_sha256'] == next(old['pcm_sha256']
            for old in original['samples'] if old['id'] == sample['id']) for sample in queries),
        'rerun_cache_hit': False,
    }
    embeddings = [{key: sample[key] for key in ('start_ms', 'end_ms', 'vector',
                   'pcm_sha256', 'model', 'encoder_revision')} for sample in queries]
    query_path.write_text(json.dumps({'identity': audio['identity'], 'embeddings': embeddings,
        'query_intervals_sha256': query_intervals_sha, 'metrics': query_metrics}, ensure_ascii=False) + '\n')

query_by_id = {sample['id']: sample for sample in queries}
baseline_by_id = {line['id']: line for line in original['lines']}
window_anchors = {window['id']: [anchor for anchor in queries
    if anchor.get('label') and anchor['start_ms'] <= window['start_ms']
    and window['end_ms'] <= anchor['end_ms']] for window in audio['samples']}


def window_references(query, allowed):
    references = []
    query_cues = set(query['cue_ids'])
    for window in audio['samples']:
        anchors = [anchor for anchor in window_anchors[window['id']]
            if anchor['chunk_task_id'] in allowed and anchor['id'] != query['id']
            and not query_cues.intersection(anchor['cue_ids'])
            and not (query['start_ms'] < anchor['end_ms'] and anchor['start_ms'] < query['end_ms'])]
        labels = {anchor['label'] for anchor in anchors}
        if len(labels) == 1:
            references.append({**window, 'label': next(iter(labels))})
    return references


def score(line, references):
    query = query_by_id.get(line['source_id'], line)
    scored = score_voiceprint_line(query, references, model=query['model'],
        encoder_revision=query['encoder_revision'], **{key: parameters[key] for key in
        ('dimensions', 'trim_fraction', 'min_duration_ms')})
    decision = classify_voiceprint_result(scored, current_label=line['understanding_speaker'],
        song=line['song'], overlap=line['overlap'], **{key: parameters[key] for key in
        ('accept_gap', 'accept_duration_ms', 'accept_min_cues', 'conflict_gap', 'no_voice_duration_ms')})
    return {**line, **scored, **decision}


def top(line):
    return line['candidates'][0]['speaker'] if line['candidates'] else None


sequences = {}
for path in (baseline / 'refine-calls').glob('*.input.json'):
    for ref, seq in json.loads(path.read_text())['member_sequence_nos'].items():
        assert ref not in sequences or sequences[ref] == seq
        sequences[ref] = seq
lines_by_task = defaultdict(list)
for line in original['lines']:
    lines_by_task[line['chunk_task_id']].append(line)
assert len(rows) == len(sequences) == 24


def refine_snapshot(row, lines):
    ref = row['chunk_task_id']
    return {
        'interval_parse_task_id': 'audio-first-replay-' + ref,
        'member_sequence_nos': {ref: sequences[ref]}, 'member_versions': {ref: row['version']},
        'member_bounds': {ref: (0, 1420127)}, 'member_scopes': {ref: [sequences[ref]]},
        'member_contents': {ref: deepcopy(row['content'])},
        'member_evidence': {ref: {'voiceprint': {
            'mode': 'prefill', 'method_version': original['method_version'], 'lines': lines,
        }}},
    }


results = {}
all_names = {row['chunk_task_id'] for row in rows}
for mode, allowed, use_windows in [
    ('exact_expression_full', all_names, False), ('exact_expression_two_thirds', early_ids, False),
    ('audio_window_two_thirds', early_ids, True), ('audio_window_full', all_names, True),
]:
    started = time.perf_counter()
    references = [sample for sample in queries if sample['chunk_task_id'] in allowed]
    scored = []
    reference_counts = []
    for line in original['lines']:
        refs = window_references(query_by_id.get(line['source_id'], line), allowed) if use_windows else references
        scored.append(score(line, refs))
        reference_counts.append(len(refs))
    scored_by_id = {line['id']: line for line in scored}
    actual = {}
    prefilled = 0
    changed_from_understanding = 0
    for row in rows:
        ref = row['chunk_task_id']
        member_lines = [scored_by_id[line['id']] for line in lines_by_task[ref]]
        snapshot = refine_snapshot(row, member_lines)
        before = deepcopy(snapshot)
        canvas = ParseContextCanvas.from_refine(snapshot)
        assert snapshot == before
        draft = canvas.result(ref)
        for line in member_lines:
            event = draft.timeline[line['position']]
            eligible = line['band'] in {'ACCEPT', 'CONFLICT'} and bool(line['candidates'])
            expected = top(line) if eligible else line['understanding_speaker']
            assert event.speaker == expected, (mode, line['id'], expected, event.speaker)
            prefilled += eligible
            changed_from_understanding += event.speaker != line['understanding_speaker']
            actual[line['id']] = {'speaker': event.speaker, 'speaker_source': event.speaker_source}
    assert len(actual) == len(scored) == 309
    targets = {}
    fault_injection = {}
    for name, bounds, truth in [('opening_soyo', (48000, 49800), '長崎そよ'),
                               ('anon_introduction', (292500, 295499), '千早愛音')]:
        line = next(line for line in scored if (line['start_ms'], line['end_ms']) == bounds)
        targets[name] = {'start_ms': bounds[0], 'end_ms': bounds[1], 'expected_speaker': truth,
            'understanding_speaker': line['understanding_speaker'], 'top1': top(line),
            'band': line['band'], 'gap': line['gap'], 'prefill': actual[line['id']],
            'candidates': [{key: value for key, value in c.items() if key != 'reference_ids'}
                           for c in line['candidates']]}
        wrong = '高松燈' if name == 'opening_soyo' else '若葉睦'
        modified = deepcopy(line)
        modified.update(classify_voiceprint_result(line, current_label=wrong,
            song=line['song'], overlap=line['overlap'], **{key: parameters[key] for key in
            ('accept_gap', 'accept_duration_ms', 'accept_min_cues', 'conflict_gap', 'no_voice_duration_ms')}))
        modified['understanding_speaker'] = wrong
        row = next(row for row in rows if row['chunk_task_id'] == line['chunk_task_id'])
        snapshot = refine_snapshot(row, [modified])
        member = snapshot['member_contents'][row['chunk_task_id']]
        event = member['timeline'][line['position']]
        event['speaker'] = wrong
        event['speaker_source'] = 'understanding'
        if wrong not in member['persons']:
            member['persons'].append(wrong)
        if wrong not in event['participants']:
            event['participants'].append(wrong)
        injected = ParseContextCanvas.from_refine(snapshot).result(row['chunk_task_id']).timeline[line['position']]
        fault_injection[name] = {'synthetic_understanding_speaker': wrong, 'band': modified['band'],
            'prefill_speaker': injected.speaker, 'speaker_source': injected.speaker_source,
            'corrected_by_prefill': injected.speaker == truth}
    results[mode] = {
        'queries': len(scored), 'anchor_chunks': len(allowed),
        'anchor_expressions': sum(bool(q.get('label')) and q['chunk_task_id'] in allowed for q in queries),
        'query_dependent_reference_count_min': min(reference_counts),
        'query_dependent_reference_count_max': max(reference_counts),
        'bands': dict(Counter(line['band'] for line in scored)), 'actual_canvas_prefills': prefilled,
        'actual_draft_speaker_changes': changed_from_understanding,
        'top1_changes_vs_saved_baseline': sum(top(line) != top(baseline_by_id[line['id']]) for line in scored),
        'band_changes_vs_saved_baseline': sum(line['band'] != baseline_by_id[line['id']]['band'] for line in scored),
        'targets': targets, 'synthetic_wrong_label_prefill': fault_injection,
        'scoring_and_canvas_seconds': time.perf_counter() - started,
    }
    (private / f'{mode}-lines.json').write_text(json.dumps(scored, ensure_ascii=False) + '\n')
    print(mode, json.dumps({key: value for key, value in results[mode].items() if key != 'targets'}), flush=True)

stage1_targets = {}
for name, bounds in [('opening_soyo', (48000, 49800)), ('anon_introduction', (292500, 295499))]:
    stage1_targets[name] = [turn for turn in audio['diarization']['turns']
                           if turn['start_ms'] < bounds[1] and bounds[0] < turn['end_ms']]
summary = {
    'source_task_id': original['video_parse_task_id'], 'query_preparation': query_metrics,
    'source_artifact_sha256': hashlib.sha256(original_raw).hexdigest(),
    'phase1_coverage': {
        'short_cue_duration_ms': sum(c['end_seconds'] - c['start_seconds']
            for c in audio['prefilled_asr']['transcript'] if c['end_seconds'] - c['start_seconds'] < 400),
        'total_asr_duration_ms': sum(c['end_seconds'] - c['start_seconds']
            for c in audio['prefilled_asr']['transcript']),
        'groups_with_single_window': sum(len(g['sample_ids']) == 1 for g in audio['groups']),
    },
    'phase1_target_turns': stage1_targets, 'results': results,
    'limits': ['Real CPU encoding and actual Canvas construction; saved Understanding commits are replayed.',
               'Anonymous phase 1 never reads Understanding; named phase 2 uses only the indicated anchor horizon.',
               'Query encoding occurs after its Understanding interval exists; exact phase-1 cache hits are counted.',
               'Full-episode song/overlap flags remain fixed: not an online unknown-future-flag validation.',
               'No new Understanding/Refine model call, ASR call, GPU access or DB write.',
               'Initial labels are proxies; only two target identities have human confirmation.',
               'Wrong-label fault injection edits copied drafts only; it is not a newly observed model error.',
               'Stage-1 groups are never assigned human names by fiat or used as B0 identity labels.'],
}
assert (baseline / 'voiceprint-artifact.json').read_bytes() == original_raw
(out / 'evidence/prefill-comparison.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n')
print(json.dumps({'query_preparation': query_metrics, 'targets': {mode: {
    name: {'top1': target['top1'], 'band': target['band'], 'prefill': target['prefill']}
    for name, target in value['targets'].items()} for mode, value in results.items()}}, ensure_ascii=False, indent=2))
