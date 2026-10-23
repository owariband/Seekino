"""Reconcile the user-stopped run from frozen artifacts; never requests inference."""
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import re
import statistics

from audit_full import audit_row, read_rows, usage_totals
from tag_vlm import OUT, write_json


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    paths = ['full/frames.jsonl', 'full/missing.jsonl', 'full/manifest.json',
             'pilot/primary_a.jsonl', 'pilot/primary_a-summary.json',
             'pilot/full.jsonl', 'pilot/full-summary.json', 'prompt/shot-tag-v1.md', 'schema_models.py']
    frozen = {path: sha(OUT / path) for path in paths}
    full, pilot = read_rows(OUT / 'pilot/full.jsonl'), read_rows(OUT / 'pilot/primary_a.jsonl')
    source, missing = read_rows(OUT / 'full/frames.jsonl'), read_rows(OUT / 'full/missing.jsonl')
    source_by_frame = {row['frame_id']: row for row in source}
    original_summary = json.loads((OUT / 'pilot/full-summary.json').read_text())
    binding = json.loads((OUT / 'pilot/primary_a-summary.json').read_text())['binding']
    prompt = (OUT / 'prompt/shot-tag-v1.md').read_text()
    system, user = re.findall(r'```text\n(.*?)\n```', prompt, flags=re.S)
    errors, events, verified = [], Counter(), set()
    for row in full + pilot:
        try:
            frame = source_by_frame[row['frame_id']]
            if row['sha256'] not in verified:
                from pathlib import Path
                if sha(Path(frame['image_path'])) != row['sha256']:
                    raise ValueError('Source JPEG SHA mismatch')
                verified.add(row['sha256'])
            events.update(audit_row(row, frame, system, user, frozen['prompt/shot-tag-v1.md'], binding))
            if row['run_id'] == 'full':
                if json.loads((OUT / 'pilot/full' / f"{row['frame_id']}.json").read_text()) != row:
                    raise ValueError('Per-frame completed record differs from aggregate')
        except Exception as error:
            errors.append(dict(run_id=row['run_id'], frame_id=row['frame_id'], type=type(error).__name__, error=str(error)))
    per_frame_paths = list((OUT / 'pilot/full').glob('*.json'))
    if {p.stem for p in per_frame_paths} != {row['frame_id'] for row in full}:
        errors.append(dict(error='Completed per-frame files differ from full aggregate coverage'))
    completed_paths = {attempt['raw_path'] for row in full for attempt in row['attempts']}
    raw_paths = list((OUT / 'raw').glob('*/shot-tag-v1/full/*/attempt-*'))
    interrupted = []
    for directory in sorted(raw_paths):
        relative = str(directory.relative_to(OUT))
        if relative in completed_paths:
            continue
        request = json.loads((directory / 'request.json').read_text())
        event_rows = read_rows(directory / 'events.jsonl') if (directory / 'events.jsonl').exists() else []
        methods = [event.get('payload', {}).get('method') for event in event_rows]
        usage_events = [event for event in event_rows if event.get('payload', {}).get('method') == 'thread/tokenUsage/updated']
        unexpected = [name for name in ('call.json', 'response.txt', 'response.json', 'parsed.json') if (directory / name).exists()]
        if unexpected:
            errors.append(dict(raw_path=relative, error='Unaggregated attempt contains a terminal artifact', files=unexpected))
        interrupted.append(dict(frame_id=directory.parent.name, sha256=request['image_sha256'], raw_path=relative,
                                status='interrupted_unresolved', response_available=False, parsed_available=False,
                                turn_start_observed='turn/started' in methods, turn_completion_observed='turn/completed' in methods,
                                first_event_at=event_rows[0]['timestamp'] if event_rows else None,
                                last_event_at=event_rows[-1]['timestamp'] if event_rows else None,
                                last_event_method=methods[-1] if methods else None,
                                observed_usage_events=usage_events, terminal_usage_available=False,
                                retained_file_sha256={p.name: sha(p) for p in sorted(directory.iterdir()) if p.is_file()}))
    if frozen != {path: sha(OUT / path) for path in paths}:
        errors.append(dict(error='A frozen source changed during reconciliation'))
    attempts = [attempt for row in full for attempt in row['attempts']]
    cache = {row['sha256']: row for row in full + pilot}
    covered = [row for row in source if row['sha256'] in cache]
    last_completed = max(attempt['finished_at'] for attempt in attempts)
    last_raw = max([last_completed] + [row['last_event_at'] for row in interrupted if row['last_event_at']])
    now = datetime.now(timezone.utc).isoformat()
    pilot_report = json.loads((OUT / 'pilot/report.json').read_text())
    known_cost = pilot_report['experimental_usage_totals']['configured_cost_cny']['known_subtotal']
    runs = ['primary_smoke', 'cheap_smoke', 'primary_a', 'primary_b', 'cheap', 'full']
    run_rows = {run: read_rows(OUT / 'pilot' / f'{run}.jsonl') for run in runs}
    all_rows = [row for rows in run_rows.values() for row in rows]
    timing = dict(full_run_started_at=original_summary['started_at'], stop_timestamp_utc=None,
                  stop_timestamp_note='The terminating command did not record a UTC timestamp; no timestamp is inferred.',
                  last_completed_call_at=last_completed, last_retained_raw_event_at=last_raw, reconciled_at=now,
                  original_summary_last_progress_wall_seconds=original_summary['wall_seconds'],
                  completed_attempt_latency_sum_seconds=sum(a['latency_seconds'] for a in attempts),
                  completed_attempt_latency_median_seconds=statistics.median(a['latency_seconds'] for a in attempts),
                  completed_attempt_latency_max_seconds=max(a['latency_seconds'] for a in attempts))
    costs = dict(actual_total_cny=None, actual_total_usd=None, configured_known_cheap_estimate_cny=known_cost,
                 primary_configured_cost_cny=None, incomplete_attempt_cost=None,
                 note='Configured cheap-model estimate only, not an invoice. Codex/gpt-5.6-sol is unpriced in project configuration. Interrupted attempts have no terminal usage and are not counted as zero-cost.',
                 pricing_source=pilot_report['pricing_source'], estimate_source='pilot/report.json')
    receipt = dict(status='interrupted_by_user', integrity_status='passed' if not errors else 'failed',
                   user_instruction='现在就停止新增标注，用已完成数据做探索，并注明集数偏差',
                   control=dict(reported_runner_pid=88773, reported_runner_exit_code=143,
                                action='Root froze dispatch, terminated the ten owned inference children, then terminated/resumed the frozen runner. The observer was separately stopped.',
                                no_new_calls_after_stop=True, no_retries_or_replay=True),
                   original_target_frames=len(missing), original_source_keyframes=len(source),
                   original_source_shots=len({row['shot_id'] for row in source}), completed_full_rows=len(full),
                   selected_pilot_rows=len(pilot), completed_valid_unique_sha256=len(cache),
                   raw_full_attempts=len(raw_paths), completed_full_attempts=len(attempts),
                   interrupted_raw_attempts=len(interrupted), interrupted_attempts=interrupted,
                   not_started_unique_sha256=len(missing)-len(full)-len(interrupted),
                   additional_completed_results_recovered=0, unresolved_attempts_retained=True,
                   frozen_original_summary_status=original_summary['status'], original_summary_preserved=True,
                   timing=timing, costs=costs, full_completed_usage=usage_totals(full),
                   all_experiment_completed_usage=usage_totals(all_rows),
                   all_experiment_completed_attempts=sum(len(row['attempts']) for row in all_rows),
                   per_run_completed_usage={run: usage_totals(rows) for run, rows in run_rows.items()},
                   incomplete_usage_note='No interrupted attempt has a terminal token usage record; completed token totals exclude these unknown amounts.',
                   source_hashes=frozen, artifact_audit_errors=errors)
    write_json(OUT / 'full/interruption.json', receipt)
    audit = dict(status='interrupted_by_user', integrity_status='passed' if not errors else 'failed',
                 audit_scope='Completed primary_a cache plus completed full-run records; original target remains unfinished.',
                 original_target_frames=len(missing), original_source_keyframes=len(source),
                 original_source_shots=len({row['shot_id'] for row in source}),
                 completed=len(full), completed_full_rows=len(full), audited_rows=len(full),
                 selected_pilot_audited_rows=len(pilot), audited_unique_sha256=len(verified),
                 status_counts=dict(Counter(row['status'] for row in full)), artifact_audit_errors=errors,
                 runner_error_frames=[row['frame_id'] for row in full if row.get('runner_error')],
                 recorded_attempts=len(attempts), retried_frames=sum(len(row['attempts']) > 1 for row in full),
                 invalid_attempts=sum(a['status']=='invalid' for a in attempts),
                 schema_invalid_causes={}, api_error_causes={}, codex_event_counts=dict(events),
                 interrupted_raw_attempts=len(interrupted), interrupted_raw_paths=[row['raw_path'] for row in interrupted],
                 no_new_calls_or_replay=True, covered_source_keyframes=len(covered),
                 valid_source_keyframes=sum(cache[row['sha256']]['status']=='valid' for row in covered),
                 original_source_coverage=len(covered)/len(source), full_run_usage=usage_totals(full),
                 all_experiment_usage=receipt['per_run_completed_usage'], timing=timing, costs=costs,
                 verification_contract=['Original JPEG SHA matches source manifest.', 'Untouched response parses strictly without repairs or duplicate-key acceptance.',
                                        'Strict response == parsed.json == result tags.', 'Exact system/user/schema/model/reasoning/sampling binding.',
                                        'Raw call metadata == aggregate attempt metadata.', 'Completed per-frame files == frozen aggregate.',
                                        'No unexpected tool items; interrupted attempts remain unparsed and unconsumed.'],
                 source_hashes={**frozen, 'full/interruption.json': sha(OUT / 'full/interruption.json')})
    write_json(OUT / 'full/audit-final.json', audit)
    print(json.dumps({key: audit[key] for key in ['status','integrity_status','completed_full_rows','selected_pilot_audited_rows','audited_unique_sha256','covered_source_keyframes','interrupted_raw_attempts','artifact_audit_errors']}))
    print(json.dumps(dict(full_completed_usage=receipt['full_completed_usage'], all_experiment_completed_usage=receipt['all_experiment_completed_usage'], all_experiment_completed_attempts=receipt['all_experiment_completed_attempts'], timing=timing, costs=costs)))
    return int(bool(errors))


if __name__ == '__main__':
    raise SystemExit(main())
