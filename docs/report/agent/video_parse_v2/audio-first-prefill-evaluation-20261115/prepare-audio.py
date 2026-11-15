"""Build anonymous prefill using cached ASR and local audio only."""

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import time
import wave

from engine.parsev2.diarization import DiarizationResult, assign_video_speakers
from engine.parsev2.voiceprint_matching import cluster_samples
from gateway.local.campplus import (
    CAMPPLUS_ENCODER_REVISION, CAMPPLUS_MODEL, CampPlusSpeakerEmbedder,
)

parser = argparse.ArgumentParser()
parser.add_argument('--data-root', type=Path, required=True)
parser.add_argument('--source', type=Path, required=True)
args = parser.parse_args()
root = args.data_root.resolve()
source = args.source.resolve()
out = root / 'outputs/verification/audio-first-prefill-evaluation-20261115'
private = out / 'private'
private.mkdir(parents=True, exist_ok=True)
(out / 'evidence').mkdir(exist_ok=True)
asr_path = root / 'outputs/verification/voiceprint-early-preparation-20261115/private/asr.json'
asr_raw = asr_path.read_bytes()
asr = json.loads(asr_raw)
with source.open('rb') as stream:
    media_sha = hashlib.file_digest(stream, 'sha256').hexdigest()
identity = {
    'media_sha256': media_sha, 'asr_sha256': hashlib.sha256(asr_raw).hexdigest(),
    'model': CAMPPLUS_MODEL, 'encoder_revision': CAMPPLUS_ENCODER_REVISION,
    'window_ms': 1000, 'minimum_ms': 400, 'cpu_threads': 8,
    'clustering': {'method': 'agglomerative', 'threshold': 0.35},
}
artifact_path = private / 'audio-first-artifact.json'
if artifact_path.exists():
    existing = json.loads(artifact_path.read_text())
    assert existing['identity'] == identity, 'Audio cache provenance differs'
    print(json.dumps({'cache_hit': True, 'encoder_calls': 0,
                      'cached_samples': len(existing['samples']), 'identity': identity}))
    raise SystemExit(0)

started = time.perf_counter()
wav_path = private / 'source-16k.wav'
subprocess.run([
    'ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-y', '-threads', '8',
    '-i', str(source), '-vn', '-ac', '1', '-ar', '16000', '-c:a', 'pcm_s16le', str(wav_path),
], check=True)
with wave.open(str(wav_path), 'rb') as audio:
    assert (audio.getnchannels(), audio.getsampwidth(), audio.getframerate()) == (1, 2, 16000)
    pcm = audio.readframes(audio.getnframes())
duration_ms = len(pcm) // 32
decode_seconds = time.perf_counter() - started
windows = []
short_cues = 0
cue_ids = []
for cue in asr['transcript']:
    cue_id = 'asr:' + hashlib.sha256(json.dumps([
        cue['start_seconds'], cue['end_seconds'], cue['text'],
    ], ensure_ascii=False).encode()).hexdigest()
    cue_ids.append(cue_id)
    start, end = cue['start_seconds'], min(cue['end_seconds'], duration_ms)
    if end - start < 400:
        short_cues += 1
        continue
    bounds = list(range(start, end, 1000))
    if len(bounds) > 1 and end - bounds[-1] < 400:
        bounds.pop()
    for index, left in enumerate(bounds):
        right = bounds[index + 1] if index + 1 < len(bounds) else end
        windows.append({'id': f'audio:{left:09d}:{right:09d}', 'start_ms': left,
                        'end_ms': right, 'cue_ids': [cue_id], 'label': None})

encoder = CampPlusSpeakerEmbedder(
    CAMPPLUS_MODEL, checkpoint_path=root / 'outputs/local-models/campplus/campplus_cn_common.bin',
    request_timeout_seconds=120, cpu_threads=8,
)
encoding_started = time.perf_counter()
clip_path = private / 'encoding-window.wav'
samples = []
try:
    for window in windows:
        clip = pcm[window['start_ms'] * 32:window['end_ms'] * 32]
        assert len(clip) >= 400 * 32
        with wave.open(str(clip_path), 'wb') as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(16000)
            audio.writeframes(clip)
        result = encoder.embed_audio(clip_path)
        samples.append({**window, 'model': result.model,
                        'encoder_revision': result.encoder_revision,
                        'pcm_sha256': hashlib.sha256(clip).hexdigest(), 'vector': result.vector})
        if len(samples) % 100 == 0:
            print(f'encoded={len(samples)}/{len(windows)}', flush=True)
finally:
    encoder.close()
    clip_path.unlink(missing_ok=True)
encoding_seconds = time.perf_counter() - encoding_started
clustering_started = time.perf_counter()
clusters = cluster_samples(samples, **identity['clustering'])
group_for = {sample_id: f'VOICE_{group["cluster_id"]:03d}'
             for group in clusters['groups'] for sample_id in group['sample_ids']}
turns = [{'start_ms': sample['start_ms'], 'end_ms': sample['end_ms'],
          'speaker_id': group_for[sample['id']]} for sample in samples]
diarization = DiarizationResult.model_validate({
    'turns': turns, 'exclusive_turns': turns,
}).validate_duration(duration_ms).model_dump()
prefill = assign_video_speakers(asr, diarization, scope_id=media_sha)
assert all(sample['label'] is None for sample in samples)
for before, after in zip(asr['transcript'], prefill['transcript']):
    assert {key: value for key, value in before.items() if key != 'speaker_id'} == {
        key: value for key, value in after.items() if key != 'speaker_id'}
assert asr_path.read_bytes() == asr_raw
artifact = {
    'identity': identity, 'samples': samples, 'groups': clusters['groups'],
    'diarization': diarization, 'prefilled_asr': prefill,
    'timing_seconds': {'decode': decode_seconds, 'encoding': encoding_seconds,
                       'clustering_and_prefill': time.perf_counter() - clustering_started,
                       'total': time.perf_counter() - started},
    'short_cues_skipped': short_cues, 'encoder_calls': len(samples),
    'limits': ['Only ASR intervals, waveform and encoder enter stage 1; no Understanding input.',
               'Anonymous clusters are hypotheses, not certified single-person identities.',
               'Mixed cues retain a null aggregate ID; detailed turns are preserved separately.',
               'No VAD or word-speaker alignment is inferred from these fixed windows.'],
}
artifact_path.write_text(json.dumps(artifact, ensure_ascii=False) + '\n')
summary = {
    'identity': identity, 'asr_cues': len(asr['transcript']), 'audio_samples': len(samples),
    'short_cues_skipped': short_cues, 'anonymous_groups': clusters['k'],
    'cues_with_single_anonymous_group': sum(bool(cue.get('speaker_id')) for cue in prefill['transcript']),
    'cues_with_unknown_or_mixed_group': sum(not cue.get('speaker_id') for cue in prefill['transcript']),
    'source_asr_unchanged': True, 'understanding_used': False, 'encoder_calls': len(samples),
    'timing_seconds': artifact['timing_seconds'], 'limits': artifact['limits'],
}
(out / 'evidence/audio-first-summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n')
print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
