"""Small read-only progress sample; baseline pins the 13 authorized tasks."""
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

from sqlalchemy import select
from scripts.local_services import configure
from server.caller.mysql import open_database_storage
from server.caller.elasticsearch import open_elasticsearch_client
from server.model.job import VideoParseTask, ChunkTask

directory = Path(__file__).resolve().parent
baseline = json.loads((directory / 'before.json').read_text())
settings = configure()
sample = {'observed_at': datetime.now(timezone.utc).isoformat(), 'episodes': []}
with open_database_storage(settings.database_url, environment=settings.environment,
                           pool_recycle_seconds=3600) as (_, sessions), open_elasticsearch_client(settings.elasticsearch) as client:
    with sessions() as session:
        for episode in baseline['episodes']:
            task = session.get(VideoParseTask, episode['task_id'])
            rows = session.execute(select(ChunkTask.status, ChunkTask.retry_count, ChunkTask.last_error).where(
                ChunkTask.video_task_id == task.id)).all()
            count = client.count(index=baseline['event_index'], query={'term': {'parse_task_id': task.id}})['count']
            sample['episodes'].append({'episode': episode['episode'], 'status': task.status,
                'chunks': dict(Counter(r.status for r in rows)), 'es_events': count,
                'expected_events': episode['expected_events'],
                'retries': sum(r.retry_count for r in rows), 'errors': sum(r.last_error is not None for r in rows)})
with (directory / 'progress.jsonl').open('a') as stream:
    stream.write(json.dumps(sample, ensure_ascii=False) + '\n')
print(json.dumps(sample, ensure_ascii=False))
