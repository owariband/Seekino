"""Reuse B0 with non-null ASR IDs as references; score every original cue."""

import argparse
from collections import Counter
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
import time
from types import SimpleNamespace
from uuid import uuid4
import wave

from gateway.local.campplus import (
    CAMPPLUS_ENCODER_REVISION, CAMPPLUS_MODEL, CampPlusSpeakerEmbedder,
)
from server.service.video_parse import VideoParseRuntimeService


parser = argparse.ArgumentParser()
parser.add_argument('--data-root', type=Path, required=True)
parser.add_argument('--source', type=Path, required=True)
parser.add_argument('--run-name', default='audio-first-prefill-asr-id-b0-20261115')
args = parser.parse_args()
root = args.data_root.resolve()
source = args.source.resolve()
assert Path(args.run_name).name == args.run_name
out = root / 'outputs/verification' / args.run_name
private = out / 'private'
private.mkdir(parents=True, exist_ok=True)
(out / 'evidence').mkdir(exist_ok=True)
asr_path = root / 'outputs/verification/voiceprint-early-preparation-20261115/private/asr.json'
asr_raw = asr_path.read_bytes()
asr = json.loads(asr_raw)
with source.open('rb') as stream:
    media_sha = hashlib.file_digest(stream, 'sha256').hexdigest()
parameters = VideoParseRuntimeService._voiceprint_parameters()
identity = {
    'method_version': 'asr-id-b0-v1',
    'media_sha256': media_sha, 'asr_sha256': hashlib.sha256(asr_raw).hexdigest(),
    'model': CAMPPLUS_MODEL, 'encoder_revision': CAMPPLUS_ENCODER_REVISION,
    'parameters': parameters, 'cpu_threads': 8,
}
artifact_path = private / 'audio-first-artifact.json'
wav_path = private / 'source-16k.wav'
if artifact_path.exists():
    existing = json.loads(artifact_path.read_text())
    assert existing['identity'] == identity, 'Audio cache provenance differs'
    assert hashlib.sha256(wav_path.read_bytes()).hexdigest() == existing['pcm_file_sha256']
    print(json.dumps({'cache_hit': True, 'encoder_calls': 0,
                      'cached_queries': len(existing['queries']), 'identity': identity}))
    raise SystemExit(0)

started = time.perf_counter()
subprocess.run([
    'ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-y', '-threads', '8',
    '-i', str(source), '-vn', '-ac', '1', '-ar', '16000', '-c:a', 'pcm_s16le', str(wav_path),
], check=True)
with wave.open(str(wav_path), 'rb') as audio:
    assert (audio.getnchannels(), audio.getsampwidth(), audio.getframerate()) == (1, 2, 16000)
    pcm = audio.readframes(audio.getnframes())
duration_ms = len(pcm) // 32
decode_seconds = time.perf_counter() - started


class LocalAudio:
    @contextmanager
    def prepare_speaker_audio(self, identity, *, source_video, start_ms, end_ms, check_active):
        check_active()
        path = private / 'encoding-cue.wav'
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
source_input = SimpleNamespace(duration_seconds=duration_ms, original_source_uri=str(wav_path))
lease = SimpleNamespace(video_parse_task_id=str(uuid4()), lease_version=1)
encoder = CampPlusSpeakerEmbedder(
    CAMPPLUS_MODEL, checkpoint_path=root / 'outputs/local-models/campplus/campplus_cn_common.bin',
    request_timeout_seconds=120, cpu_threads=8,
)
queries = []
encoding_started = time.perf_counter()
try:
    for position, cue in enumerate(asr['transcript']):
        cue_id = 'asr:' + hashlib.sha256(json.dumps([
            cue['start_seconds'], cue['end_seconds'], cue['text'],
        ], ensure_ascii=False).encode()).hexdigest()
        query = {
            'id': cue_id, 'cue_ids': [cue_id], 'position': position,
            'start_ms': cue['start_seconds'], 'end_ms': cue['end_seconds'],
            'label': cue.get('speaker_id'), 'understanding_speaker': cue.get('speaker_id'),
            'model': CAMPPLUS_MODEL, 'encoder_revision': CAMPPLUS_ENCODER_REVISION,
        }
        if query['end_ms'] - query['start_ms'] >= parameters['min_duration_ms']:
            embedding, digest = host._embed_voiceprint_sample(
                encoder, source_input, lease, start_ms=query['start_ms'], end_ms=query['end_ms'],
                check_active=lambda: None,
            )
            query.update(vector=embedding.vector, pcm_sha256=digest)
        queries.append(query)
