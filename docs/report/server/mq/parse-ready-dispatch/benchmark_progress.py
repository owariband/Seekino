"""Compare old/full and new/projected reads of the same completed test episode."""
import argparse
import gc
import json
import os
from statistics import median
from time import perf_counter

from sqlalchemy.orm import Session
from server.database import DatabaseEnvironment, create_database_engine
from server.repository import video_parse_chunk as chunks


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("task_id")
    parser.add_argument("--repeats", type=int, default=5)
    args = parser.parse_args()
    engine = create_database_engine(os.environ["SEEKINO_TEST_DATABASE_URL"], environment=DatabaseEnvironment.TEST)
    timings = {"full": [], "projection": []}
    reference = None
    for run in range(args.repeats):
        for variant in (("full", "projection") if run % 2 == 0 else ("projection", "full")):
            gc.collect()
            with Session(engine) as session:
                started = perf_counter()
                if variant == "full":
                    raw = chunks.list_task_chunk_content_rows(session, args.task_id, include_missing_content=True)
                    rows = [chunks.ChunkProgress(
                        child.id, chunk.video_id, chunk.sequence_no, child.phase, child.status, child.lease_owner,
                        child.lease_version, child.next_execute_time, bool(content and content.content),
                        content.version if content else None, child.indexed_content_version,
                    ) for child, chunk, content in raw]
                    del raw
                else:
                    rows = chunks.list_progress(session, args.task_id)
                timings[variant].append(round((perf_counter() - started) * 1000, 2))
                assert rows and (reference is None or rows == reference)
                reference = rows
    print(json.dumps({"rows": len(reference), "samples_ms": timings,
                      "median_ms": {k: median(v) for k, v in timings.items()}}, indent=2))
    engine.dispose()


if __name__ == "__main__":
    main()
