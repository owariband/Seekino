"""Finish both live cases from their own new Understanding after a host scope fix."""

import argparse
import asyncio
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import shutil
import time


def read(path):
    return json.loads(path.read_text())


async def main(root, check_only):
    script = Path(__file__).with_name('live-ab.py')
    spec = importlib.util.spec_from_file_location('live_ab', script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    private = root / 'outputs/verification/audio-first-prefill-live-ab-20261115/private'
    baseline = root / 'outputs/verification/ep04-offscreen-flash-low-20261114/private'
    manifest = read(private / 'manifest.json')
    inputs = module.ParseInput(series_id='d622007b-858b-5f18-ac72-a134d86f689e',
        video_id='5df2f69f-6644-5a2d-ab37-776ece409aa4',
        parse_config=module.ParseConfig.model_validate(manifest['parse_config']))
    old_call = read(next((baseline / 'refine-calls').glob('*.input.json')))
    body, _ = json.JSONDecoder().raw_decode(old_call['prompt'].split('Input: ', 1)[1])
    audio = read(root / 'outputs/verification/audio-first-prefill-evaluation-20261115/private/audio-first-artifact.json')
    gateway = None if check_only else module.GatewayClient(module.load_config(baseline / 'model-config.yaml'))
    semaphores = asyncio.Semaphore(10), asyncio.Semaphore(10)
    cases = []
    for name in ('A_audio_only', 'B_two_thirds'):
        directory = private / name
        assert not (directory / 'complete.json').exists()
        rows = [read(path) for path in sorted((directory / 'understanding').glob('*.json'))]
        assert [row['sequence_no'] for row in rows] == list(range(24))
        progress = read(directory / 'progress.json')
        case = module.Experiment(name=name, shared=root, inputs=inputs, background=body['background'],
            media=[], gateway=gateway, audio=audio, output=directory, semaphores=semaphores)
        case.task_id = progress['task_id']
        case.refs = [row['chunk_task_id'] for row in rows]
        case.sequences = {ref: seq for seq, ref in enumerate(case.refs)}
        case.commit_order = [row['chunk_task_id'] for row in sorted(rows, key=lambda row: row['commit_order'])]
        for row in rows:
            ref = row['chunk_task_id']
            content = module.deepcopy(row['content'])
            # Mirror the production host-owned commit stamp; model answers remain intact.
            for event in content['timeline']:
                if event['event_type'] == 'DIALOGUE':
                    event['speaker_source'] = 'understanding'
            case.initial[ref] = module.VideoParseResult.model_validate(content)
            case.contents[ref] = case.initial[ref].model_copy(deep=True)
            case.evidence[ref], case.versions[ref], case.scopes[ref] = row['evidence'], 1, [row['sequence_no']]
        case.query_lock = asyncio.Lock()
        if name == 'B_two_thirds':
            frozen = read(directory / 'frozen-sixteen.json')
            assert frozen['chunk_refs'] == case.commit_order[:16]
            case.frozen_anchors = read(directory / 'frozen-anchors.json')
            assert all(anchor['chunk_task_id'] in frozen['chunk_refs'] for anchor in case.frozen_anchors)
            case.named_lines = read(directory / 'named-lines.json')
            case.named_ready = True
        if check_only:
            # Replay the actual failed B interval against its retained merged scopes.
            if name == 'B_two_thirds':
                for seq, retained in read(directory / 'final-contents.json').items():
                    ref = case.refs[int(seq)]
                    case.contents[ref] = module.VideoParseResult.model_validate(retained['content'])
                    case.scopes[ref] = retained['scope']
                    case.versions[ref] = retained['version']
                snapshot = await case.load_refine_input(f'{case.task_id}-20')
                from agent.video_parse_v2.timelineEvent_canvas import ParseContextCanvas
                canvas = ParseContextCanvas.from_refine(snapshot)
                canvas.accept_refinement({'members': [{'sequence_no': case.sequences[ref],
                    **canvas.result(ref).model_dump(mode='json')} for ref in snapshot['member_contents']]})
                print('Actual merged-scope incident passes production Canvas and result gate', flush=True)
            continue
        archive = directory / 'attempt-before-scope-fix'
        assert not archive.exists(), 'Do not archive twice or rerun successful model work silently'
        archive.mkdir()
        for path in list((directory / 'calls').glob('*.input.json')):
            call = read(path)
            if call['schema_name'] == 'IntervalRefinementResult':
                for item in (path, path.with_name(path.name.replace('.input.json', '.outcome.json'))):
                    if item.exists():
                        shutil.move(item, archive / item.name)
            else:
                outcome = read(path.with_name(path.name.replace('.input.json', '.outcome.json')))
                case.calls.append({key: value for key, value in {**call, **outcome}.items()
                                   if key not in ('inputs', 'binding', 'result')})
        for name_to_archive in ('progress.json', 'final-contents.json', 'recovery.json'):
            path = directory / name_to_archive
            if path.exists():
                shutil.move(path, archive / path.name)
        module.save(directory / 'recovery.json', {
            'resumed_utc': datetime.now(timezone.utc).isoformat(), 'attempt': 2,
            'cause': 'File host used nominal member bounds after earlier Refine had expanded member scopes.',
            'fix': 'Mirror production expanded member bounds, fixed write window and consulted-scope union; reserve complete member sets atomically.',
            'current_run_understanding_reused': 24, 'new_understanding_calls': 0,
            'frozen_sixteen_unchanged': name == 'B_two_thirds', 'script_sha256': module.sha(script),
            'limits': 'Retried Refine from these new initial answers; earlier attempts retained. No early-release latency claim.',
        })
        cases.append(case)
    if check_only:
        return

    async def finish(case):
        case.started = time.monotonic()
        case.progress()
        case.schedule_refine()
        await asyncio.gather(*case.refine_jobs.values())
        assert len(case.refined) == 22 and not case.errors, case.errors
        module.save(case.output / 'complete.json', {'case': case.name, 'understanding': 24, 'refine': 22,
            'refine_resume_elapsed_seconds': time.monotonic() - case.started,
            'asr_calls': 0, 'gpu_calls': 0, 'database_calls': 0,
            'calls': case.calls, 'recovered': True, 'completed_utc': datetime.now(timezone.utc).isoformat()})
        print(case.name + ' COMPLETE', flush=True)

    try:
        outcomes = await asyncio.gather(*(finish(case) for case in cases), return_exceptions=True)
        for case, outcome in zip(cases, outcomes):
            if isinstance(outcome, BaseException):
                print(case.name, repr(outcome), flush=True)
        assert not any(isinstance(outcome, BaseException) for outcome in outcomes), outcomes
    finally:
        for case in cases:
            if case.encoder is not None:
                case.encoder.close()
        gateway.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--data-root', type=Path, required=True)
    parser.add_argument('--check-only', action='store_true')
    args = parser.parse_args()
    asyncio.run(main(args.data_root.resolve(), args.check_only))