finally:
    encoder.close()
encoding_seconds = time.perf_counter() - encoding_started
# Reference labels always come from original non-null ASR IDs, never scored queries.
references = [query for query in queries if query['label'] and 'vector' in query]
scoring_started = time.perf_counter()
lines = [host._score_voiceprint_sample(query, references, parameters=parameters) for query in queries]
prefill = deepcopy(asr)
for cue, line in zip(prefill['transcript'], lines):
    if line['band'] in {'ACCEPT', 'CONFLICT'} and line['candidates']:
        cue['speaker_id'] = line['candidates'][0]['speaker']
    # Unscored evidence preserves the original ID, including null, with a reason in lines.
prefill.update(speaker_scope='video', speaker_scope_id=media_sha,
               speaker_ids=sorted({cue['speaker_id'] for cue in asr['transcript'] if cue.get('speaker_id')}))
scoring_seconds = time.perf_counter() - scoring_started
assert all(reference['label'] for reference in references)
assert all(before.get('speaker_id') is None or after.get('speaker_id') is not None
           for before, after in zip(asr['transcript'], prefill['transcript']))
for before, after in zip(asr['transcript'], prefill['transcript']):
    assert {key: value for key, value in before.items() if key != 'speaker_id'} == {
        key: value for key, value in after.items() if key != 'speaker_id'}
assert asr_path.read_bytes() == asr_raw
artifact = {
    'identity': identity, 'references': references, 'queries': queries, 'lines': lines,
    'prefilled_asr': prefill, 'pcm_path': str(wav_path),
    'pcm_file_sha256': hashlib.sha256(wav_path.read_bytes()).hexdigest(),
    'timing_seconds': {'decode': decode_seconds, 'encoding': encoding_seconds,
                       'scoring_and_prefill': scoring_seconds, 'total': time.perf_counter() - started},
    'embedding_queries': sum('vector' in query for query in queries),
    'limits': ['Reuses production B0 encoding, scoring, exclusions and thresholds; no clustering.',
               'Original ASR IDs are anonymous reference labels, not certified character names.',
               'Null-ID audio is query-only and never enters the reference pool.',
               'Too-short or unsupported queries retain their original ID with explicit score reasons.',
               'No new Understanding, Refine, ASR, GPU, CPU/prod or database access.'],
}
artifact_path.write_text(json.dumps(artifact, ensure_ascii=False) + '\n')
summary = {
    'identity': identity, 'asr_cues': len(queries), 'reference_samples': len(references),
    'reference_ids': dict(Counter(reference['label'] for reference in references)),
    'null_id_reference_samples': 0,
    'null_id_queries': sum(not query['label'] for query in queries),
    'embedded_null_id_queries': sum(not query['label'] and 'vector' in query for query in queries),
    'before_non_null': sum(bool(cue.get('speaker_id')) for cue in asr['transcript']),
    'after_non_null': sum(bool(cue.get('speaker_id')) for cue in prefill['transcript']),
    'non_null_to_null': sum(bool(before.get('speaker_id')) and not after.get('speaker_id')
                            for before, after in zip(asr['transcript'], prefill['transcript'])),
    'bands': dict(Counter(str(line['band']) for line in lines)),
    'embedding_queries': artifact['embedding_queries'],
    'source_asr_unchanged': True, 'clustering_calls': 0, 'generative_model_calls': 0,
    'timing_seconds': artifact['timing_seconds'], 'limits': artifact['limits'],
}
(out / 'evidence/audio-first-summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n')
print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
