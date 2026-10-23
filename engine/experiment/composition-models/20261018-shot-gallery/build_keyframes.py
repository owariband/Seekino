"""Offline shot detection and motion-aware source-frame capture for the shared gallery."""
import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
from fractions import Fraction
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

OUT = Path(__file__).resolve().parent
sys.path.insert(0, str(OUT / '_deps'))
import cv2
import imagehash
import numpy as np
from PIL import Image
from scenedetect.detectors import AdaptiveDetector, ThresholdDetector

PARAMETERS = dict(
    detector='PySceneDetect AdaptiveDetector + ThresholdDetector fades', scenedetect_version='0.6.7.1',
    adaptive_threshold=3.0, min_content_val=15.0, min_scene_len_seconds=0.25,
    window_width=2, detection_width=320, detection_height=180,
    fade_black_threshold=12, fade_white_threshold=240, fade_min_scene_seconds=0.5,
    fade_cut_distance_from_existing_seconds=0.25,
    capture_height=720, jpeg_qscale=3, motion_grid=[160, 90],
    motion_window_frames=5, central_window=[0.4, 0.6],
    extra_windows=[[0.15, 0.35], [0.65, 0.85]],
    long_shot_seconds=4, very_long_shot_seconds=8,
    motion_p90_threshold=2.5, motion_span_threshold=8.0,
    intervals='Half-open [start_ms, end_ms); decoded frame n maps to n/fps.',
)


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + '\n')


