"""Call the real Event/frames Agent tools against existing MyGO data; no reindex."""
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

from agent.chat.common.state import ExecutionControl
from agent.chat.video_search.frame_evidence import FrameInspectionSession
from agent.chat.video_search.tools.event_retrieval import build_search_video_events_tool
from agent.chat.video_search.tools.frames import build_inspect_video_frames_tool
from agent.chat.video_search.video_search_canvas import VideoSearchContextCanvas
from gateway.config import load_config
from gateway.model_factory import GatewayClient
from scripts.local_services import configure
from server.caller.elasticsearch import open_elasticsearch_client, build_event_elasticsearch_storage
from server.caller.mysql import open_database_storage
from server.cli import _event_contract
from server.common.retrieval import build_event_retrieval_service
from server.common.storage import build_artifact_storage
from server.config.parse_profile import resolve_embedding_config
from server.config.settings import resolve_parse_max_height
from server.repository.video_parse import VideoParseRepository
from server.service.video_frame_reader import VideoFrameReader

OUT = Path(__file__).resolve().parent
SERIES = '836fa9f0-2bf4-50fb-9c64-75526fa1dea9'
QUERIES = [
    ('content', '从高处斜俯视房间全景，一个粉色长发少女坐在画面右下角的沙发上抱着电吉他，中央是长方形茶几和大块浅色地毯，左边大片木地板，靠墙有书架和柜子。'),
    ('layout', '斜俯拍的室内全景，人物很小，位于画面右下角，左侧及中央大面积地面留白，家具与房间边界形成对角线。'),
    ('broad', '高位俯视大空间的全景，小人物靠画面边缘，地面占画面的大部分，场景线条沿对角线延伸。'),
]


def main():
    if (OUT / 'results.json').exists():
        raise RuntimeError('Existing run must not be overwritten')
    logging.disable(logging.CRITICAL)
    (OUT / 'frames').mkdir(exist_ok=True)
    reference = OUT.parent / '20261017-chat-composition/reference.jpg'
    shutil.copy2(reference, OUT / 'reference.jpg')
    settings = configure()
    gateway = load_config()
    embedding = resolve_embedding_config(gateway, dimensions=settings.text_vector_dimensions)
    contract = _event_contract(gateway, embedding, max_height=resolve_parse_max_height())
    inputs = dict(conversation_id=str(uuid4()), assistant_message_id=str(uuid4()),
        tenant_id='local-default', series_ids=[SERIES], series_context=[], user_message='参考图构图检索对照',
        history=[], requested_top_k=10)
    canvas = VideoSearchContextCanvas(inputs['tenant_id'], (SERIES,))
    control = ExecutionControl(monotonic() + 600)
    calls = []
    report = dict(created_at=datetime.now(timezone.utc).isoformat(), reference='reference.jpg',
        reference_sha256=hashlib.sha256(reference.read_bytes()).hexdigest(),
        query_modality='text', input_explanation='Assistant inspected reference and wrote the exact queries below; reference pixels were not embedded.',
        tools=['search_video_events', 'inspect_video_frames'], contract_id=contract.contract_id, queries=[])
    client = GatewayClient(gateway)
    try:
        with open_database_storage(settings.database_url, environment=settings.environment,
            pool_recycle_seconds=3600) as (_, sessions), open_elasticsearch_client(settings.elasticsearch) as es:
            repo = VideoParseRepository(sessions)
            store = build_event_elasticsearch_storage(es, settings.elasticsearch, environment=settings.environment,
                semantic_dimensions=settings.text_vector_dimensions, vision_dimensions=settings.vision_vector_dimensions)
            service = build_event_retrieval_service(repo, store, gateway_config=gateway, embedding_config=embedding,
                retrieval_config=settings.retrieval, contract=contract, request_timeout_seconds=60, gateway_client=client)
            search = build_search_video_events_tool(service.search, canvas=canvas, inputs=inputs, control=control,
                calls=calls, role='query', maximum_result_bytes=32000, maximum_catalog_items=10)
            artifacts = build_artifact_storage(settings.artifact_root, environment=settings.environment)
            frames = build_inspect_video_frames_tool(reader=VideoFrameReader(repo, artifacts),
                session=FrameInspectionSession(30), canvas=canvas, inputs=inputs, control=control,
                role='query', max_height=720, maximum_result_bytes=16000)
            report['tool_input_schema'] = search.args_schema.model_json_schema()
            inspected = {}
            for key, query in QUERIES:
                args = dict(query=query, top_k=10, channel_weights=dict(lexical=0, semantic=0, visual=1))
                start = monotonic()
                raw = search.invoke(args)
                page = json.loads(raw)
                if page.get('error'):
                    raise RuntimeError(page['error']['code'])
                call = calls[-1]
                assert call.status == 'success' and not call.result.partial and not call.event_trace['rejected']
                (OUT / f'{key}-tool.json').write_text(raw + '\n')
                (OUT / f'{key}-trace.json').write_text(json.dumps(call.event_trace, ensure_ascii=False, indent=2) + '\n')
                hits = {h.candidate.video_chunk_id: h for h in call.result.hits}
                candidates = []
                for rank, match in enumerate(call.event_trace['matched_events'], 1):
                    evidence = hits[match['video_chunk_id']].evidence
                    candidates.append(dict(rank=rank, episode=evidence.episode_order,
                        description=evidence.all_time_events[match['timeline_index']]['description'], **match))
                entry = dict(key=key, arguments=args, retrieval_ms=round((monotonic()-start)*1000),
                    source_chunks=len(call.event_trace['sources']), chunk_groups=len(hits), candidates=candidates, previews=[])
                seen = []
                for c in candidates:
                    # Preserve all raw ranks; avoid using overlapping Event spans as different previews.
                    if any(ep == c['episode'] and c['start_ms'] < end + 1500 and begin - 1500 < c['end_ms'] for ep, begin, end in seen):
                        continue
                    seen.append((c['episode'], c['start_ms'], c['end_ms']))
                    if len(seen) > 7:
                        break
                    identity = (c['video_chunk_id'], c['start_ms'], c['end_ms'])
                    if identity not in inspected:
                        evidence = hits[c['video_chunk_id']].evidence
                        frame_args = dict(video_chunk_id=c['video_chunk_id'], start_ms=max(c['start_ms'], evidence.start_seconds),
                            end_ms=min(c['end_ms'], evidence.end_seconds), frame_count=1)
                        result = frames.execute(**frame_args)
                        assert result.success, result.model_content()
                        payload = result.payload
                        f = payload['frames'][0]
                        path = OUT / 'frames' / f"ep{c['episode']:02d}-{f['requested_timestamp_ms']}.jpg"
                        shutil.copy2(artifacts.resolve(f['artifact_uri']), path)
                        assert hashlib.sha256(path.read_bytes()).hexdigest() == f['sha256']
                        inspected[identity] = dict(image=str(path.relative_to(OUT)), frame_args=frame_args,
                            frame_timestamp_ms=f['requested_timestamp_ms'], sha256=f['sha256'], receipt=payload)
                    entry['previews'].append(dict(**c, **inspected[identity]))
                report['queries'].append(entry)
                (OUT / 'results.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
                print(json.dumps(dict(query=key, count=len(candidates), previews=len(entry['previews']), hit1={
                    k:candidates[0][k] for k in ('episode','start_ms','end_ms','description','channel_scores')}), ensure_ascii=False), flush=True)
    finally:
        client.close()


if __name__ == '__main__':
    main()
