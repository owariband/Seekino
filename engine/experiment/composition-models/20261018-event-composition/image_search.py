"""One real image embedding -> Event vectors; experiment, not a public tool API."""
import hashlib
import json
import logging
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

from agent.chat.video_search.frame_evidence import FrameReadRequest
from engine.rag.contracts import VideoChunkRetrievalIdentity
from engine.rag.encoding import validate_embedding_vector
from engine.rag.retrieval.contracts import EventFilters
from engine.rag.retrieval.events import fuse_event_hits, verify_event_candidate
from gateway.config import EmbeddingModelBinding, load_config
from gateway.model_factory import GatewayClient
from scripts.local_services import configure, config_digest
from server.caller.elasticsearch import open_elasticsearch_client, build_event_elasticsearch_storage
from server.caller.mysql import open_database_storage
from server.cli import _event_contract
from server.common.storage import build_artifact_storage
from server.config.parse_profile import resolve_embedding_config
from server.config.settings import resolve_parse_max_height
from server.repository.video_parse import VideoParseRepository
from server.service.video_frame_reader import VideoFrameReader

OUT = Path(__file__).resolve().parent
SERIES = '836fa9f0-2bf4-50fb-9c64-75526fa1dea9'
TENANT = 'local-default'
RESULT = OUT / 'image-results.json'


def save(report):
    RESULT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')


