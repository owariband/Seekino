"""Smoke host for PR 1. PR 2 will call the same library API from its UI."""
import argparse
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path

def _reject_child_process(event, _args):
    if event in {"subprocess.Popen", "os.system", "os.posix_spawn", "os.fork", "os.forkpty", "os.exec"}:
        raise RuntimeError(f"embedded backend attempted to create a process: {event}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("library", type=Path)
    parser.add_argument("--import-file", type=Path)
    parser.add_argument("--series", default="Seekino")
    parser.add_argument("--episode", type=int, default=1)
    parser.add_argument("--request-key")
    parser.add_argument("--query")
    parser.add_argument("--keywords-only", action="store_true")
    parser.add_argument("--task")
    parser.add_argument("--verify-process", action="store_true", help="fail on Python child-process or exec audit events")
    args = parser.parse_args()
    if args.verify_process:
        sys.addaudithook(_reject_child_process)
    from .runtime import open_library
    if args.import_file and not args.request_key:
        parser.error("--import-file requires a stable --request-key")
    with open_library(args.library) as library:
        report = {"pid": os.getpid(), "library": str(args.library.resolve())}
        if args.import_file or args.query:
            series = library.create_series(args.series)
        if args.import_file:
            upload = library.import_file(args.import_file, series_id=series.series_id,
                                         episode_order=args.episode, idempotency_key=args.request_key)
            report.update(video_id=upload.video_id, task_id=upload.video_parse_task_id)
        if args.task:
            report["status"] = library.task(args.task).task.status
        if args.query:
            result = library.search(args.query, series_ids=[series.series_id], keywords_only=args.keywords_only)
            report["search"] = asdict(result)
            if result.hits:
                source = library.open_result(result.hits[0].source_ref)
                report["playback"] = {**asdict(source), "path": str(source.path)}
        print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
