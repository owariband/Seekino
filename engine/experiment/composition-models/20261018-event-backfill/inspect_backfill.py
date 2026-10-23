"""Read-only MyGO backfill snapshots; run from repo root with .env.local loaded."""
import argparse
import hashlib
import json
import math
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

from sqlalchemy import select
from agent.video_parse_v2.input import ParseConfig
from engine.rag.indexing.events import plan_event_index
from gateway.config import load_config
from scripts.local_services import configure, config_digest
from server.caller.mysql import open_database_storage
from server.caller.elasticsearch import open_elasticsearch_client, build_event_elasticsearch_storage
from server.cli import _event_contract
from server.config.parse_profile import resolve_embedding_config
from server.config.settings import resolve_parse_max_height
from server.model.job import VideoParseTask, ChunkTask, ChunkParseContent
from server.model.video import Video, Series, VideoChunk
from server.repository.video_parse import VideoParseRepository

SERIES = '836fa9f0-2bf4-50fb-9c64-75526fa1dea9'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--verify-es', action='store_true')
    parser.add_argument('--episode', type=int)
    args = parser.parse_args()
    baseline = json.loads((Path(__file__).resolve().parent / 'before.json').read_text()) if args.verify_es else None
    settings = configure()
    gateway = load_config()
    contract = _event_contract(gateway, resolve_embedding_config(gateway, dimensions=settings.text_vector_dimensions), max_height=resolve_parse_max_height())
    report = {'observed_at': datetime.now(timezone.utc).isoformat(), 'series_id': SERIES,
              'config_digest': config_digest(), 'contract_id': contract.contract_id,
              'contract': contract.as_dict(), 'episodes': []}
    if args.verify_es:
        assert report['contract_id'] == baseline['contract_id']
    with open_database_storage(settings.database_url, environment=settings.environment,
                               pool_recycle_seconds=3600) as (_, sessions), open_elasticsearch_client(settings.elasticsearch) as client:
        repo = VideoParseRepository(sessions)
        store = build_event_elasticsearch_storage(client, settings.elasticsearch, environment=settings.environment,
                    semantic_dimensions=settings.text_vector_dimensions, vision_dimensions=settings.vision_vector_dimensions)
        report['event_index'] = store.index_name
        report['index_exists'] = bool(client.indices.exists(index=store.index_name))
        if report['index_exists']:
            store.bind_contract(contract)
        elif args.verify_es:
            raise RuntimeError('Event index missing')
        with sessions() as session:
            videos = session.scalars(select(Video).join(Series, Series.id == Video.series_id).where(
                Video.series_id == SERIES, Series.tenant_id == 'local-default').order_by(Video.episode_order)).all()
            assert len(videos) == 13
            for video in videos:
                if args.episode and video.episode_order != args.episode:
                    continue
                task = session.scalar(select(VideoParseTask).where(VideoParseTask.video_id == video.id,
                    VideoParseTask.is_deleted.is_(False)).order_by(VideoParseTask.version.desc()).limit(1))
                frozen = ParseConfig.model_validate(task.parse_config)
                assert _event_contract(gateway, frozen.models.embedding, max_height=resolve_parse_max_height()) == contract
                state = repo.event_index_status(task.id)
                rows = session.execute(select(ChunkTask, ChunkParseContent, VideoChunk).join(
                    ChunkParseContent, ChunkParseContent.chunk_task_id == ChunkTask.id).join(
                    VideoChunk, VideoChunk.id == ChunkTask.chunk_id).where(ChunkTask.video_task_id == task.id)
                    .order_by(VideoChunk.sequence_no)).all()
                if args.verify_es:
                    assert state['task_complete'] and state['covered_chunks'] == state['total_chunks']
                    assert len(rows) == state['total_chunks'] == len(state['chunks'])
                    assert all(c['receipt_ready'] for c in state['chunks'])
                    assert state['target']['index_name'] == store.index_name
                    assert state['target']['contract_id'] == contract.contract_id
                item = {'episode': video.episode_order, 'task_id': task.id, 'task_version': task.version,
                        'status': task.status, 'coverage': state, 'chunks': []}
                for chunk, content, media in rows:
                    plans = plan_event_index(task.id, media.id, content.version, content.content,
                                             media.start_ms, media.end_ms, contract)
                    entry = {'chunk_id': media.id, 'chunk_task_id': chunk.id, 'content_id': content.id,
                        'content_version': content.version, 'content_sha256': hashlib.sha256(json.dumps(
                            content.content, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
                        'status': chunk.status, 'phase': chunk.phase, 'retry_count': chunk.retry_count,
                        'expected_events': len(plans), 'expected_frames': sum(len(p.frame_timestamps_ms) for p in plans),
                        'event_ids': [p.event_id for p in plans]}
                    if args.verify_es:
                        docs = store.inspect(entry['event_ids'])
                        present = [d for d in docs.values() if d is not None]
                        entry['es_all_present'] = len(present) == len(plans)
                        entry['es_searchable'] = entry['es_all_present'] and store.verify_ready(present)
                        entry['es_exact_set'] = store.verify_set(task.id, media.id, content.version, entry['event_ids'])
                        assert entry['es_searchable'] and entry['es_exact_set'], media.id
                        for plan in plans:
                            doc = docs[plan.event_id]
                            assert (doc.parse_task_id, doc.video_chunk_id, doc.version, doc.timeline_index) == (
                                task.id, media.id, content.version, plan.timeline_index)
                            assert (doc.event_type, doc.speaker, doc.lexical_fields) == (
                                plan.event_type, plan.speaker, plan.lexical_fields)
                            assert abs(math.hypot(*doc.vision_vector) - 1.0) < 1e-4
                            assert tuple(f.requested_at_ms for f in doc.detail.capture.frames) == plan.frame_timestamps_ms
                            assert all(f.actual_at_ms is None or plan.capture_start_ms <= f.actual_at_ms < plan.capture_end_ms
                                       for f in doc.detail.capture.frames)
                        entry['es_source_capture_verified'] = True
                    item['chunks'].append(entry)
                item['expected_events'] = sum(c['expected_events'] for c in item['chunks'])
                item['expected_frames'] = sum(c['expected_frames'] for c in item['chunks'])
                item['chunk_states'] = dict(Counter(c['status'] for c in item['chunks']))
                if args.verify_es:
                    old = next(e for e in baseline['episodes'] if e['episode'] == item['episode'])
                    assert (item['task_id'], item['task_version']) == (old['task_id'], old['task_version'])
                    identities = ('chunk_id', 'chunk_task_id', 'content_id', 'content_version', 'content_sha256', 'event_ids')
                    assert [[c[k] for k in identities] for c in item['chunks']] == [
                        [c[k] for k in identities] for c in old['chunks']]
                    item['formal_content_unchanged'] = True
                report['episodes'].append(item)
            if args.verify_es:
                assert [e['episode'] for e in report['episodes']] == [
                    e['episode'] for e in baseline['episodes'] if args.episode is None or e['episode'] == args.episode]
                count = client.count(index=store.index_name, query={
                    'terms': {'parse_task_id': [e['task_id'] for e in report['episodes']]}})
                assert count.get('_shards', {}).get('failed', 0) == 0
                report['es_event_count'] = count['count']
                assert report['es_event_count'] == sum(e['expected_events'] for e in report['episodes'])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({k: report[k] for k in ('observed_at', 'event_index', 'index_exists', 'contract_id', 'contract')}, ensure_ascii=False))
    for e in report['episodes']:
        print(json.dumps({k: e[k] for k in ('episode', 'task_id', 'status', 'chunk_states', 'expected_events', 'expected_frames')} | {
            'covered_chunks': e['coverage']['covered_chunks'], 'total_chunks': e['coverage']['total_chunks']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
