"""Continue only uncommitted intervals after the configured API is funded again."""

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import time


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
    assert [row['sequence_no'] for row in rows] == list(range(24))
    receipts = [read(path) for path in (directory / 'intervals').glob('*.json')]
    completed = sorted(receipt['start_sequence_no'] for receipt in receipts)
    pending = [index for index in range(22) if index not in completed]
    assert len(set(completed)) == len(completed)
    frozen = read(directory / 'frozen-sixteen.json')
    ordered = sorted(rows, key=lambda row: row['commit_order'])
    assert frozen['chunk_refs'] == [row['chunk_task_id'] for row in ordered[:16]]
    for row in ordered[:16]:
        digest = hashlib.sha256(json.dumps(row['content'], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        assert frozen['content_sha256'][row['chunk_task_id']] == digest
    print(json.dumps({'case': args.case, 'understanding_retained': 24,
                      'committed_intervals_retained': completed, 'pending_intervals': pending}), flush=True)
    if args.inspect_only:
        return
    script = Path(__file__).resolve().with_name('live-ab.py')
    spec = importlib.util.spec_from_file_location('live_ab', script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.sha(Path(module.parse_prompts.__file__)) == manifest['prompt_source_sha256']
    repo = script.resolve().parents[5]
    guarded = ['agent/video_parse_v2', str(script.relative_to(repo)), str(Path(__file__).resolve().relative_to(repo))]
    assert not subprocess.check_output(['git', 'diff', '--name-only', 'HEAD', '--', *guarded], cwd=repo, text=True).strip()
    baseline = root / 'outputs/verification/ep04-offscreen-flash-low-20261114/private'
    assert module.sha(baseline / 'model-config.yaml') == manifest['model_config_sha256']
    old_call = read(next((baseline / 'refine-calls').glob('*.input.json')))
    body, _ = json.JSONDecoder().raw_decode(old_call['prompt'].split('Input: ', 1)[1])
    inputs = module.ParseInput(series_id='d622007b-858b-5f18-ac72-a134d86f689e',
        video_id='5df2f69f-6644-5a2d-ab37-776ece409aa4', parse_config=manifest['parse_config'])
    gateway = module.GatewayClient(module.load_config(baseline / 'model-config.yaml'))
    case = module.Experiment(name=args.case, shared=root, inputs=inputs, background=body['background'],
        media=[], gateway=gateway,
        audio=read(root / 'outputs/verification/audio-first-prefill-evaluation-20261115/private/audio-first-artifact.json'),
        output=directory, semaphores=(asyncio.Semaphore(10), asyncio.Semaphore(10)))
    previous = read(directory / 'progress.json')
    case.task_id = previous['task_id']
    case.refs = [row['chunk_task_id'] for row in rows]
    case.sequences = {ref: sequence for sequence, ref in enumerate(case.refs)}
    case.commit_order = [row['chunk_task_id'] for row in ordered]
    retained = read(directory / 'final-contents.json')
    for row in rows:
        ref = row['chunk_task_id']
        current = retained[str(row['sequence_no'])]
        case.initial[ref] = module.VideoParseResult.model_validate(row['content'])
        case.contents[ref] = module.VideoParseResult.model_validate(current['content'])
        case.evidence[ref], case.versions[ref], case.scopes[ref] = row['evidence'], current['version'], current['scope']
    case.frozen_anchors = read(directory / 'frozen-anchors.json')
    assert all(anchor['chunk_task_id'] in frozen['chunk_refs'] for anchor in case.frozen_anchors)
    case.named_lines = read(directory / 'named-lines.json')
    case.named_ready, case.refined, case.query_lock = True, list(completed), asyncio.Lock()
    for path in sorted((directory / 'calls').glob('*.input.json')):
        call = read(path)
        outcome_path = path.with_name(path.name.replace('.input.json', '.outcome.json'))
        outcome = read(outcome_path) if outcome_path.exists() else {'status': 'interrupted'}
        case.calls.append({key: value for key, value in {**call, **outcome}.items()
                           if key not in ('inputs', 'binding', 'result')})
    history = directory / 'balance-recovery-history.json'
    attempts = read(history) if history.exists() else []
    attempts.append({'started_utc': datetime.now(timezone.utc).isoformat(),
                     'previous_progress': previous, 'retained_intervals': completed, 'pending_intervals': pending,
                     'resume_source_sha256': module.sha(Path(__file__)),
                     'source_revision': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=repo, text=True).strip(),
                     'new_understanding_calls': 0, 'frozen_sixteen_unchanged': True})
    module.save(history, attempts)
    original_generate = case._generate_multimodal
    balance_exhausted = False

    async def generate(*positional, **keywords):
        nonlocal balance_exhausted
        if balance_exhausted:
            raise RuntimeError('Balance exhausted; no further provider requests in this attempt')
        try:
            return await original_generate(*positional, **keywords)
        except Exception as error:
            if 'Insufficient Balance' in str(error):
                balance_exhausted = True
            raise

    case._generate_multimodal = generate
    started = time.monotonic()
    case.progress()
    try:
        await asyncio.gather(*(case.run_refine(index) for index in pending))
        assert len(case.refined) == 22 and not case.errors, case.errors
        module.save(directory / 'complete.json', {'case': case.name, 'understanding': 24, 'refine': 22,
            'resume_elapsed_seconds': time.monotonic() - started, 'asr_calls': 0, 'gpu_calls': 0,
            'database_calls': 0, 'calls': case.calls, 'resumed_after_balance_error': True,
            'retained_intervals': completed, 'resumed_intervals': pending})
        print(case.name + ' COMPLETE', flush=True)
    finally:
        if case.encoder is not None:
            case.encoder.close()
        gateway.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--data-root', type=Path, required=True)
    parser.add_argument('--run-name', default='audio-first-prefill-prompt-synced-20261115')
    parser.add_argument('--case', choices=['B_two_thirds'], default='B_two_thirds')
    parser.add_argument('--inspect-only', action='store_true')
    asyncio.run(main(parser.parse_args()))
