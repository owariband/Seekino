"""Resumable image embeddings for the shot gallery; never reads pooled Event vectors."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import logging
from pathlib import Path
import sys
import threading
import time

import numpy as np

OUT = Path(__file__).resolve().parent
ROOT = OUT.parents[3]
sys.path.insert(0, str(ROOT))

from arkruntime._exceptions import ArkAPIConnectionError
from engine.rag.encoding import validate_embedding_vector
from gateway.config import load_config
from gateway.embedding import describe_embedding_model
from gateway.model_factory import GatewayClient


def save_json(path: Path, value: object) -> None:
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n')
    temporary.replace(path)


def load_rows(path: Path) -> list[dict]:
    # A producer may append rows while this process runs; only consume complete lines.
    lines = path.read_bytes().splitlines(keepends=True)
    rows = [json.loads(line) for line in lines if line.endswith(b'\n') and line.strip()]
    identities = set()
    for row in rows:
        if row['frame_id'] in identities:
            raise ValueError('Duplicate frame_id in keyframe manifest')
        identities.add(row['frame_id'])
        if not row['shot_id'] or len(row['sha256']) != 64:
            raise ValueError('Invalid keyframe identity or SHA-256')
        int(row['sha256'], 16)
        frame = (OUT / row['frame_path']).resolve()
        if not frame.is_relative_to(OUT) or not frame.is_file():
            raise ValueError('Keyframe path is missing or outside the experiment')
        if hashlib.sha256(frame.read_bytes()).hexdigest() != row['sha256']:
            raise ValueError(f"Keyframe SHA-256 mismatch: {row['frame_id']}")
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, default=OUT / 'keyframes.jsonl')
    parser.add_argument('--workers', type=int, default=6)
    parser.add_argument('--max-attempts', type=int, default=4)
    parser.add_argument('--timeout-seconds', type=float, default=60)
    parser.add_argument('--consolidate', action='store_true',
                        help='Write embeddings.npy and embedding_rows.jsonl for this input snapshot')
    parser.add_argument('--dry-run', action='store_true', help='Validate local inputs without API calls or writes')
    args = parser.parse_args()
    if not 1 <= args.workers <= 32 or not 1 <= args.max_attempts <= 8 or args.timeout_seconds <= 0:
        parser.error('Use 1–32 workers, 1–8 attempts, and a positive timeout')
    logging.disable(logging.CRITICAL)
    rows = load_rows(args.input)
    if not rows:
        raise ValueError('No complete keyframe rows available yet')
    unique = {row['sha256']: row for row in rows}
    config = load_config()
    binding = config.resolve_embedding_binding().model_copy(update={'request_timeout_seconds': args.timeout_seconds})
    info = describe_embedding_model(binding, config.source(binding.source))
    if info.encoder_model != 'doubao-embedding-vision-250615' or info.dimensions != 1024:
        raise ValueError('Gallery requires the configured doubao-embedding-vision-250615 / 1024d encoder')
    contract = dict(provider=info.provider_name, model=info.encoder_model,
                    encoder_revision=info.encoder_revision, dimensions=info.dimensions,
                    modality='image', dtype='float32', normalization='none')
    metadata_path = OUT / 'embeddings.json'
    metadata = json.loads(metadata_path.read_text()) if metadata_path.exists() else {'contract': contract}
    if metadata['contract'] != contract:
        raise ValueError('Existing embedding cache has a different encoder contract')
    cache = OUT / 'embeddings'
    pending = []
    for digest, row in unique.items():
        target = cache / f'{digest}.npy'
        if target.exists():
            vector = np.load(target, allow_pickle=False)
            if vector.shape != (info.dimensions,):
                raise ValueError(f'Cached embedding has incorrect shape: {digest}')
            validate_embedding_vector(vector.tolist(), dimensions=info.dimensions)
        else:
            pending.append(row)
    print(json.dumps(dict(stage='embedding_plan', frames=len(rows), unique_images=len(unique),
                          cached=len(unique) - len(pending), pending=len(pending), workers=args.workers)), flush=True)
    if args.dry_run:
        return 0

    # Prevent overlapping invocations from making duplicate paid requests for the same images.
    with (OUT / 'embeddings.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('Another gallery embedding process is active') from None
        cache.mkdir(exist_ok=True)
        for key in ('matrix_file', 'row_map', 'matrix_shape'):
            metadata.pop(key, None)
        metadata.update(input_file=args.input.name, input_frames=len(rows), unique_images=len(unique),
                        status='running', workers=args.workers, max_attempts=args.max_attempts,
                        request_timeout_seconds=args.timeout_seconds,
                        started_at=datetime.now(timezone.utc).isoformat(), completed=len(unique) - len(pending),
                        failures=[], requests_this_run=0, cache_directory='embeddings')
        save_json(metadata_path, metadata)
        local = threading.local()
        clients = []
        clients_lock = threading.Lock()
        fatal = threading.Event()

        def embed(row: dict) -> dict:
            if fatal.is_set():
                return dict(sha256=row['sha256'], error='cancelled_after_auth_failure', attempts=0)
            if not hasattr(local, 'model'):
                client = GatewayClient(config)
                with clients_lock:
                    clients.append(client)
                local.model = client.create_embedding_model(binding)
            target = cache / f"{row['sha256']}.npy"
            # Cache can grow between the initial plan and acquiring the process lock.
            if target.exists():
                validate_embedding_vector(np.load(target, allow_pickle=False).tolist(), dimensions=info.dimensions)
                return dict(sha256=row['sha256'], attempts=0)
            for attempt in range(1, args.max_attempts + 1):
                try:
                    result = local.model.embed_image(OUT / row['frame_path'])
                    if result.modality != 'image' or result.model != info.encoder_model:
                        raise ValueError('Image embedding response contract differs')
                    vector = np.asarray(validate_embedding_vector(result.vector, dimensions=info.dimensions), dtype=np.float32)
                    temporary = target.with_suffix('.npy.tmp')
                    with temporary.open('wb') as handle:
                        np.save(handle, vector, allow_pickle=False)
                    temporary.replace(target)
                    return dict(sha256=row['sha256'], attempts=attempt)
                except Exception as error:
                    status = getattr(error, 'status_code', None)
                    if status in (401, 403):
                        fatal.set()
                    retryable = isinstance(error, ArkAPIConnectionError) or status in (408, 409, 429) or (
                        isinstance(status, int) and 500 <= status <= 599)
                    if not retryable or attempt == args.max_attempts:
                        # Exception bodies may contain request details; persist only type and HTTP status.
                        return dict(sha256=row['sha256'], error=type(error).__name__, http_status=status, attempts=attempt)
                    time.sleep(min(2 ** (attempt - 1), 30))
            raise AssertionError('Retry loop did not return')

        try:
            with ThreadPoolExecutor(max_workers=args.workers) as executor:
                futures = {executor.submit(embed, row): row for row in pending}
                for finished, future in enumerate(as_completed(futures), 1):
                    try:
                        receipt = future.result()
                    except Exception as error:
                        fatal.set()
                        receipt = dict(sha256=futures[future]['sha256'], error=type(error).__name__, attempts=0)
                    metadata['requests_this_run'] += receipt['attempts']
                    if receipt.get('error'):
                        metadata['failures'].append(receipt)
                    else:
                        metadata['completed'] += 1
                    if finished % 25 == 0 or finished == len(pending):
                        save_json(metadata_path, metadata)
                        print(json.dumps(dict(stage='embedding_progress', completed=metadata['completed'],
                                              unique_images=len(unique), failures=len(metadata['failures']))), flush=True)
        finally:
            for client in clients:
                client.close()
        metadata.update(status='complete' if not metadata['failures'] else 'incomplete',
                        finished_at=datetime.now(timezone.utc).isoformat())
        if args.consolidate and not metadata['failures']:
            matrix = np.stack([np.load(cache / f"{row['sha256']}.npy", allow_pickle=False) for row in rows])
            temporary = OUT / 'embeddings.npy.tmp'
            with temporary.open('wb') as handle:
                np.save(handle, matrix, allow_pickle=False)
            temporary.replace(OUT / 'embeddings.npy')
            row_map = OUT / 'embedding_rows.jsonl.tmp'
            row_map.write_text(''.join(json.dumps(dict(row=i, frame_id=row['frame_id'], shot_id=row['shot_id'],
                                                       sha256=row['sha256'], frame_path=row['frame_path'])) + '\n'
                                       for i, row in enumerate(rows)))
            row_map.replace(OUT / 'embedding_rows.jsonl')
            metadata.update(matrix_file='embeddings.npy', row_map='embedding_rows.jsonl', matrix_shape=list(matrix.shape))
        save_json(metadata_path, metadata)
        print(json.dumps(dict(stage='embedding_finished', status=metadata['status'],
                              completed=metadata['completed'], failures=len(metadata['failures']))), flush=True)
        return 0 if not metadata['failures'] else 1


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as error:
        print(json.dumps(dict(stage='embedding_failed', error=type(error).__name__)), file=sys.stderr, flush=True)
        raise SystemExit(1) from None
