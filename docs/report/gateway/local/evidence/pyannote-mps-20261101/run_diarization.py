"""Measured, isolated pyannote speaker grouping using the official pinned weights."""
from pathlib import Path
import argparse
import datetime
import json
import os
import resource
import signal
import subprocess
import threading
import time
import traceback
import warnings
import wave

OUT = Path(__file__).resolve().parent
ROOT = Path('/Users/hypnos/workspace/agent/Seekino')
os.environ['HF_HOME'] = str(ROOT / 'outputs/local-models/whisperx/hf-cache')
os.environ['HF_HUB_OFFLINE'] = '1'
os.environ['HF_HUB_DISABLE_TELEMETRY'] = '1'
os.environ['PYANNOTE_METRICS_ENABLED'] = '0'
os.environ['PYTORCH_ENABLE_MPS_FALLBACK'] = '0'
os.environ['MPLCONFIGDIR'] = str(ROOT / 'outputs/local-models/whisperx/matplotlib')
os.environ['MPLBACKEND'] = 'Agg'
warnings.filterwarnings('ignore', message='(?s).*torchcodec is not installed.*')

parser = argparse.ArgumentParser()
parser.add_argument('--device', choices=('cpu', 'mps'), required=True)
parser.add_argument('--seconds', type=int, default=0)
parser.add_argument('--tag', required=True)
parser.add_argument('--timeout', type=int, default=1200)
args = parser.parse_args()
DEST = OUT / args.tag
DEST.mkdir(exist_ok=True)
assert not (DEST / 'result.json').exists(), 'Do not overwrite a completed run'
START = time.monotonic()
state = {'pid': os.getpid(), 'device': args.device, 'stage': 'starting',
         'started_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
         'cpu_threads': 6, 'input_seconds_limit': args.seconds,
         'peak_rss_bytes': 0, 'peak_mps_allocated_bytes': 0,
         'peak_mps_driver_allocated_bytes': 0,
         'speaker_count_forced': False, 'production_pipeline_integrated': False}
lock = threading.Lock()
finished = threading.Event()
torch = None

def record(event, **values):
    with lock:
        state.update(values)
        state['elapsed_seconds'] = round(time.monotonic() - START, 3)
        row = {'event': event, **state}
        temp = DEST / 'progress.tmp'
        temp.write_text(json.dumps(row, ensure_ascii=False, indent=2) + '\n')
        temp.replace(DEST / 'progress.json')
        with (DEST / 'events.jsonl').open('a') as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + '\n')
    print(json.dumps({k: row.get(k) for k in (
        'event', 'pid', 'stage', 'elapsed_seconds', 'completed', 'total',
        'stage_elapsed_seconds', 'stage_eta_seconds', 'sampled_cpu_percent',
        'peak_rss_bytes', 'peak_mps_allocated_bytes', 'speaker_count', 'error')},
        ensure_ascii=False), flush=True)

def monitor():
    last = 0
    while not finished.wait(1):
        sample = subprocess.check_output(
            ['ps', '-p', str(os.getpid()), '-o', 'rss=,pcpu='], text=True).split()
        rss = int(sample[0]) * 1024
        mps = driver = 0
        if torch is not None and args.device == 'mps' and torch.backends.mps.is_available():
            mps = torch.mps.current_allocated_memory()
            driver = torch.mps.driver_allocated_memory()
        with lock:
            state.update(sampled_rss_bytes=rss, sampled_cpu_percent=float(sample[1]))
            state['peak_rss_bytes'] = max(state['peak_rss_bytes'], rss)
            state['peak_mps_allocated_bytes'] = max(state['peak_mps_allocated_bytes'], mps)
            state['peak_mps_driver_allocated_bytes'] = max(state['peak_mps_driver_allocated_bytes'], driver)
        if time.monotonic() - last >= 15:
            record('heartbeat')
            last = time.monotonic()

def deadline(*_):
    raise TimeoutError(f'Diarization observation reached {args.timeout} seconds')

