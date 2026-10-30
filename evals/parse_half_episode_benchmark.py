"""Bounded real-pipeline versus sequential compact JSON half-episode experiment.

Each phase is explicit; imports never start providers or mutate shared storage.
Run with the source checkout's environment and dependencies, from this worktree.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from contextlib import contextmanager
from dataclasses import replace
import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import threading
import time


def save(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f'.{threading.get_ident()}.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + '\n')
    temporary.replace(path)


def digest(path: Path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def emit(event, **fields):
    print(json.dumps({'time': time.time(), 'event': event, **fields}, ensure_ascii=False, default=str), flush=True)


def environment(args):
    """Unique test stores and owned processes, never the development instances."""
    output = args.out.resolve()
    os.environ.update({
        'SEEKINO_ENVIRONMENT': 'test',
        'SEEKINO_DATABASE_URL': 'mysql+pymysql://root@127.0.0.1:13306/seekino_half_episode_20261030_test?charset=utf8mb4',
        'SEEKINO_MYSQL_DATA_DIR': str(output / 'storage/mysql'),
        'SEEKINO_ELASTICSEARCH_URL': 'http://127.0.0.1:19200',
        'SEEKINO_ELASTICSEARCH_INDEX': 'seekino_half_episode_chunks_20261030_test',
        'SEEKINO_ELASTICSEARCH_EVENT_INDEX': 'seekino_half_episode_events_20261030_test',
        'SEEKINO_ELASTICSEARCH_DATA_DIR': str(output / 'storage/elasticsearch'),
        'SEEKINO_ELASTICSEARCH_TRANSPORT_PORT': '19300',
        'SEEKINO_ELASTICSEARCH_HEAP_SIZE': '512m',
        'SEEKINO_ARTIFACT_ROOT': str(output / 'library'),
        'SEEKINO_MODEL_CONFIG': str(output / 'model-config.yaml'),
        'SEEKINO_MODEL_TRACE_DIR': str(output / 'a-current/model-trace'),
        'SEEKINO_MYSQLD_BIN': str(args.source_root / 'outputs/tools/mysql-8.4.13-macos15-arm64/bin/mysqld'),
        'SEEKINO_ELASTICSEARCH_BIN': str(args.source_root / 'outputs/tools/elasticsearch-8.19.22/bin/elasticsearch'),
    })
    os.environ.pop('SEEKINO_ELASTICSEARCH_API_KEY', None)


def prepare(args):
    import yaml
    from server.config import ServerSettings
    from server.bootstrap import prepare_service_configuration

    args.out.mkdir(parents=True, exist_ok=False)
    origin = args.source_root / 'outputs/verification/ep04-doubao-nonthinking-20261030'
    source = json.loads((origin / 'source.json').read_text())
    video = Path(source['path'])
    assert digest(video) == source['sha256']
    config = yaml.safe_load((args.source_root / 'config/local-dev-config.yaml').read_text())
    config['model_sources']['llama_embedding_local']['base_url'] = 'http://127.0.0.1:18844'
    (args.out / 'model-config.yaml').write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False))
    environment(args)
    settings = ServerSettings.from_environment()
    _, profile = prepare_service_configuration(settings)
    frozen = profile.to_video_parse_v2().model_dump(mode='json')
    save(args.out / 'a-requested-config.json', frozen)
    assert frozen['models']['understanding']['model'] == 'k3.1-pro'
    assert frozen['models']['asr']['scope'] == 'video'
    assert frozen['chunk_duration'] == 60
    background = (origin / 'background.md').read_text()
    (args.out / 'background.md').write_text(background)
    clip = args.out / 'ep04-first-12min.mp4'
    began = time.monotonic()
    subprocess.run(['ffmpeg', '-v', 'error', '-nostdin', '-i', str(video), '-map', '0:v:0',
                    '-map', '0:a:0', '-t', '720', '-vf', 'fps=24', '-c:v', 'libx264',
                    '-preset', 'veryfast', '-crf', '18', '-c:a', 'aac', '-b:a', '192k',
                    '-movflags', '+faststart', str(clip)], check=True, timeout=600)
    probe = json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-show_format',
                       '-show_streams', '-of', 'json', str(clip)], text=True))
    duration = float(probe['format']['duration'])
    if not 719.95 <= duration <= 720:
        raise ValueError(f'Clip must produce exactly twelve chunks: {duration}')
    save(args.out / 'media.json', {'original': source, 'clip': str(clip), 'clip_sha256': digest(clip),
         'duration_seconds': duration, 'preparation_seconds': time.monotonic() - began,
         'transform': 'first 720 s, 24 fps H264 CRF18/AAC192k; shared by both arms', 'probe': probe})
    save(args.out / 'manifest.json', {
        'created_at_epoch': time.time(), 'git_head': subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip(),
        'script_sha256': digest(Path(__file__)), 'range_ms': [0, 720000], 'chunk_ms': 60000,
        'a': 'actual current V2 Consumer/Understanding/Refine/persistence/index; first-chunk safety gate before releasing other model requests',
        'b': 'Doubao disabled; compact-shot-v2 plain JSON; serial history; no Refine',
        'common': 'same half-episode, same background, A-produced full-video WhisperX cues; same core 3 fps frames',
        'visual_context_difference': 'A retains configured +/-3 s visual context; B uses core frames and previous generated text',
        'history': 'latest whole generated events, up to 24000 JSON characters; names from all previous generated dialogue; source chunks retained; no A output or answers',
        'budget': {'a_remote_generations': 48, 'b_remote_generations': 12, 'per_request_seconds': 600, 'execution_wall_seconds': 5400},
        'a_first_chunk_gate': 'safety preflight included in observed wall clock; record gate time, do not claim unmodified production throughput',
        'evaluation': 'whole 12-minute sequence; facts and evidence availability separate; uncertainty retained; no p95/p99',
    })
    emit('prepared', duration_seconds=duration, output=args.out)


def local_services(args):
    from scripts import local_services as services
    services.STATE = Path(__file__).resolve().parents[1] / 'outputs/half-episode-services'
    services.STATE.mkdir(parents=True, exist_ok=True)
    return services


def start_storage(args):
    from server.config import ServerSettings
    services = local_services(args)
    settings = ServerSettings.from_environment()
    services.start_mysql(settings)
    services.start_elasticsearch(settings)
    services.initialize_storage()
    if not services.owned_pid('embedding'):
        services.launch('embedding', [sys.executable, 'scripts/run_local_embedding.py',
            '--binary', str(args.source_root / 'outputs/tools/llama.cpp/llama-server'),
            '--model', str(args.source_root / 'outputs/local-models/qwen3-embedding-0.6b/Qwen3-Embedding-0.6B-Q8_0.gguf'),
            '--port', '18844'])
        services.wait_ready('embedding', lambda: services.listening(18844), timeout=60)
    emit('isolated_storage_ready')


def run_a(args):
    from fastapi.testclient import TestClient
    from sqlalchemy import select
    from server.config import ServerSettings
    from server.main import init_application
    from server.api.app import build_app
    from server.model.job import VideoParseTask, ChunkParseTask, ChunkParseContent, IntervalParseTask
    from server.model.video import VideoChunk
    from gateway.usage import ModelCallTracker, ModelCallCancelledError
    from engine.parsev2.observations import select_asr_window

    out = args.out / 'a-current'
    out.mkdir(exist_ok=True)
    if (out / 'run-start.json').exists():
        raise ValueError('A already attempted; no implicit rerun')
    settings = ServerSettings.from_environment()
    runtime = init_application(settings)
    consumer = runtime.services.parse_consumer
    queue = consumer._queue
    service = consumer._handlers.understand.__self__
    # Bound actual provider attempts, including Refine tool rounds and retries.
    counter_lock = threading.Lock()
    calls = []
    timers = []
    original_mark = ModelCallTracker.mark_request_started
    def mark(tracker):
        if tracker.provider in ('kimi', 'ark'):
            with counter_lock:
                if len(calls) >= 48:
                    raise ModelCallCancelledError('Experiment remote generation budget exhausted')
                calls.append({'call_id': tracker.call_id, 'model': tracker.model,
                              'operation': tracker.operation, 'context': dict(tracker.context), 'epoch': time.time()})
                save(out / 'admitted-calls.json', calls)
            timer = threading.Timer(600, tracker.cancel)
            timer.daemon = True
            timer.start()
            timers.append(timer)
        return original_mark(tracker)
    ModelCallTracker.mark_request_started = mark
    original_asr = service._prepare_video_asr
    async def capture_asr(lease, task_input, model):
        began = time.monotonic()
        await original_asr(lease, task_input, model)
        transcript = service._video_transcripts[(lease.video_parse_task_id, lease.lease_version)]
        save(args.out / 'common/full-asr.json', transcript)
        save(args.out / 'common/asr-timing.json', {'seconds': time.monotonic() - began})
        for seq in range(12):
            save(args.out / f'common/chunk-{seq:02}/asr.json', select_asr_window(
                transcript, start_ms=seq * 60000, end_ms=(seq + 1) * 60000))
        emit('full_asr_frozen', seconds=time.monotonic() - began)
    service._prepare_video_asr = capture_asr
    original_episode = service._media_service.prepare_episode
    @contextmanager
    def capture_episode(*positional, **keywords):
        with original_episode(*positional, **keywords) as manifest_path:
            manifest = json.loads(manifest_path.read_text())
            assert len(manifest['chunks']) == 12
            for seq, row in enumerate(manifest['chunks']):
                folder = args.out / f'common/chunk-{seq:02}'
                frames = []
                for stamp, relative in row['frames']:
                    original = manifest_path.parent / relative
                    target = folder / 'frames' / f'{stamp:09}.jpg'
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if not target.exists():
                        os.link(original, target)
                    frames.append({'timestamp_ms': stamp, 'file': str(target.relative_to(args.out)), 'sha256': digest(target)})
                save(folder / 'frames.json', frames)
            save(args.out / 'common/media-frozen.json', {'chunks': 12, 'epoch': time.time(), 'source': keywords['source_video']})
            yield manifest_path
    service._media_service.prepare_episode = capture_episode
    original_understand = service.understand_chunk
    gate = asyncio.Event()
    first_claim = False
    gate_failed = False
    async def capture_understand(execution, **kwargs):
        nonlocal first_claim, gate_failed
        seq = execution.sequence_no
        folder = args.out / f'common/chunk-{seq:02}'
        folder.mkdir(parents=True, exist_ok=True)
        save(folder / 'background.json', execution.background)
        (folder / 'a-prompt.txt').write_text(kwargs['prompt'])
        pioneer = not first_claim
        if pioneer:
            first_claim = True
        else:
            await gate.wait()
            if gate_failed:
                raise ValueError('First-chunk preflight failed; no further remote calls')
        began = time.monotonic()
        emit('a_understanding_started', chunk=seq)
        try:
            result = await original_understand(execution, **kwargs)
            if result.timeline and all(len(event.description.strip()) <= 1 for event in result.timeline):
                raise ValueError('Single-character schema output detected')
            save(out / f'base/chunk-{seq:02}.json', result.model_dump(mode='json'))
            save(out / f'base/chunk-{seq:02}-timing.json', {'seconds': time.monotonic() - began})
            if pioneer:
                save(out / 'first-chunk-gate.json', {'chunk': seq, 'seconds': time.monotonic() - began, 'passed': True})
            emit('a_understanding_complete', chunk=seq, seconds=time.monotonic() - began, events=len(result.timeline))
            return result
        except BaseException:
            if pioneer:
                gate_failed = True
            raise
        finally:
            if pioneer:
                gate.set()
    service.understand_chunk = capture_understand
    # Summary is separate from the requested parse comparison; actual Refine/index retained.
    consumer._handlers = replace(consumer._handlers, summarize=None)
    began = time.monotonic()
    try:
        with TestClient(build_app(runtime.services, settings=settings), base_url='http://127.0.0.1:9621') as client:
            def check(response):
                response.raise_for_status()
                return response.json()
            identity = check(client.get('/api/auth/me'))
            tenant = identity['tenantId']
            client.headers['X-CSRF-Token'] = identity['csrfToken']
            background = (args.out / 'background.md').read_text()
            series = check(client.post('/api/series/create', json={'tenantId': tenant,
                'name': 'MyGO EP04 half episode A/B 2026-10-30', 'transcriptLanguageHint': 'ja',
                'importantExtraData': {'format': 'markdown', 'content': background}}))
            save(out / 'series.json', series)
            video = args.out / 'ep04-first-12min.mp4'
            size = video.stat().st_size
            config = json.loads((args.out / 'a-requested-config.json').read_text())
            upload = check(client.post('/api/uploads/create', params={'seriesId': series['seriesId']},
                headers={'Idempotency-Key': 'half-episode-ab-20261030'}, json={'tenantId': tenant,
                'episodeOrder': 4, 'originalFilename': video.name, 'totalSizeBytes': size,
                'mediaType': 'video/mp4', 'contentSha256': digest(video), 'parseConfig': config}))
            with video.open('rb') as stream:
                for seq in upload['missingSegmentNumbers']:
                    start = seq * upload['preferredSegmentSizeBytes']
                    stream.seek(start)
                    data = stream.read(min(upload['preferredSegmentSizeBytes'], size - start))
                    check(client.put('/api/uploads/segments/upload', params={'tenantId': tenant,
                        'uploadSessionId': upload['uploadSessionId'], 'segmentNumber': seq},
                        headers={'Content-Range': f'bytes {start}-{start+len(data)-1}/{size}',
                        'X-Content-SHA256': hashlib.sha256(data).hexdigest(), 'Content-Type': 'application/octet-stream'}, content=data))
            complete = check(client.post('/api/uploads/complete', params={'tenantId': tenant, 'uploadSessionId': upload['uploadSessionId']}))
            save(out / 'complete.json', complete)
            task_id = complete['videoParseTaskId']
            save(out / 'run-start.json', {'epoch': time.time(), 'task_id': task_id, 'tenant_id': tenant, 'gate': 'first successful chunk before other model requests'})
            consumer.start()
            previous = None
            while True:
                with queue._session_factory() as session:
                    task = session.get(VideoParseTask, task_id)
                    chunks = list(session.scalars(select(ChunkParseTask).where(ChunkParseTask.video_task_id == task_id)))
                    intervals = list(session.scalars(select(IntervalParseTask).where(IntervalParseTask.video_task_id == task_id)))
                    contents = []
                    for row, child, chunk in session.execute(select(ChunkParseContent, ChunkParseTask, VideoChunk)
                            .join(ChunkParseTask, ChunkParseTask.id == ChunkParseContent.chunk_task_id)
                            .join(VideoChunk, VideoChunk.id == ChunkParseTask.chunk_id)
                            .where(ChunkParseTask.video_task_id == task_id)):
                        contents.append({'chunk_task_id': child.id, 'sequence_no': chunk.sequence_no,
                                         'version': row.version, 'content': row.content, 'detail': row.detail})
                    save(out / 'latest-contents.json', contents)
                    snapshot = {'elapsed_seconds': time.monotonic() - began, 'status': task.status,
                        'chunks': dict(Counter(f'{c.phase}:{c.status}' for c in chunks)),
                        'refine': dict(Counter(i.status for i in intervals)), 'content_count': len(contents),
                        'task_detail': task.detail, 'remote_calls': len(calls), 'consumer_failure': str(consumer.failure) if consumer.failure else None}
                    save(out / 'progress.json', snapshot)
                    status = (snapshot['status'], str(snapshot['chunks']), str(snapshot['refine']), len(calls))
                    if status != previous:
                        emit('a_progress', **{k:v for k,v in snapshot.items() if k != 'task_detail'})
                        previous = status
                    terminal = task.status in ('COMPLETE', 'FAILED', 'CANCELLED', 'PARSING_FAILED', 'PREPARING_FAILED', 'INDEXING_FAILED')
                if terminal or consumer.failure:
                    break
                if time.monotonic() - began > 5400:
                    snapshot['experiment_interrupted'] = '90-minute wall budget'
                    break
                time.sleep(3)
            save(out / 'result.json', snapshot)
    finally:
        consumer.close(timeout=30)
        runtime.close()
        ModelCallTracker.mark_request_started = original_mark
        for timer in timers:
            timer.cancel()


def history_context(results, limit=24000):
    """Use whole events from actual B outputs, preserving their source chunk."""
    names = sorted({event['speaker'] for result in results for event in result['timeline'] if event.get('speaker')})
    selected = []
    for seq, result in reversed(list(enumerate(results))):
        for event in reversed(result['timeline']):
            item = {'source_chunk': seq, 'event': event}
            if len(json.dumps({'names': names, 'events': [item, *selected]}, ensure_ascii=False)) > limit:
                return {'names': names, 'events': selected, 'older_events_omitted': True}
            selected.insert(0, item)
    return {'names': names, 'events': selected, 'older_events_omitted': False}


def run_b(args):
    import httpx
    from openai import OpenAI
    from evals import parse_compact_protocol as compact
    from gateway.media import encode_media_file
    from gateway.constants import ARK_OPENAI_BASE_URL
    from engine.parsev2.observations import select_asr_window

    out = args.out / 'b-serial'
    out.mkdir(exist_ok=True)
    if (out / 'run-start.json').exists():
        raise ValueError('B already attempted; no implicit replay or reuse of old summaries')
    transcript = json.loads((args.out / 'common/full-asr.json').read_text())
    # Reuse the exact bytes prepared by A, including any upload inference proxy.
    assert (args.out / 'common/media-frozen.json').is_file()
    frames_by_chunk = []
    for seq in range(12):
        rows = json.loads((args.out / f'common/chunk-{seq:02}/frames.json').read_text())
        rows = [row for row in rows if seq * 60000 <= row['timestamp_ms'] < (seq + 1) * 60000]
        assert len(rows) == 180, (seq, len(rows))
        for row in rows:
            assert digest(args.out / row['file']) == row['sha256']
        frames_by_chunk.append(rows)
    background = (args.out / 'background.md').read_text()
    results, summaries = [], []
    overall = time.monotonic()
    save(out / 'run-start.json', {'epoch': time.time(), 'history_limit_chars': 24000})
    def timed_out(_signum, _frame):
        raise TimeoutError('600-second request wall budget')
    previous = signal.signal(signal.SIGALRM, timed_out)
    try:
        for seq in range(12):
            folder = out / f'chunk-{seq:02}'
            folder.mkdir(exist_ok=False)
            start, end = seq * 60000, (seq + 1) * 60000
            observations = select_asr_window(transcript, start_ms=start, end_ms=end)
            observations = {**observations, 'transcript': [{k:v for k,v in cue.items() if k != 'words'} for cue in observations['transcript']]}
            history = history_context(results)
            prompt = compact.COMMON.replace('[0,60000)', f'[{start},{end})').replace('0<=start<end<=60000', f'{start}<=start<end<={end}')
            prompt += '\n' + compact.JSON_CONTRACT + '\n人物背景与当前观察：\n' + json.dumps({'background': background, 'asr': observations, 'ocr': None}, ensure_ascii=False)
            prompt += '\n前文来自你实际生成的已完成块，仅辅助连续性；可能有错，当前证据优先，不能照抄为当前事件，也不能凭前文编造。只输出当前区间。\n' + json.dumps(history, ensure_ascii=False)
            (folder / 'prompt.txt').write_text(prompt)
            save(folder / 'history.json', history)
            save(folder / 'asr.json', observations)
            frame_rows = frames_by_chunk[seq]
            selected = [args.out / row['file'] for row in frame_rows]
            save(folder / 'frames.json', frame_rows)
            began = time.monotonic()
            content = []
            for row, path in zip(frame_rows, selected):
                content.extend([{'type': 'input_text', 'text': f"timestamp_ms={row['timestamp_ms']}"},
                    {'type': 'input_image', 'image_url': encode_media_file(path, expected_kind='image')[0], 'detail': 'high'}])
            content.append({'type': 'input_text', 'text': prompt})
            summary = {'chunk': seq, 'bounds_ms': [start, end], 'valid': False, 'frame_count': 180,
                       'thinking': 'disabled', 'max_output_tokens': 30000, 'history_chars': len(json.dumps(history, ensure_ascii=False))}
            save(folder / 'request-meta.json', summary)
            pieces, terminal, first = [], None, None
            emit('b_started', chunk=seq)
            signal.setitimer(signal.ITIMER_REAL, 600)
            try:
                with OpenAI(base_url=ARK_OPENAI_BASE_URL, api_key=os.environ['ARK_API_KEY'], max_retries=0, timeout=httpx.Timeout(90, connect=30)) as client:
                    with client.responses.create(model='doubao-seed-2-1-pro-260628', input=[{'role':'user', 'content':content}],
                            max_output_tokens=30000, extra_body={'thinking':{'type':'disabled'}}, stream=True) as stream:
                        with (folder / 'events.jsonl').open('w') as trace:
                            for event in stream:
                                elapsed = time.monotonic() - began
                                trace.write(json.dumps({'t':elapsed, 'event':event.model_dump(mode='json')}, ensure_ascii=False) + '\n')
                                trace.flush()
                                if event.type == 'response.output_text.delta' and event.delta:
                                    first = elapsed if first is None else first
                                    pieces.append(event.delta)
                                if event.type in ('response.completed','response.failed','response.incomplete'):
                                    terminal = event.response
                                    save(folder / 'terminal.json', terminal.model_dump(mode='json'))
                                    break
                if terminal is None or terminal.status != 'completed':
                    raise ValueError('No completed provider terminal')
                raw = ''.join(pieces)
                final_text = ''.join(p.text for item in terminal.output if item.type == 'message' for p in item.content if p.type == 'output_text')
                if raw != final_text:
                    raise ValueError('Stream and terminal text differ')
                summary['complete_response'] = True
                value = json.loads(raw, object_pairs_hook=compact._json_object)
                if (not isinstance(value, dict) or not isinstance(value.get('timeline'), list)
                        or any(not isinstance(e, dict) or not isinstance(e.get('text'), str) for e in value['timeline'])):
                    raise ValueError('Completed output has no usable timeline for serial history')
                # Retain the full original object even if strict field validation fails.
                # This is evidence, not a repaired/accepted result. The next request
                # explicitly treats all previous generated facts as uncertain.
                save(folder / 'raw.json', value)
                results.append(value)
                summary.update(history_usable=True, events=len(value['timeline']))
                result = compact.decode_compact_result(raw, output_format='json', start_ms=start, end_ms=end)
                save(folder / 'result.json', result.model_dump(mode='json'))
                summary['valid'] = True
            except Exception as exc:
                summary['error'] = str(exc)[:2000]
            finally:
                signal.setitimer(signal.ITIMER_REAL, 0)
                (folder / 'raw.txt').write_text(''.join(pieces))
                if terminal is not None:
                    summary.update(terminal_status=terminal.status, returned_model=terminal.model,
                                   usage=terminal.usage.model_dump(mode='json') if terminal.usage else None)
                summary.update(total_seconds=time.monotonic()-began, ttft_seconds=first)
                save(folder / 'summary.json', summary)
                summaries.append(summary)
                save(out / 'summary.json', {'elapsed_seconds':time.monotonic()-overall, 'chunks':summaries})
                emit('b_complete', **summary)
            if not summary.get('history_usable'):
                emit('b_stopped', reason='Continuous history unavailable; retain failure, no automatic paid repair')
                break
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase', choices=['prepare', 'start-storage', 'run-a', 'run-b', 'stop-storage'])
    parser.add_argument('--source-root', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    args.out = args.out.resolve()
    args.source_root = args.source_root.resolve()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s %(message)s')
    logging.getLogger('elastic_transport').setLevel(logging.ERROR)
    if args.phase != 'prepare':
        environment(args)
    if args.phase == 'stop-storage':
        local_services(args).stop(('embedding', 'elasticsearch', 'mysql'))
    else:
        globals()[args.phase.replace('-', '_')](args)


if __name__ == '__main__':
    main()