def detect_episode(source):
    cv2.setNumThreads(1)
    episode = source['episode']
    checkpoint = OUT / 'shots' / f'ep{episode:02d}.json'
    if checkpoint.exists():
        cached = json.loads(checkpoint.read_text())
        assert cached['parameters'] == PARAMETERS, 'Detection parameters changed; use a fresh checkpoint directory'
        return cached
    started = time.monotonic()
    path = source['path']
    probe = json.loads(subprocess.check_output([
        'ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries',
        'stream=avg_frame_rate,r_frame_rate,width,height,start_time,duration,nb_frames',
        '-of', 'json', path]))['streams'][0]
    fps = float(Fraction(probe['avg_frame_rate']))
    assert abs(fps - float(Fraction(probe['r_frame_rate']))) < 0.001, probe
    detector = AdaptiveDetector(
        adaptive_threshold=PARAMETERS['adaptive_threshold'],
        min_scene_len=max(1, round(fps * PARAMETERS['min_scene_len_seconds'])),
        window_width=PARAMETERS['window_width'], min_content_val=PARAMETERS['min_content_val'])
    fade_minimum = round(fps * PARAMETERS['fade_min_scene_seconds'])
    fades = [ThresholdDetector(threshold=PARAMETERS['fade_black_threshold'], min_scene_len=fade_minimum),
             ThresholdDetector(threshold=PARAMETERS['fade_white_threshold'], min_scene_len=fade_minimum,
                               method=ThresholdDetector.Method.CEILING)]
    command = ['ffmpeg', '-v', 'error', '-threads', '2', '-i', path, '-map', '0:v:0',
               '-an', '-sn', '-vf', 'scale=320:180:flags=area', '-threads', '1',
               '-fps_mode', 'passthrough', '-f', 'rawvideo', '-pix_fmt', 'bgr24', 'pipe:1']
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    changes, thumbnails, cuts, fade_cuts = [], [], [0], []
    previous = None
    number = 0
    frame_bytes = 320 * 180 * 3
    while True:
        data = process.stdout.read(frame_bytes)
        if not data:
            break
        assert len(data) == frame_bytes
        frame = np.frombuffer(data, np.uint8).reshape(180, 320, 3)
        cuts.extend(int(cut) for cut in detector.process_frame(number, frame))
        for fade in fades:
            fade_cuts.extend(int(cut) for cut in fade.process_frame(number, frame))
        gray = cv2.resize(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), (160, 90), interpolation=cv2.INTER_AREA)
        changes.append(float(np.mean(cv2.absdiff(gray, previous))) if previous is not None else 0.)
        # Small temporal probes identify shots whose composition changes internally.
        thumbnails.append(cv2.resize(gray, (32, 18), interpolation=cv2.INTER_AREA))
        previous = gray
        number += 1
    error = process.stderr.read().decode()
    assert process.wait() == 0, error
    cuts.extend(int(cut) for cut in detector.post_process(number))
    for cut in sorted(set(fade_cuts)):
        if min(abs(cut - existing) for existing in cuts + [number]) > round(fps * PARAMETERS['fade_cut_distance_from_existing_seconds']):
            cuts.append(cut)
    cuts = sorted(set([c for c in cuts if 0 <= c < number] + [number]))
    changes = np.asarray(changes, np.float32)
    np.save(OUT / 'shots' / f'ep{episode:02d}-motion.npy', changes)
    thumbnails = np.asarray(thumbnails, np.float32)
    shots = []
    for index, (start, end) in enumerate(zip(cuts, cuts[1:]), 1):
        count = end - start
        duration = count / fps
        inner = changes[start + 1:end]
        p90 = float(np.percentile(inner, 90)) if len(inner) else 0.
        early, late = start + int(count * .2), min(end - 1, start + int(count * .8))
        span = float(np.mean(np.abs(thumbnails[early] - thumbnails[late])))
        moving = p90 >= PARAMETERS['motion_p90_threshold'] and span >= PARAMETERS['motion_span_threshold']
        # Clip the smoothing window to this shot so cuts do not bias representative selection.
        local = changes[start:end].copy()
        local[0] = local[1] if len(local) > 1 else 0
        smooth = np.convolve(np.pad(local, (2, 2), mode='edge'), np.ones(5) / 5, mode='valid')

        def select(low, high, center):
            begin = min(count - 1, max(0, int(count * low)))
            finish = min(count, max(begin + 1, int(count * high)))
            return start + min(range(begin, finish), key=lambda n: (round(float(smooth[n]), 4), abs(n - center * count)))

        selected = [select(.4, .6, .5)]
        reason = 'central_low_motion'
        if duration > PARAMETERS['very_long_shot_seconds'] or moving:
            selected += [select(.15, .35, .25), select(.65, .85, .75)]
            reason = 'three_views_motion' if moving else 'three_views_over_8s'
        elif duration > PARAMETERS['long_shot_seconds']:
            sides = [select(.15, .35, .25), select(.65, .85, .75)]
            selected.append(max(sides, key=lambda n: (abs(n - selected[0]), -smooth[n-start])))
            reason = 'two_views_over_4s'
        selected = sorted(set(selected))
        shots.append(dict(
            shot_id=f'mygo-ep{episode:02d}-s{index:05d}', episode=episode,
            start_frame=start, end_frame=end,
            start_ms=round(start / fps * 1000), end_ms=round(end / fps * 1000),
            frame_indices=selected, representative_frame_index=select(.4, .6, .5),
            motion_p90=round(p90, 5), motion_span=round(span, 5), internal_motion=moving,
            selection_reason=reason, selected_motion_scores=[round(float(smooth[n-start]), 5) for n in selected]))
    result = dict(episode=episode, source_path=path, fps=fps, fps_ratio=probe['avg_frame_rate'],
                  decoded_frames=number, duration_ms=round(number / fps * 1000),
                  source_metadata=probe, parameters=PARAMETERS, shots=shots,
                  detection_seconds=round(time.monotonic() - started, 2))
    write_json(checkpoint, result)
    print(json.dumps(dict(stage='detected', episode=episode, shots=len(shots),
                         keyframes=sum(len(s['frame_indices']) for s in shots),
                         seconds=result['detection_seconds'])), flush=True)
    return result


