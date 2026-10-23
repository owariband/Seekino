"""Factory-backed VLM pilot with untouched responses and one explicit validation retry."""
from __future__ import annotations

import argparse
from collections import Counter
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import fields, is_dataclass
from datetime import datetime, timezone
from enum import Enum
from functools import partial
import hashlib
import json
import logging
from pathlib import Path
import re
import sys
from threading import Lock
from time import perf_counter

OUT = Path(__file__).resolve().parent
ROOT = OUT.parents[3]
sys.path.insert(0, str(ROOT))

from pydantic import BaseModel, ValidationError
from gateway.config import ModelBinding, load_config
from gateway.codex.client import CodexSession
from gateway.model_diagnostics import _error_message
from gateway.model_factory import GatewayClient
from gateway.multimodal import ImageInput, TextInput
from gateway.usage import capture_model_usage, model_call_context
from schema_models import PROMPT_VERSION, SCHEMA_VERSION, ShotTag


def plain(value):
    if isinstance(value, BaseModel):
        return plain(value.model_dump(mode='json', by_alias=True))
    if is_dataclass(value):
        return {f.name: plain(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, Mapping):
        return {str(k): '[redacted]' if str(k).lower() in {'authorization', 'api_key', 'access_token', 'secret_key'}
                else plain(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [plain(v) for v in value]
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, str) and value.startswith('data:'):
        return {'media': 'same image as request.json', 'data_url_sha256': hashlib.sha256(value.encode()).hexdigest()}
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def write_json(path, value):
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(plain(value), ensure_ascii=False, indent=2) + '\n')
    temporary.replace(path)


def strict_json(text):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError(f'Duplicate JSON object key: {key}')
            result[key] = value
        return result

    def constant(value):
        raise ValueError(f'Non-finite JSON numeric constant: {value}')

    return json.loads(text, object_pairs_hook=pairs, parse_constant=constant)


class Capture:
    def __init__(self, directory, system):
        self.directory, self.system = directory, system
        self.text = None
        self.terminal_success = False
        self.actual_model = None
        self.lock = Lock()
        self.deltas = []

    def event(self, kind, value):
        entry = dict(event=kind, timestamp=datetime.now(timezone.utc).isoformat(), payload=plain(value))
        with self.lock, (self.directory / 'events.jsonl').open('a') as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + '\n')

    def response(self, value, text, success):
        self.text, self.terminal_success = text, success
        write_json(self.directory / 'response.json', value)
        (self.directory / 'response.txt').write_text(text or '')


class CapturingCodexSession(CodexSession):
    def __init__(self, *args, capture, **kwargs):
        self.capture = capture
        super().__init__(*args, **kwargs)

    def _thread_options(self):
        options = super()._thread_options()
        # Preserve the existing bounded-tools developer instructions; use the exact experiment system prompt.
        options['baseInstructions'] = self.capture.system
        write_json(self.capture.directory / 'effective-thread-options.json', options)
        return options

    def _handle_notification(self, event):
        self.capture.event('codex_notification', event)
        super()._handle_notification(event)

    def run_turn(self, prompt, *, output_schema=None):
        write_json(self.capture.directory / 'effective-turn-input.json',
                   dict(input=prompt, output_schema=output_schema, reasoning_effort=self._reasoning_effort))
        turn = super().run_turn(prompt, output_schema=output_schema)
        self.capture.response(turn, turn.final_response, str(turn.status.value) == 'completed')
        return turn


class CapturingArkStream:
    def __init__(self, stream, capture):
        self.stream, self.capture = stream, capture
        self.response = getattr(stream, 'response', None)

    def __iter__(self):
        for event in self.stream:
            self.capture.event('ark_sse', event)
            if event.type == 'response.output_text.delta':
                self.capture.deltas.append(event.delta)
            elif event.type == 'response.output_text.done':
                self.capture.text = event.text
                (self.capture.directory / 'response.txt').write_text(event.text)
            if event.type in {'response.completed', 'response.failed', 'response.incomplete'}:
                response = event.response
                self.capture.actual_model = getattr(response, 'model', None)
                text = ''.join(part.text for item in getattr(response, 'output', []) or []
                               for part in getattr(item, 'content', []) or [] if getattr(part, 'type', None) == 'output_text')
                self.capture.response(response, text or self.capture.text or ''.join(self.capture.deltas),
                                      event.type == 'response.completed')
            yield event

    def close(self):
        self.stream.close()
        if not (self.capture.directory / 'response.txt').exists() and self.capture.deltas:
            (self.capture.directory / 'response.txt').write_text(''.join(self.capture.deltas))


