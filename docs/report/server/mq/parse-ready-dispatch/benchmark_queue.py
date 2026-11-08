"""Drive identical mixed 10-slot batches through either checkout's queue in an isolated test DB."""
import argparse
import json
import os
from datetime import timedelta
from time import perf_counter

from sqlalchemy import event
from sqlalchemy.orm import Session
from server.database import DatabaseEnvironment, create_database_engine, create_schema, create_session_factory
from server.model.task import ChunkParseTask, IntervalParseTask
from server.mq.video_parse_queue import MySqlVideoParseQueue
from server.service.video_parse_persistence import VideoParsePersistenceService
from tests.server.test_video_parse_queue import NOW, _prepare, _task
from tests.server.test_video_parse_repository import _finish_index, _refinement_inputs


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dispatch", action="store_true")
    parser.add_argument("--chunks", type=int, default=146)
    args = parser.parse_args()
    db = os.environ["SEEKINO_TEST_DATABASE_URL"]
    engine = create_database_engine(db, environment=DatabaseEnvironment.TEST)
    assert engine.url.database == "seekino_ready_benchmark_test"
    create_schema(engine)
    factory = create_session_factory(engine)
    options = {}
    if args.dispatch:
        from server.service.video_parse_dispatch import ParseDispatch
        options["dispatch"] = ParseDispatch(factory, reconcile_interval=timedelta(seconds=30))
    queue = MySqlVideoParseQueue(factory, video_concurrency=1, **options)
    repo = VideoParsePersistenceService(factory, **options)
    task = _task(factory, chunk_count=args.chunks, accepted=True, batch_size=10)
    _, parent, _ = _prepare(factory, queue, task, chunk_count=args.chunks)
    claims = (queue.claim_chunk, queue.claim_interval_refine, queue.claim_chunk_index)
    claim_counts, candidate_reads = [0, 0, 0], [0, 0, 0]
    sql_count = 0
    current_phase = None

    @event.listens_for(engine, "before_cursor_execute")
    def count_sql(*_):
        nonlocal sql_count
        sql_count += 1

    @event.listens_for(Session, "loaded_as_persistent")
    def count_candidate(_session, obj):
        model = IntervalParseTask if current_phase == 1 else ChunkParseTask
        if current_phase is not None and isinstance(obj, model):
            candidate_reads[current_phase] += 1

    started = perf_counter()
    while claim_counts[2] < args.chunks:
        jobs = []
        for stage, claim in enumerate(claims):
            current_phase = stage
            for slot in range(10):
                lease = claim("worker", video_parse_task_id=task, lease_duration=timedelta(minutes=5), now=NOW)
                if lease is None:
                    break
                jobs.append((stage, lease))
                claim_counts[stage] += 1
            current_phase = None
        assert jobs, "ready work was stranded"
        for stage, lease in jobs:
            if stage == 0:
                repo.commit_chunk_result(lease, content={"persons": [], "timeline": []}, now=NOW)
            elif stage == 1:
                repo.commit_interval_refinement(lease, **_refinement_inputs(repo, lease, now=NOW),
                                               interval_content={"updates": []}, member_updates={}, now=NOW)
            else:
                _finish_index(repo, lease, now=NOW)
    assert repo.aggregate_task(parent, now=NOW).status == "COMPLETE"
    assert claim_counts == [args.chunks, max(1, args.chunks - 2), args.chunks]
    print(json.dumps({"dispatch": args.dispatch, "elapsed_s": round(perf_counter()-started, 3),
                      "claims": claim_counts, "candidate_rows": candidate_reads, "sql_statements": sql_count}))


if __name__ == "__main__":
    main()