def capture_episode(result):
    episode = result['episode']
    checkpoint = OUT / 'shots' / f'ep{episode:02d}-keyframes.jsonl'
    if checkpoint.exists():
        return [json.loads(line) for line in checkpoint.read_text().splitlines()]
    destination = OUT / 'frames' / f'ep{episode:02d}'
    destination.mkdir(parents=True, exist_ok=True)
    indices = sorted(n for shot in result['shots'] for n in shot['frame_indices'])
    # A flat sum hits FFmpeg's expression recursion limit on hundreds of keyframes.
    terms = [f'eq(n\\,{n})' for n in indices]
    while len(terms) > 1:
        terms = ['(' + '+'.join(terms[i:i+2]) + ')' for i in range(0, len(terms), 2)]
    selection = terms[0]
    command = ['ffmpeg', '-v', 'error', '-threads', '2', '-i', result['source_path'],
               '-map', '0:v:0', '-an', '-sn', '-vf',
               f"select={selection},scale=-2:'min({PARAMETERS['capture_height']},ih)'",
               '-fps_mode', 'vfr', '-q:v', str(PARAMETERS['jpeg_qscale']), '-threads', '1',
               '-start_number', '0', '-y', str(destination / 'capture-%06d.jpg')]
    captured = subprocess.run(command, capture_output=True)
    if captured.returncode:
        raise RuntimeError(f"Episode {episode} capture failed: {captured.stderr.decode()[-500:]}")
    assert len(list(destination.glob('capture-*.jpg'))) == len(indices)
    temporary = {n: destination / f'capture-{i:06d}.jpg' for i, n in enumerate(indices)}
    rows = []
    for shot in result['shots']:
        for slot, (number, score) in enumerate(zip(shot['frame_indices'], shot['selected_motion_scores']), 1):
            frame_id = f"{shot['shot_id']}-f{slot}"
            target = destination / f'{frame_id}.jpg'
            temporary[number].replace(target)
            with Image.open(target) as image:
                phash, dhash = str(imagehash.phash(image)), str(imagehash.dhash(image))
                size = list(image.size)
            rows.append(dict(
                frame_id=frame_id, shot_id=shot['shot_id'], episode=episode,
                start_ms=shot['start_ms'], end_ms=shot['end_ms'],
                start_frame=shot['start_frame'], end_frame=shot['end_frame'],
                frame_timestamp_ms=round(number / result['fps'] * 1000), frame_index=number,
                frame_path=str(target.relative_to(OUT)), sha256=hashlib.sha256(target.read_bytes()).hexdigest(),
                phash=phash, dhash=dhash, image_size=size,
                is_representative=number == shot['representative_frame_index'],
                motion_score=score, motion_p90=shot['motion_p90'], motion_span=shot['motion_span'],
                internal_motion=shot['internal_motion'], selection_reason=shot['selection_reason']))
    checkpoint.write_text(''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in rows))
    print(json.dumps(dict(stage='captured', episode=episode, keyframes=len(rows))), flush=True)
    return rows


def process_episode(source):
    return capture_episode(detect_episode(source))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--workers', type=int, default=3)
    parser.add_argument('--episodes', type=int, nargs='*')
    args = parser.parse_args()
    sources = json.loads((OUT / 'sources.json').read_text())
    episodes = [s for s in sources['episodes'] if not args.episodes or s['episode'] in args.episodes]
    (OUT / 'shots').mkdir(exist_ok=True)
    write_json(OUT / 'parameters.json', PARAMETERS)
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        for future in as_completed([pool.submit(process_episode, s) for s in episodes]):
            future.result()
            completed = [json.loads(line) for p in sorted((OUT / 'shots').glob('ep*-keyframes.jsonl'))
                         for line in p.read_text().splitlines()]
            temporary = OUT / 'keyframes.jsonl.tmp'
            temporary.write_text(''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in completed))
            temporary.replace(OUT / 'keyframes.jsonl')
    rows = [json.loads(line) for p in sorted((OUT / 'shots').glob('ep*-keyframes.jsonl'))
            for line in p.read_text().splitlines()]
    rows.sort(key=lambda r: (r['episode'], r['frame_index']))
    (OUT / 'keyframes.jsonl').write_text(''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in rows))
    shots = [s for p in sorted((OUT / 'shots').glob('ep[0-9][0-9].json'))
             for s in json.loads(p.read_text())['shots']]
    (OUT / 'shots.jsonl').write_text(''.join(json.dumps(shot) + '\n' for shot in shots))
    print(json.dumps(dict(stage='keyframes_complete', episodes=len(set(r['episode'] for r in rows)),
                         shots=len(set(r['shot_id'] for r in rows)), keyframes=len(rows))), flush=True)


if __name__ == '__main__':
    main()