class CapturingArkClient:
    def __init__(self, client, capture):
        self.client, self.capture = client, capture
        self.responses = self

    def create(self, **request):
        request['input'] = [{'role': 'system', 'content': self.capture.system}, *request['input']]
        request.update(temperature=0, top_p=1)
        write_json(self.capture.directory / 'effective-provider-request.json', request)
        stream = self.client.responses.create(**request)
        return CapturingArkStream(stream, self.capture)

    def close(self):
        self.client.close()


def invoke_frame(row, *, run_id, model_kind, config, binding, gallery, system, user, prompt_sha):
    sampling = dict(reasoning_effort=binding.reasoning_effort, temperature=0 if model_kind == 'cheap' else None,
                    top_p=1 if model_kind == 'cheap' else None, seed=None,
                    sampling_control='explicit temperature/top_p' if model_kind == 'cheap' else 'unavailable in Codex turn API')
    inference_settings = dict(sampling=sampling, max_output_tokens=binding.max_output_tokens,
                              source=binding.source, fallback=None, image_detail='high', max_model_calls=1)
    schema = ShotTag.model_json_schema()
    def validate_saved_attempt(directory, metadata):
        request = json.loads((directory / 'request.json').read_text())
        prior_binding = request['binding']
        if (metadata.get('sampling') != sampling or request.get('sampling') != sampling
                or request.get('prompt_sha256') != prompt_sha or request.get('system') != system
                or request.get('image_sha256') != row['sha256']
                or prior_binding.get('source') != binding.source or prior_binding.get('model') != binding.model
                or prior_binding.get('reasoning_effort') != binding.reasoning_effort
                or prior_binding.get('max_output_tokens') != binding.max_output_tokens
                or prior_binding.get('fallback') is not None or request.get('requested_schema') != schema):
            raise ValueError('Existing attempt has a different image/prompt/model/reasoning/sampling/schema contract')

    result_path = OUT / 'pilot' / run_id / f"{row['frame_id']}.json"
    if result_path.exists():
        existing = json.loads(result_path.read_text())
        if existing['sha256'] != row['sha256'] or existing['prompt_sha256'] != prompt_sha or existing['model'] != binding.model:
            raise ValueError('Existing result belongs to a different image/prompt/model')
        if not existing.get('attempts') or any(a['sampling'] != sampling for a in existing['attempts']):
            raise ValueError('Existing result has different or missing reasoning/sampling settings')
        for saved_attempt in existing['attempts']:
            validate_saved_attempt(OUT / saved_attempt['raw_path'], saved_attempt)
        if existing.get('inference_settings', inference_settings) != inference_settings:
            raise ValueError('Existing result has a different inference contract')
        return existing
    image_path = Path(row.get('image_path') or gallery / row['frame_path']).resolve()
    if hashlib.sha256(image_path.read_bytes()).hexdigest() != row['sha256']:
        raise ValueError('Pilot image SHA mismatch')
    raw_root = OUT / 'raw' / row['sha256'] / PROMPT_VERSION / run_id / row['frame_id']
    raw_root.mkdir(parents=True, exist_ok=True)
    record = {k: row[k] for k in ('frame_id', 'shot_id', 'episode', 'frame_timestamp_ms', 'sha256')}
    record.update(run_id=run_id, model=binding.model, source=binding.source, prompt_version=PROMPT_VERSION,
                  schema_version=SCHEMA_VERSION, prompt_sha256=prompt_sha, tags=None, attempts=[],
                  inference_settings=inference_settings)
    validation_errors = None
    for attempt in (1, 2):
        directory = raw_root / f'attempt-{attempt:02d}'
        directory.mkdir(exist_ok=True)
        metadata_path = directory / 'call.json'
        if metadata_path.exists():
            metadata = json.loads(metadata_path.read_text())
            validate_saved_attempt(directory, metadata)
            status = metadata['status']
            parsed = json.loads((directory / 'parsed.json').read_text()) if status == 'valid' else None
            validation_errors = metadata.get('validation_errors')
        else:
            if (directory / 'request.json').exists():
                raise RuntimeError(f"Interrupted attempt retained; inspect before replay: {row['frame_id']}")
            effective_user = user if attempt == 1 else user + '\n\nYour previous response failed validation. Return a complete corrected JSON object. Validation errors:\n' + json.dumps(validation_errors, ensure_ascii=False)
            capture = Capture(directory, system)
            write_json(directory / 'request.json', dict(system=system, user=effective_user, image_path=str(image_path),
                       image_sha256=row['sha256'], prompt_version=PROMPT_VERSION, prompt_sha256=prompt_sha,
                       binding=binding.model_dump(mode='json'), sampling=sampling,
                       validation_attempt=attempt, requested_schema=schema))
            started_at = datetime.now(timezone.utc).isoformat()
            tick = perf_counter()
            usage, gateway_error, parsed, status = [], None, None, 'api_error'
            gateway = GatewayClient(config)
            ark_client = None
            try:
                model = gateway.create_generative_model(binding)
                delegate = model._delegate._delegate
                if model_kind == 'primary':
                    delegate.cwd = str(OUT)
                    delegate._session_factory = partial(CapturingCodexSession, capture=capture)
                else:
                    ark_client = CapturingArkClient(delegate._new_client(), capture)
                    delegate._client = ark_client
                with model_call_context(dict(experiment='shot-tags', run_id=run_id, frame_id=row['frame_id'], validation_attempt=attempt)), capture_model_usage(lambda call: usage.append(plain(call))):
                    model.generate([TextInput(effective_user), ImageInput(image_path)], schema_name='shot_composition_v1',
                                   result_type=ShotTag, tools=(), max_model_calls=1)
            except Exception as error:
                gateway_error = dict(type=type(error).__name__, message=_error_message(error),
                                     status_code=getattr(error, 'status_code', None))
            finally:
                if ark_client is not None:
                    ark_client.close()
                gateway.close()
            if capture.terminal_success and isinstance(capture.text, str):
                try:
                    parsed = ShotTag.model_validate(strict_json(capture.text), strict=True).model_dump(mode='json')
                    status = 'valid' if gateway_error is None else 'api_error'
                    if status == 'valid':
                        write_json(directory / 'parsed.json', parsed)
                except (ValidationError, ValueError) as error:
                    status = 'invalid'
                    validation_errors = error.errors(include_url=False, include_context=False) if isinstance(error, ValidationError) else [{'type': type(error).__name__, 'message': str(error)}]
                    write_json(directory / 'validation-errors.json', validation_errors)
            metadata = dict(status=status, attempt=attempt, requested_model=binding.model, actual_model=capture.actual_model,
                            source=binding.source, run_id=run_id, frame_id=row['frame_id'], prompt_version=PROMPT_VERSION,
                            started_at=started_at, finished_at=datetime.now(timezone.utc).isoformat(),
                            latency_seconds=round(perf_counter() - tick, 6), sampling=sampling, usage=usage,
                            gateway_error=gateway_error, validation_errors=validation_errors if status == 'invalid' else None,
                            raw_path=str(directory.relative_to(OUT)))
            write_json(metadata_path, metadata)
        record['attempts'].append(metadata)
        if status != 'invalid' or attempt == 2:
            record.update(status=status, tags=parsed if status == 'valid' else None)
            break
    record['provenance'] = {group: dict(source=f'vlm:{binding.model}', prompt_version=PROMPT_VERSION, run_id=run_id)
                            for group in ('camera', 'composition', 'subjects', 'frame', 'content')}
    write_json(result_path, record)
    print(json.dumps(dict(stage='frame_complete', run_id=run_id, frame_id=row['frame_id'], status=record['status'],
                          attempts=len(record['attempts']), latency_seconds=sum(a['latency_seconds'] for a in record['attempts']))), flush=True)
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', required=True)
    parser.add_argument('--model', choices=['primary', 'cheap'], required=True)
    parser.add_argument('--workers', type=int, default=6)
    parser.add_argument('--limit', type=int)
    parser.add_argument('--manifest', type=Path, default=OUT / 'pilot' / 'frames.jsonl')
    args = parser.parse_args()
    if not re.fullmatch(r'[a-z0-9_-]+', args.run) or args.workers < 1:
        parser.error('Use a simple run name and a positive worker count')
    logging.disable(logging.CRITICAL)
    config = load_config()
    if args.model == 'primary':
        binding = config.resolve_model_binding('vision').model_copy(update={'fallback': None, 'reasoning_effort': 'low'})
        if binding.model != 'gpt-5.6-sol' or config.source(binding.source).adapter != 'codex_app_server':
            raise ValueError('Primary model must be configured gpt-5.6-sol on Codex')
    else:
        binding = config.resolve_model_binding(ModelBinding(source='ark_default', model='doubao-seed-2-0-lite-260428',
                  supports_vision_input=True, reasoning_effort='none', max_output_tokens=8192,
                  request_timeout_seconds=180, fallback=None))
    worker_cap = min(10 if args.model == 'primary' else 20,
                     getattr(config.source(binding.source), 'max_concurrency', None) or 20)
    if args.workers > worker_cap:
        parser.error(f'{args.model} permits at most {worker_cap} concurrent workers')
    rows = [json.loads(line) for line in args.manifest.read_text().splitlines() if line.strip()]
    if args.limit:
        rows = rows[:args.limit]
    assert rows and len({r['frame_id'] for r in rows}) == len(rows)
    gallery = Path(json.loads((OUT / 'input_manifest.json').read_text())['gallery'])
    prompt = (OUT / 'prompt' / 'shot-tag-v1.md').read_text()
    system, user = re.findall(r'```text\n(.*?)\n```', prompt, flags=re.S)
    prompt_sha = hashlib.sha256(prompt.encode()).hexdigest()
    if args.run == 'full':
        pilot_run = 'primary_a' if args.model == 'primary' else 'cheap'
        pilot_summary = json.loads((OUT / 'pilot' / f'{pilot_run}-summary.json').read_text())
        if (pilot_summary.get('status') != 'complete' or pilot_summary.get('completed') != 200
                or pilot_summary.get('binding') != binding.model_dump(mode='json')
                or pilot_summary.get('prompt_sha256') != prompt_sha):
            raise ValueError('Full run must match the completed selected pilot model binding and prompt exactly')
    (OUT / 'pilot' / args.run).mkdir(exist_ok=True)
    result_path = OUT / 'pilot' / f'{args.run}.jsonl'
    summary_path = OUT / 'pilot' / f'{args.run}-summary.json'
    results = {}
    summary = dict(run_id=args.run, model=binding.model, source=binding.source, binding=binding.model_dump(mode='json'),
                   prompt_version=PROMPT_VERSION, prompt_sha256=prompt_sha, frames=len(rows), workers=args.workers,
                   started_at=datetime.now(timezone.utc).isoformat(), status='running')
    write_json(summary_path, summary)
    tick = perf_counter()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(invoke_frame, row, run_id=args.run, model_kind=args.model, config=config,
                   binding=binding, gallery=gallery, system=system, user=user, prompt_sha=prompt_sha): row for row in rows}
        for future in as_completed(futures):
            row = futures[future]
            try:
                result = future.result()
            except Exception as error:
                result = dict(frame_id=row['frame_id'], sha256=row['sha256'], run_id=args.run, model=binding.model,
                              status='api_error', tags=None, attempts=[], runner_error=dict(type=type(error).__name__, message=_error_message(error)))
                write_json(OUT / 'pilot' / args.run / f"{row['frame_id']}.runner-error.json", result)
            results[row['frame_id']] = result
            ordered = [results[r['frame_id']] for r in rows if r['frame_id'] in results]
            temporary = result_path.with_suffix('.jsonl.tmp')
            temporary.write_text(''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in ordered))
            temporary.replace(result_path)
            summary.update(completed=len(results), counts=dict(Counter(r['status'] for r in ordered)),
                           wall_seconds=round(perf_counter() - tick, 3))
            write_json(summary_path, summary)
            print(json.dumps(dict(stage='run_progress', run_id=args.run, completed=len(results), total=len(rows), counts=summary['counts'])), flush=True)
    summary.update(status='complete', finished_at=datetime.now(timezone.utc).isoformat())
    write_json(summary_path, summary)
    return 0 if summary['counts'].get('api_error', 0) == 0 else 1


if __name__ == '__main__':
    raise SystemExit(main())
