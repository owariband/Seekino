"""Observe the full run, audit newly completed artifacts, and publish progress only."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import time

from tag_vlm import OUT, ShotTag, strict_json, write_json


def read_rows(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def usage_totals(rows):
    totals, units = Counter(), 0
    for row in rows:
        for attempt in row.get('attempts', []):
            for call in attempt.get('usage', []):
                units += 1
                for key, value in (call.get('usage') or {}).items():
                    if isinstance(value, int) and not isinstance(value, bool):
                        totals[key] += value
    return dict(reported_usage_records=units, tokens=dict(totals))


def audit_row(row, source, system, user, prompt_sha, binding):
    event_counts = Counter()
    if row['sha256'] != source['sha256'] or row['model'] != binding['model']:
        raise ValueError('Source image/model mismatch')
    if row.get('runner_error'):
        return event_counts
    if row['prompt_sha256'] != prompt_sha or row['source'] != binding['source']:
        raise ValueError('Prompt/source mismatch')
    attempts = row['attempts']
    if not 1 <= len(attempts) <= 2 or attempts[-1]['status'] != row['status']:
        raise ValueError('Invalid final attempt/status contract')
    sampling = dict(reasoning_effort='low', temperature=None, top_p=None, seed=None,
                    sampling_control='unavailable in Codex turn API')
    previous_errors = None
    for number, attempt in enumerate(attempts, 1):
        directory = OUT / attempt['raw_path']
        request = json.loads((directory / 'request.json').read_text())
        expected_user = user if number == 1 else user + '\n\nYour previous response failed validation. Return a complete corrected JSON object. Validation errors:\n' + json.dumps(previous_errors, ensure_ascii=False)
        if (request['system'] != system or request['user'] != expected_user
                or request['prompt_sha256'] != prompt_sha or request['binding'] != binding
                or request['image_sha256'] != source['sha256']
                or request['sampling'] != sampling or attempt['sampling'] != sampling
                or request['requested_schema'] != ShotTag.model_json_schema()):
            raise ValueError('Raw request image/prompt/schema/settings mismatch')
        if json.loads((directory / 'call.json').read_text()) != attempt:
            raise ValueError('Call metadata differs from aggregate row')
        if attempt['status'] == 'valid':
            parsed = ShotTag.model_validate(strict_json((directory / 'response.txt').read_text()), strict=True).model_dump(mode='json')
            if parsed != row['tags'] or parsed != json.loads((directory / 'parsed.json').read_text()):
                raise ValueError('Raw/parsed/final tag mismatch')
        elif attempt['status'] == 'invalid':
            previous_errors = json.loads((directory / 'validation-errors.json').read_text())
            if previous_errors != attempt['validation_errors']:
                raise ValueError('Invalid-response error metadata mismatch')
            try:
                ShotTag.model_validate(strict_json((directory / 'response.txt').read_text()), strict=True)
            except (ValueError, TypeError):
                pass
            else:
                raise ValueError('Response recorded invalid but passes strict validation')
        elif attempt['status'] != 'api_error':
            raise ValueError('Unknown attempt status')
        if number == 1 and len(attempts) == 2 and attempt['status'] != 'invalid':
            raise ValueError('Second attempt is allowed only for invalid schema/JSON')
        events_path = directory / 'events.jsonl'
        for event in read_rows(events_path) if events_path.exists() else []:
            if event['event'] != 'codex_notification':
                continue
            notification = event['payload']
            method = notification.get('method', '')
            if 'error' in method.lower():
                event_counts['provider_error_notifications'] += 1
            if method == 'item/completed':
                item_type = notification['payload']['item']['type']
                if item_type not in {'userMessage', 'reasoning', 'agentMessage'}:
                    raise ValueError(f'Unexpected Codex item type: {item_type}')
    if row['status'] != 'valid' and row.get('tags') is not None:
        raise ValueError('Nonvalid row contains tags')
    return event_counts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--watch', action='store_true')
    parser.add_argument('--interval', type=float, default=30)
    args = parser.parse_args()
    source = read_rows(OUT / 'full/frames.jsonl')
    missing = read_rows(OUT / 'full/missing.jsonl')
    expected = {row['frame_id']: row for row in missing}
    pilot = read_rows(OUT / 'pilot/primary_a.jsonl')
    selected_sha = {row['sha256'] for row in source}
    if len(expected) != len(missing) or len({row['sha256'] for row in missing}) != len(missing):
        raise ValueError('Full manifest is not unique by frame ID and SHA')
    if {row['sha256'] for row in missing} & {row['sha256'] for row in pilot}:
        raise ValueError('Full calls overlap the selected pilot SHA cache')
    if selected_sha != {row['sha256'] for row in missing + pilot}:
        raise ValueError('Selected source coverage differs from pilot plus missing manifest')
    prompt = (OUT / 'prompt/shot-tag-v1.md').read_text()
    system, user = re.findall(r'```text\n(.*?)\n```', prompt, flags=re.S)
    prompt_sha = hashlib.sha256(prompt.encode()).hexdigest()
    binding = json.loads((OUT / 'pilot/primary_a-summary.json').read_text())['binding']
    summary = json.loads((OUT / 'pilot/full-summary.json').read_text())
    history = [(datetime.fromisoformat(summary['started_at']).timestamp(), 0)]
    seen, errors, last_count = {}, [], 0
    event_counts = Counter()
    image_verified = set()
    source_by_frame = {row['frame_id']: row for row in source}
    for row in pilot:
        try:
            image = source_by_frame[row['frame_id']]
            if hashlib.sha256(Path(image['image_path']).read_bytes()).hexdigest() != row['sha256']:
                raise ValueError('Selected pilot image SHA mismatch')
            image_verified.add(row['sha256'])
            event_counts.update(audit_row(row, image, system, user, prompt_sha, binding))
        except Exception as error:
            errors.append(dict(run_id='primary_a', frame_id=row['frame_id'], type=type(error).__name__, error=str(error)))
    while True:
        summary = json.loads((OUT / 'pilot/full-summary.json').read_text())
        rows = read_rows(OUT / 'pilot/full.jsonl') if (OUT / 'pilot/full.jsonl').exists() else []
        now = time.time()
        if len(rows) < last_count:
            errors.append(dict(error='Completed row count decreased', previous=last_count, current=len(rows)))
        last_count = len(rows)
        for row in rows:
            frame = row['frame_id']
            signature = hashlib.sha256(json.dumps(row, sort_keys=True).encode()).hexdigest()
            if frame in seen:
                if seen[frame] != signature:
                    errors.append(dict(frame_id=frame, error='Previously audited result changed'))
                continue
            try:
                if frame not in expected:
                    raise ValueError('Unexpected full-run frame ID')
                image = expected[frame]
                if image['sha256'] not in image_verified:
                    if hashlib.sha256(Path(image['image_path']).read_bytes()).hexdigest() != image['sha256']:
                        raise ValueError('Source JPEG SHA mismatch')
                    image_verified.add(image['sha256'])
                event_counts.update(audit_row(row, image, system, user, prompt_sha, binding))
            except Exception as error:
                errors.append(dict(frame_id=frame, type=type(error).__name__, error=str(error)))
            seen[frame] = signature
        history.append((now, len(rows)))
        while len(history) > 2 and history[1][0] < now - 600:
            history.pop(0)
        elapsed = max(now - history[0][0], .001)
        rate = (len(rows) - history[0][1]) / elapsed
        counts = Counter(row['status'] for row in rows)
        attempts = [a for row in rows for a in row.get('attempts', [])]
        invalid_causes, api_causes = Counter(), Counter()
        for attempt in attempts:
            if attempt['status'] == 'invalid':
                for error in attempt.get('validation_errors') or []:
                    invalid_causes['.'.join(map(str, error.get('loc', []))) + ':' + error.get('type', 'unknown')] += 1
            elif attempt['status'] == 'api_error':
                api_causes[(attempt.get('gateway_error') or {}).get('type', 'unknown')] += 1
        sha_results = {row['sha256']: row for row in pilot + rows}
        covered = [row for row in source if row['sha256'] in sha_results]
        valid_covered = sum(sha_results[row['sha256']]['status'] == 'valid' for row in covered)
        complete = summary['status'] == 'complete' and len(rows) == len(expected)
        report = dict(updated_at=datetime.now(timezone.utc).isoformat(), status='complete' if complete else 'running',
                      completed=len(rows), total=len(expected), status_counts=dict(counts),
                      monotonic_completed=not any(e.get('error') == 'Completed row count decreased' for e in errors),
                      audited_rows=len(seen), artifact_audit_errors=errors,
                      selected_pilot_audited_rows=len(pilot), audited_unique_sha256=len(image_verified),
                      throughput_window_seconds=round(elapsed, 3), throughput_frames_per_second=rate,
                      eta_seconds=(len(expected) - len(rows)) / rate if rate > 0 else None,
                      recorded_attempts=len(attempts), retried_frames=sum(len(r.get('attempts', [])) > 1 for r in rows),
                      invalid_attempts=sum(a['status'] == 'invalid' for a in attempts),
                      schema_invalid_causes=dict(invalid_causes), api_error_causes=dict(api_causes),
                      codex_event_counts=dict(event_counts),
                      runner_error_frames=[r['frame_id'] for r in rows if r.get('runner_error')],
                      full_run_usage=usage_totals(rows),
                      selected_source_keyframes=len(source), selected_source_shots=len({r['shot_id'] for r in source}),
                      covered_source_keyframes=len(covered), valid_source_keyframes=valid_covered,
                      selected_source_coverage=len(covered)/len(source), selected_source_valid_rate=valid_covered/len(source),
                      pricing=dict(currency='CNY', full_primary_cost=None,
                                   reason='Configured pricing has no Codex/gpt-5.6-sol price; null is unpriced, not zero.'))
        if complete:
            runs = ['primary_smoke', 'cheap_smoke', 'primary_a', 'primary_b', 'cheap', 'full']
            report['all_experiment_usage'] = {run: usage_totals(read_rows(OUT / 'pilot' / f'{run}.jsonl')) for run in runs if (OUT / 'pilot' / f'{run}.jsonl').exists()}
            hash_paths = ['full/frames.jsonl', 'full/missing.jsonl', 'pilot/primary_a.jsonl',
                          'pilot/primary_a-summary.json', 'pilot/full.jsonl', 'pilot/full-summary.json',
                          'prompt/shot-tag-v1.md', 'schema_models.py']
            report['source_hashes'] = {path: hashlib.sha256((OUT / path).read_bytes()).hexdigest() for path in hash_paths}
            if set(seen) != set(expected):
                errors.append(dict(error='Final full-run frame coverage mismatch'))
        write_json(OUT / 'full/audit-progress.json', report)
        print(json.dumps({k: report[k] for k in ['updated_at', 'status', 'completed', 'total', 'status_counts', 'retried_frames', 'throughput_frames_per_second', 'eta_seconds']} | {'audit_errors': len(errors)}), flush=True)
        if complete:
            write_json(OUT / 'full/audit-final.json', report)
            return int(bool(errors))
        if not args.watch:
            return int(bool(errors))
        time.sleep(min(max(args.interval, 1), 60))


if __name__ == '__main__':
    raise SystemExit(main())
