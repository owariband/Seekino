"""Create video results and move legacy Summary bodies; dry-run by default.

Stop old writers and back up MySQL before --apply --writers-stopped.
An absent ASR is never reconstructed from Chunk evidence or experiment files.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, UTC
import json
import os

from sqlalchemy import inspect, select, text, update
from sqlalchemy.orm import Session

from server.caller.mysql import create_database_engine
from server.constants.parse import RUNTIME_DETAIL_KEY
from server.database import DatabaseEnvironment
from server.model.task import VideoParseTask
from server.model.parse_content import VideoParseContent
from server.repository import video_parse_content as result_store
from server.utils.time import datetime_to_unix_time_ms


def legacy_summary(detail: dict) -> dict | None:
    state = (detail or {}).get(RUNTIME_DETAIL_KEY, {}).get("summarize", {})
    if not isinstance(state.get("result"), dict):
        return None
    # Video.cover_uri may already belong to a different parse or a manual edit.
    return {"submission": deepcopy(state["result"]), "cover_uri": None,
            "frame_receipts": deepcopy(state.get("frame_receipts", []))}


def migrate(engine, *, apply: bool = False, writers_stopped: bool = False) -> dict:
    if apply and not writers_stopped:
        raise ValueError("stop old writers and pass --writers-stopped before applying")
    if "video_parse_task" not in inspect(engine).get_table_names():
        raise ValueError("existing Video Parse task table is required")
    with Session(engine) as session:
        rows = session.execute(select(VideoParseTask.id, VideoParseTask.detail)).all()
        candidates = [row.id for row in rows if legacy_summary(row.detail) is not None]
    plan = {"apply": apply, "table_exists": inspect(engine).has_table("video_parse_content"),
            "summary_candidates": len(candidates), "migrated": 0}
    existing_columns = ({column["name"] for column in inspect(engine).get_columns("video_parse_content")}
                        if plan["table_exists"] else set())
    plan["add_diarization_column"] = plan["table_exists"] and "diarization" not in existing_columns
    if not apply:
        return plan
    # DDL is deliberately separate from data transactions; reruns are idempotent.
    VideoParseContent.__table__.create(engine, checkfirst=True)
    if plan["add_diarization_column"]:
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE video_parse_content ADD COLUMN diarization JSON NULL"))
    now_ms = datetime_to_unix_time_ms(datetime.now(UTC))
    with Session(engine) as session, session.begin():
        for task_id in candidates:
            row = session.execute(select(VideoParseTask.id, VideoParseTask.detail).where(
                VideoParseTask.id == task_id).with_for_update()).one()
            result = legacy_summary(row.detail)
            if result is None:
                continue
            metadata = result_store.get_metadata(session, task_id, for_update=True)
            existing = result_store.load_result(session, task_id, "summary") if metadata else None
            if existing is not None and existing != result:
                raise ValueError(f"existing video Summary differs from legacy result: {task_id}")
            detail = deepcopy(row.detail)
            state = detail[RUNTIME_DETAIL_KEY]["summarize"]
            if existing is None:
                result_detail = deepcopy(metadata.detail if metadata else {})
                result_detail["summary"] = {
                    "source_fingerprint": state.get("source_fingerprint"),
                    "committed_lease_version": state.get("lease_version"),
                    "completed_at_ms": state.get("finished_at_ms"),
                    "migrated_from_task_detail": True,
                    "legacy_cover_uri_unavailable": result["submission"].get("cover") is not None,
                }
                result_store.save_result(session, task_id, field="summary", result=result,
                                         detail=result_detail, now_ms=now_ms)
            state.pop("result", None)
            state.pop("frame_receipts", None)
            session.execute(update(VideoParseTask).where(VideoParseTask.id == task_id).values(
                detail=detail, update_time=VideoParseTask.update_time, update_date=VideoParseTask.update_date))
            plan["migrated"] += 1
    return plan


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--writers-stopped", action="store_true")
    args = parser.parse_args()
    engine = create_database_engine(os.environ["SEEKINO_DATABASE_URL"],
                                    environment=DatabaseEnvironment(os.environ.get("SEEKINO_ENVIRONMENT", "development")))
    try:
        print(json.dumps(migrate(engine, apply=args.apply, writers_stopped=args.writers_stopped)))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
