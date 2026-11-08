"""Replay two completed Responses streams through Parse control and a private MySQL queue."""
import argparse
import asyncio
import hashlib
import json
import os
from collections import Counter
from contextvars import ContextVar
from datetime import timedelta
from pathlib import Path
from time import perf_counter

import httpx
from openai import OpenAI
from sqlalchemy import delete, event

from agent.video_parse_v2.input import ParseConfig
from gateway.multimodal import TextInput
from gateway.streaming.responses import request_response
from gateway.usage import track_model_call
from server.database import DatabaseEnvironment, create_database_engine, create_schema, create_session_factory
from server.model.video import Series, Video
from server.model.task import VideoParseTask
from server.mq.video_parse_queue import MySqlVideoParseQueue
from server.service.video_parse import VideoParseRuntimeService
from server.service.video_parse_model_calls import VideoParseModelCallManager
from server.service.video_parse_persistence import VideoParsePersistenceService
from server.utils.time import now_utc
from tests.server.test_video_parse_queue import _task


def replay(runtime, model, manager, trace, role, engine):
    payloads = []
    with trace.open() as stream:
        for line in stream:
            record = json.loads(line)
            if record['event'] == 'sse':
                payloads.append(record['payload'])
    assert payloads[-1]['type'] == 'response.completed'
    expected = ''.join(p['delta'] for p in payloads if p['type'] == 'response.output_text.delta')
    wire = ''.join('event: ' + p['type'] + '\ndata: ' + json.dumps(p) + '\n\n' for p in payloads).encode()
    transport = httpx.MockTransport(
        lambda _: httpx.Response(200, content=wire, headers={'Content-Type': 'text/event-stream'})
    )
    sdk = OpenAI(
        api_key='local-replay', base_url='https://replay.invalid/v1',
        http_client=httpx.Client(transport=transport),
    )
    counts = Counter()

    def count(_conn, _cursor, sql, *_):
        counts['sql'] += 1
        sql = sql.lower()
        if sql.startswith('update ') and 'lease_expire_time' in sql:
            counts['renewals'] += 1
        if 'from video_parse_task' in sql and 'for update' in sql:
            counts['parent_locks'] += 1

    class RecordedModel:
        def generate(self, inputs, *, check_control, **_):
            with track_model_call(provider='replay', model='recording', operation='parse') as call:
                event.listen(engine, 'before_cursor_execute', count)
                try:
                    _, output = request_response(
                        sdk.responses, stream=True, usage_call=call, check_control=check_control,
                        model='recording', input='replay',
                    )
                    return output
                finally:
                    event.remove(engine, 'before_cursor_execute', count)

        def close(self):
            pass

    class Gateway:
        def create_generative_model(self, *_, **__):
            return RecordedModel()

    runtime._gateway = Gateway()
    token = runtime._model_manager.set(manager)
    try:
        with manager.capture():
            start = perf_counter()
            result = asyncio.run(runtime._generate_multimodal(
                model, [TextInput('replay')], schema_name='replay', result_type=dict,
            ))
            seconds = round(perf_counter() - start, 4)
        assert result == expected
        return dict(stage=role, events=len(payloads), seconds=seconds, renewals=counts['renewals'],
                    parent_locks=counts['parent_locks'], statements=counts['sql'],
                    output_sha256=hashlib.sha256(result.encode()).hexdigest())
    finally:
        runtime._model_manager.reset(token)
        sdk.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('understand_trace', type=Path)
    parser.add_argument('refine_trace', type=Path)
    args = parser.parse_args()
    engine = create_database_engine(os.environ['SEEKINO_TEST_DATABASE_URL'], environment=DatabaseEnvironment.TEST)
    assert engine.url.database == 'seekino_mygo_state_pilot_test' and engine.url.query.get('unix_socket')
    create_schema(engine)
    factory = create_session_factory(engine)
    queue = MySqlVideoParseQueue(factory, video_concurrency=1)
    repo = VideoParsePersistenceService(factory)
    task = _task(factory, chunk_count=1, accepted=True)
    with factory() as session:
        parent = session.get(VideoParseTask, task)
        series_id = session.get(Video, parent.video_id).series_id
        models = ParseConfig.model_validate(parent.parse_config).models
    try:
        duration = timedelta(minutes=15)
        parent = queue.claim_video('replay', lease_duration=duration)
        assert parent.video_parse_task_id == task
        chunks = repo.ensure_video_chunks(parent, [(0, 0, 10000)])
        repo.ensure_parse_tasks(parent, [chunks[0].video_chunk_id], [(0, 1)])
        repo.activate_video_task(parent)
        runtime = object.__new__(VideoParseRuntimeService)
        runtime._model_manager = ContextVar('replay-manager', default=None)
        for role, trace in [('understand', args.understand_trace), ('refine', args.refine_trace)]:
            claim = queue.claim_chunk if role == 'understand' else queue.claim_interval_refine
            pulse = queue.heartbeat_chunk if role == 'understand' else queue.heartbeat_interval
            lease = claim('replay', video_parse_task_id=task, lease_duration=duration)
            assert lease is not None
            binding = (models.refine or models.understanding) if role == 'refine' else models.understanding

            def check():
                assert pulse(lease, lease_duration=duration, now=now_utc())

            manager = VideoParseModelCallManager(repo, execution_lease=lease, lease_duration=duration, check_active=check)
            print(json.dumps(replay(runtime, binding, manager, trace, role, engine)), flush=True)
            if role == 'understand':
                repo.commit_chunk_result(lease, content={'persons': [], 'timeline': []})
    finally:
        with factory.begin() as session:
            session.execute(delete(Series).where(Series.id == series_id))
        engine.dispose()


if __name__ == '__main__':
    main()