def main():
    logging.disable(logging.CRITICAL)
    reference = OUT / 'reference.jpg'
    digest = hashlib.sha256(reference.read_bytes()).hexdigest()
    text_run = json.loads((OUT / 'results.json').read_text())
    assert digest == text_run['reference_sha256']
    settings = configure()
    gateway = load_config()
    embedding = resolve_embedding_config(gateway, dimensions=settings.text_vector_dimensions)
    contract = _event_contract(gateway, embedding, max_height=resolve_parse_max_height())
    assert contract.contract_id == text_run['contract_id']
    report = json.loads(RESULT.read_text()) if RESULT.exists() else dict(
        created_at=datetime.now(timezone.utc).isoformat(), status='prepared',
        reference='reference.jpg', reference_sha256=digest, series_id=SERIES,
        query_modality='image', contract_id=contract.contract_id, contract=contract.as_dict(),
        config_digest=config_digest(), retrieval_entry='experiment: embed_image -> Event ES search_vector',
        public_tool_image_support=False, exact=True, requested_top_k=40,
        score_definition='ES exact score = cosineSimilarity + 1; cosine_similarity = score - 1',
        preview_rule='Hit 1 plus up to 7 further Events; exclude same-video overlap with 1500 ms padding',
        limitations=['Event visual vectors pool sampled frames; a hit is not exact-frame source proof.',
                     'Source-frame inspection is required; similarity is not a composition probability.'])
    assert report['status'] != 'complete', 'Completed run must not be overwritten'
    assert report['reference_sha256'] == digest and report['contract_id'] == contract.contract_id
    client = GatewayClient(gateway)
    try:
        with open_database_storage(settings.database_url, environment=settings.environment,
            pool_recycle_seconds=3600) as (_, sessions), open_elasticsearch_client(settings.elasticsearch) as es:
            repo = VideoParseRepository(sessions)
            store = build_event_elasticsearch_storage(es, settings.elasticsearch, environment=settings.environment,
                semantic_dimensions=settings.text_vector_dimensions, vision_dimensions=settings.vision_vector_dimensions)
            store.bind_contract(contract)
            scope = repo.resolve_retrieval_scope([SERIES], tenant_id=TENANT)
            assert not scope.partial, 'Full MyGO publication must be ready'
            sources = repo.validate_event_coverage(scope, index_name=store.index_name, contract_id=contract.contract_id)
            report.update(event_index=store.index_name, source_chunks=len(sources), source_tasks=len(set(s[0] for s in sources)))
            if 'query_embedding' not in report:
                assert report['status'] != 'embedding_requested', 'Previous image request must not be repeated'
                report['status'] = 'embedding_requested'
                save(report)
                binding = EmbeddingModelBinding.model_validate(embedding.model_dump(exclude_none=True))
                model = client.create_embedding_model(binding)
                started = monotonic()
                try:
                    result = model.embed_image(reference)
                finally:
                    model.close()
                assert result.modality == 'image' and result.model == contract.vision_encoder.model
                vector = validate_embedding_vector(result.vector, dimensions=contract.vision_encoder.dimensions)
                report.update(status='embedding_saved', embedding_ms=round((monotonic() - started) * 1000),
                    query_embedding=dict(modality='image', model=result.model, vector=list(vector)))
                save(report)  # Resume without another embedding if later frame I/O fails.
            artifact = report['query_embedding']
            assert artifact['modality'] == 'image' and artifact['model'] == contract.vision_encoder.model
            vector = validate_embedding_vector(artifact['vector'], dimensions=contract.vision_encoder.dimensions)
            started = monotonic()
            raw = store.search_vector(vector, channel='vision', sources=sources,
                filters=EventFilters(), limit=40, exact=True)
            assert raw and all((h.parse_task_id, h.video_chunk_id, h.version) in set(sources) for h in raw)
            fused = fuse_event_hits(raw, channel_weights={'vision': 1.0}, rrf_k=60)
            task_videos = {task: video for video, task in scope.parse_tasks_by_video_id.items()}
            identities = {}
            for hit in fused:
                video = task_videos[hit.parse_task_id]
                identities[hit.video_chunk_id] = VideoChunkRetrievalIdentity(
                    tenant_id=TENANT, series_id=scope.series_by_video_id[video], video_id=video,
                    parse_task_id=hit.parse_task_id, content_version=hit.version, index_generation=scope.index_generation)
            hydrated = {h.facts.video_chunk_id: h for h in repo.hydrate_retrieval_candidates(list(identities.items()))}
            matches = [verify_event_candidate(h, hydrated[h.video_chunk_id].facts, contract=contract) for h in fused]
            candidates = []
            for hit in matches:
                evidence = hydrated[hit.video_chunk_id].evidence
                candidates.append(dict(rank=hit.channel_ranks['vision'], episode=evidence.episode_order,
                    video_id=evidence.video_id, description=evidence.all_time_events[hit.timeline_index]['description'],
                    cosine_similarity=hit.channel_scores['vision'] - 1.0, **hit.model_dump(mode='json')))
            report.update(status='retrieved', retrieval_ms=round((monotonic() - started) * 1000),
                raw_hits=[h.model_dump(mode='json') for h in raw], candidates=candidates, previews=[])
            save(report)
            artifacts = build_artifact_storage(settings.artifact_root, environment=settings.environment)
            reader = VideoFrameReader(repo, artifacts)
            conversation, message = str(uuid4()), str(uuid4())
            selected = []
            (OUT / 'frames').mkdir(exist_ok=True)
            for candidate in candidates:
                if any(c['video_id'] == candidate['video_id'] and candidate['start_ms'] < c['end_ms'] + 1500
                       and c['start_ms'] - 1500 < candidate['end_ms'] for c in selected):
                    continue
                selected.append(candidate)
                evidence = hydrated[candidate['video_chunk_id']].evidence
                request = FrameReadRequest(evidence=evidence, inspection_id=uuid4().hex,
                    role='image-experiment', conversation_id=conversation, message_id=message,
                    start_ms=max(candidate['start_ms'], evidence.start_seconds),
                    end_ms=min(candidate['end_ms'], evidence.end_seconds), frame_count=1,
                    max_height=720, timeout_seconds=60, check_control=lambda: None)
                inspection = reader(request)
                frame = inspection.payload['frames'][0]
                path = OUT / 'frames' / f"image-{len(selected):02d}-rank{candidate['rank']:02d}.jpg"
                shutil.copy2(artifacts.resolve(frame['artifact_uri']), path)
                assert hashlib.sha256(path.read_bytes()).hexdigest() == frame['sha256']
                report['previews'].append(dict(**candidate, image=str(path.relative_to(OUT)),
                    frame_timestamp_ms=frame['requested_timestamp_ms'], sha256=frame['sha256'], receipt=inspection.payload))
                save(report)
                if len(selected) == 8:
                    break
            report.update(status='complete', completed_at=datetime.now(timezone.utc).isoformat())
            save(report)
            print(json.dumps(dict(status='complete', candidates=len(candidates), previews=len(selected),
                embedding_ms=report['embedding_ms'], hit1={k: candidates[0][k] for k in
                ('rank', 'episode', 'start_ms', 'end_ms', 'description', 'cosine_similarity')}), ensure_ascii=False))
    finally:
        client.close()


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(json.dumps({'status': 'error', 'error_type': type(error).__name__}))
        raise SystemExit(1)