def main():
    global torch
    signal.signal(signal.SIGALRM, deadline)
    signal.alarm(args.timeout)
    threading.Thread(target=monitor, daemon=True).start()
    record('started')
    import numpy as np
    import torch as torch_module
    torch = torch_module
    torch.set_num_threads(6)
    torch.set_num_interop_threads(1)
    if args.device == 'mps' and not torch.backends.mps.is_available():
        raise RuntimeError('MPS is unavailable; refusing silent CPU fallback')
    from pyannote.audio import Pipeline
    from pyannote.audio.pipelines.utils.hook import TimingHook
    manifest = json.loads((OUT / 'diarization-model.json').read_text())
    record('load_model', stage='load_model', model=manifest['model'],
           revision=manifest['revision'])
    loaded_at = time.monotonic()
    pipeline = Pipeline.from_pretrained(manifest['local_path'])
    pipeline.to(torch.device(args.device))
    segmentation_model = pipeline._segmentation.model
    embedding_device = getattr(pipeline._embedding, 'device', None)
    record('model_loaded', model_load_seconds=round(time.monotonic()-loaded_at, 3),
           segmentation_device=str(next(segmentation_model.parameters()).device),
           embedding_device=str(embedding_device),
           segmentation_batch_size=pipeline.segmentation_batch_size,
           embedding_batch_size=pipeline.embedding_batch_size)
    with wave.open(str(OUT / 'full-episode.wav'), 'rb') as handle:
        rate = handle.getframerate()
        assert (handle.getnchannels(), handle.getsampwidth(), rate) == (1, 2, 16000)
        frames = min(handle.getnframes(), args.seconds*rate) if args.seconds else handle.getnframes()
        pcm = handle.readframes(frames)
    audio = torch.from_numpy(np.frombuffer(pcm, dtype='<i2').astype(np.float32)/32768).unsqueeze(0)
    del pcm
    file = {'waveform': audio, 'sample_rate': rate, 'uri': 'lets-build-gpt'}
    record('inference_started', stage='inference', audio_duration_seconds=frames/rate)
    step_starts = {}
    last_progress = 0
    inference_start = time.monotonic()
    with TimingHook() as timing:
        def hook(step_name, step_artifact, file=None, total=None, completed=None):
            nonlocal last_progress
            timing(step_name, step_artifact, file=file, total=total, completed=completed)
            now = time.monotonic()
            step_starts.setdefault(step_name, now)
            elapsed = now-step_starts[step_name]
            count = int(completed) if completed is not None else None
            maximum = int(total) if total is not None else None
            eta = elapsed*(maximum-count)/count if count and maximum else None
            changed = state['stage'] != step_name
            with lock:
                state.update(stage=step_name, completed=count, total=maximum,
                             stage_elapsed_seconds=round(elapsed, 3),
                             stage_eta_seconds=round(max(0, eta), 1) if eta is not None else None)
            if changed or now-last_progress>=5 or count is not None and count==maximum:
                record('stage_progress')
                last_progress = now
        # Let the model infer the speaker count; never force the expected single speaker.
        output = pipeline(file, hook=hook)
    if args.device == 'mps':
        torch.mps.synchronize()
    inference_seconds = time.monotonic()-inference_start
    def turns(annotation):
        return [{'start': float(segment.start), 'end': float(segment.end), 'speaker': str(speaker)}
                for segment, _, speaker in annotation.itertracks(yield_label=True)]
    regular = turns(output.speaker_diarization)
    exclusive = turns(output.exclusive_speaker_diarization)
    labels = sorted({x['speaker'] for x in regular})
    for turn in [*regular, *exclusive]:
        if not np.isfinite([turn['start'], turn['end']]).all() or turn['end'] <= turn['start']:
            raise ValueError('Diarization returned non-finite or reversed timestamps')
    embeddings = output.speaker_embeddings
    embeddings_finite = bool(np.isfinite(embeddings).all()) if embeddings is not None else None
    if embeddings_finite is False:
        raise ValueError('Diarization speaker embeddings contain non-finite values')
    result = {'model': manifest['model'], 'revision': manifest['revision'],
              'device': args.device, 'audio_duration_seconds': frames/rate,
              'speaker_count': len(labels), 'speakers': labels,
              'speaker_count_forced': False, 'segments': regular,
              'exclusive_segments': exclusive, 'inference_seconds': inference_seconds,
              'stage_timings': file.get('timing', {}),
              'speaker_embeddings_finite': embeddings_finite,
              'speaker_embedding_shape': list(embeddings.shape) if embeddings is not None else None,
              'elapsed_seconds': time.monotonic()-START,
              'process_peak_rss_bytes': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss}
    (DEST / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    record('completed', stage='completed', speaker_count=len(labels),
           inference_seconds=round(inference_seconds, 3),
           process_peak_rss_bytes=result['process_peak_rss_bytes'],
           result_path=str(DEST / 'result.json'), stage_eta_seconds=0)
    signal.alarm(0)

if __name__ == '__main__':
    try:
        main()
    except BaseException as error:
        record('failed', stage='failed', error_type=type(error).__name__, error=str(error))
        traceback.print_exc()
        raise
    finally:
        finished.set()
