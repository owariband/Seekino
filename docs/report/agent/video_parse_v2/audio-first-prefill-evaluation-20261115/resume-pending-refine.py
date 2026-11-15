"""Continue unfinished EP04 work from its own file checkpoints after API failures."""

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import time
from uuid import uuid4


def read(path):
    return json.loads(path.read_text())


async def main(args):
    root = args.data_root.resolve()
    assert Path(args.run_name).name == args.run_name
    private = root / 'outputs/verification' / args.run_name / 'private'
    manifest = read(private / 'manifest.json')
    directory = private / args.case
    assert not (directory / 'complete.json').exists(), 'This case is already complete'
    rows = [read(path) for path in sorted((directory / 'understanding').glob('*.json'))]
    sequences = [row['sequence_no'] for row in rows]
    assert len(sequences) == len(set(sequences)) and all(0 <= value < 24 for value in sequences)
    missing = sorted(set(range(24)) - set(sequences))
    receipts = [read(path) for path in (directory / 'intervals').glob('*.json')]
    completed = sorted(receipt['start_sequence_no'] for receipt in receipts)
    pending = sorted(set(range(22)) - set(completed))
    assert len(set(completed)) == len(completed)
    ordered = sorted(rows, key=lambda row: row['commit_order'])
    frozen_path = directory / 'frozen-sixteen.json'
    frozen = read(frozen_path) if frozen_path.exists() else None
    if frozen:
        assert frozen['chunk_refs'] == [row['chunk_task_id'] for row in ordered[:16]]
        for row in ordered[:16]:
            digest = hashlib.sha256(json.dumps(row['content'], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            assert frozen['content_sha256'][row['chunk_task_id']] == digest
    print(json.dumps({'case': args.case, 'understanding_retained':len(rows),
        'pending_understanding':missing,'committed_intervals_retained':completed,'pending_intervals':pending}),flush=True)
    if args.inspect_only:
        return
    script = Path(__file__).resolve().with_name('live-ab.py')
    spec = importlib.util.spec_from_file_location('live_ab', script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.sha(Path(module.parse_prompts.__file__)) == manifest['prompt_source_sha256']
    repo = script.resolve().parents[5]
    guarded = ['agent/video_parse_v2',str(script.relative_to(repo)),str(Path(__file__).resolve().relative_to(repo))]
    assert not subprocess.check_output(['git','diff','--name-only','HEAD','--',*guarded],cwd=repo,text=True).strip()
    baseline = root / 'outputs/verification/ep04-offscreen-flash-low-20261114/private'
    assert module.sha(baseline/'model-config.yaml') == manifest['model_config_sha256']
    audio_path = root/'outputs/verification'/args.audio_run_name/'private/audio-first-artifact.json'
    assert module.sha(audio_path) == manifest['audio_artifact_sha256']
    audio = read(audio_path)
    old_call = read(next((baseline/'refine-calls').glob('*.input.json')))
    body,_ = json.JSONDecoder().raw_decode(old_call['prompt'].split('Input: ',1)[1])
    inputs = module.ParseInput(series_id='d622007b-858b-5f18-ac72-a134d86f689e',
        video_id='5df2f69f-6644-5a2d-ab37-776ece409aa4',parse_config=manifest['parse_config'])
    gateway = module.GatewayClient(module.load_config(baseline/'model-config.yaml'))
    # Recovery bounds the total simultaneous model requests, retaining the model and prompt.
    semaphore = asyncio.Semaphore(4)
    case = module.Experiment(name=args.case,shared=root,inputs=inputs,background=body['background'],
        media=[],gateway=gateway,audio=audio,output=directory,semaphores=(semaphore,semaphore))
    previous = read(directory/'progress.json')
    case.task_id = previous['task_id']
    for row in rows:
        case.refs[row['sequence_no']] = row['chunk_task_id']
    case.sequences = {ref:seq for seq,ref in enumerate(case.refs)}
    case.commit_order = [row['chunk_task_id'] for row in ordered]
    retained_path = directory/'final-contents.json'
    retained = read(retained_path) if retained_path.exists() else {}
    for row in rows:
        ref = row['chunk_task_id']
        current = retained.get(str(row['sequence_no']),{'content':row['content'],'version':1,'scope':[row['sequence_no']]})
        case.initial[ref] = module.VideoParseResult.model_validate(row['content'])
        case.contents[ref] = module.VideoParseResult.model_validate(current['content'])
        case.evidence[ref],case.versions[ref],case.scopes[ref] = row['evidence'],current['version'],current['scope']
    if frozen:
        anchors_path = directory/'frozen-anchors.json'
        if anchors_path.exists():
            case.frozen_anchors = read(anchors_path)
            assert all(anchor['chunk_task_id'] in frozen['chunk_refs'] for anchor in case.frozen_anchors)
            case.named_ready = True
        named_path = directory/'named-lines.json'
        case.named_lines = read(named_path) if named_path.exists() else {}
    case.refined,case.query_lock = list(completed),asyncio.Lock()
    for index in completed:
        done = asyncio.get_running_loop().create_future()
        done.set_result(None)
        case.refine_jobs[index] = done
    for path in sorted((directory/'calls').glob('*.input.json')):
        call = read(path)
        outcome_path = path.with_name(path.name.replace('.input.json','.outcome.json'))
        outcome = read(outcome_path) if outcome_path.exists() else {'status':'interrupted'}
        case.calls.append({key:value for key,value in {**call,**outcome}.items()
                           if key not in ('inputs','binding','result')})
    history_path = directory/'execution-recovery-history.json'
    attempts = read(history_path) if history_path.exists() else []
    attempts.append({'started_utc':datetime.now(timezone.utc).isoformat(),
        'previous_progress':previous,'retained_intervals':completed,'pending_intervals':pending,
        'retained_understanding':sequences,'pending_understanding':missing,
        'retained_understanding_sha256':{path.name:module.sha(path) for path in (directory/'understanding').glob('*.json')},
        'frozen_sixteen_unchanged':bool(frozen),'maximum_simultaneous_model_requests':4,
        'stream_override':False if args.non_streaming else None,
        'source_revision':subprocess.check_output(['git','rev-parse','HEAD'],cwd=repo,text=True).strip(),
        'resume_source_sha256':module.sha(Path(__file__))})
    module.save(history_path,attempts)
    original_understand = case.run_understanding
    async def understand(ref):
        if ref not in case.initial:
            await original_understand(ref)
    case.run_understanding = understand
    original_generate = case._generate_multimodal
    balance_exhausted = False
    async def generate(*positional,**keywords):
        nonlocal balance_exhausted
        if balance_exhausted:
            raise RuntimeError('Balance exhausted; no further provider requests in this attempt')
        if args.non_streaming:
            positional=(positional[0].model_copy(update={'stream':False}),*positional[1:])
        try:
            return await original_generate(*positional,**keywords)
        except Exception as error:
            if 'Insufficient Balance' in str(error):
                balance_exhausted = True
            raise
    case._generate_multimodal = generate
    context = None
    started = time.monotonic()
    try:
        if missing:
            source = next((root/'assets/library/verification/ep04-prefill-context-20261114').rglob('original/*.mp4'))
            assert module.sha(source) == manifest['source_sha256']
            media_id = str(uuid4())
            context = module.VideoParseMediaService(directory/'recovery-media').prepare_episode(
                module.MediaLeaseIdentity(media_id,media_id,1,'video'),source_video=source,
                duration_ms=1420127,chunk_duration=60,visual_mode='ordered_frames',frame_fps=1,
                max_height=720,ocr_enabled=False,overlap_ms=3000,chunk_video_required=False,check_active=lambda:None)
            media_manifest = await asyncio.to_thread(context.__enter__)
            media_rows = read(media_manifest)['chunks']
            case.media = [module.PreparedVideoParseMedia(None,media_manifest.parent/item['audio_path'],
                tuple((stamp,media_manifest.parent/path) for stamp,path in item['frames']),
                visual_start_ms=item['visual_start_ms'],visual_end_ms=item['visual_end_ms']) for item in media_rows]
        if frozen and not case.named_ready:
            await case.prepare_named(frozen['chunk_refs'])
        await case.run()
        result = read(directory/'complete.json')
        result.update(resumed_after_api_error=True,resume_elapsed_seconds=time.monotonic()-started,
            retained_intervals=completed,resumed_intervals=pending,new_understanding_chunks=missing)
        module.save(directory/'complete.json',result)
        print(args.case+' COMPLETE',flush=True)
    finally:
        if context is not None:
            await asyncio.to_thread(context.__exit__,None,None,None)
        if case.encoder is not None:
            case.encoder.close()
        gateway.close()


if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--data-root',type=Path,required=True)
    parser.add_argument('--run-name',default='audio-first-prefill-b0-live-ab-20261115')
    parser.add_argument('--audio-run-name',default='audio-first-prefill-asr-id-b0-20261115')
    parser.add_argument('--case',choices=['A_audio_only','B_two_thirds'],default='B_two_thirds')
    parser.add_argument('--inspect-only',action='store_true')
    parser.add_argument('--non-streaming',action='store_true')
    asyncio.run(main(parser.parse_args()))
