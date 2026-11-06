"""Rename video display metadata and normalize its JSON shape; dry-run by default.

Stop old writers and back up MySQL before --apply --writers-stopped. The column
rename is DDL (implicitly commits); the data rewrite is a separate, idempotent
transaction that never overwrites a conflicting title/description on video.
"""
from __future__ import annotations

import argparse
import json
import os
from copy import deepcopy
from datetime import UTC, datetime

from sqlalchemy import inspect, select, text, update
from sqlalchemy.orm import Session

from server.caller.mysql import create_database_engine
from server.database import DatabaseEnvironment
from server.model.parse_content import VideoParseContent
from server.model.task import VideoParseTask
from server.model.video import Video
from server.utils.time import datetime_to_unix_time_ms


def _is_old_shape(value: object) -> bool:
    return isinstance(value, dict) and "submission" in value


def _convert(value: dict) -> dict:
    submission = value.get("submission")
    if not isinstance(submission, dict):
        submission = {}
    return {
        "title": submission.get("title"),
        "title_kind": submission.get("title_kind"),
        "description": submission.get("description"),
        "episode_order": submission.get("episode_order"),
        "cover_uri": value.get("cover_uri"),
        "evidence": deepcopy(submission.get("evidence", [])),
        "frame_receipts": deepcopy(value.get("frame_receipts", [])),
    }


def _json(value: object):
    if value is None or not isinstance(value, str):
        return value
    return json.loads(value)


def _load_content_rows(session: Session, column: str) -> list[tuple[str, str, object, object]]:
    rows = session.execute(text(
        f"SELECT id, video_task_id, {column}, detail FROM video_parse_content"
    )).all()
    return [(row[0], row[1], _json(row[2]), _json(row[3])) for row in rows]


def migrate(engine, *, apply: bool = False, writers_stopped: bool = False) -> dict:
    if apply and not writers_stopped:
        raise ValueError("stop old writers and pass --writers-stopped before applying")
    if "video_parse_task" not in inspect(engine).get_table_names():
        raise ValueError("existing Video Parse task table is required")
    if not inspect(engine).has_table("video_parse_content"):
        raise ValueError("existing video_parse_content table is required")

    inspector = inspect(engine)
    columns = {column["name"] for column in inspector.get_columns("video_parse_content")}
    if "summary" in columns and "display" in columns:
        raise ValueError("video_parse_content has both summary and display columns; resolve manually")
    rename_column = "summary" in columns and "display" not in columns
    content_column = "display" if "display" in columns else "summary"
    add_outline_column = "outline" not in columns

    with Session(engine) as session:
        content_rows = _load_content_rows(session, content_column)
        tasks = {
            row.id: (row.video_id, row.version, row.is_deleted)
            for row in session.execute(select(
                VideoParseTask.id, VideoParseTask.video_id,
                VideoParseTask.version, VideoParseTask.is_deleted,
            )).all()
        }
        videos = {
            row.id: row
            for row in session.execute(select(
                Video.id, Video.display_name, Video.description, Video.cover_uri,
            )).all()
        }

    # Latest display row per video: highest task version among non-deleted tasks
    # whose display body is not null.
    by_video: dict[str, list[tuple[int, str, str, object, object]]] = {}
    for content_id, video_task_id, display_value, detail_value in content_rows:
        task = tasks.get(video_task_id)
        if task is None or task[2] or display_value is None:
            continue
        by_video.setdefault(task[0], []).append(
            (task[1], video_task_id, content_id, display_value, detail_value),
        )
    latest = {
        video_id: max(entries, key=lambda item: (item[0], item[1]))
        for video_id, entries in by_video.items()
    }

    converted_by_id: dict[str, dict] = {}
    detail_renames: dict[str, dict] = {}
    for content_id, _task_id, display_value, detail_value in content_rows:
        if _is_old_shape(display_value):
            converted_by_id[content_id] = _convert(display_value)
        detail = dict(deepcopy(detail_value)) if isinstance(detail_value, dict) else None
        if detail is not None and "summary" in detail:
            detail["display"] = detail.pop("summary")
            detail_renames[content_id] = detail

    covers_to_fill = 0
    conflicts = 0
    cover_fills: dict[str, str] = {}
    latest_display: dict[str, dict] = {}
    for video_id, (_version, _task_id, content_id, display_value, _detail) in latest.items():
        final = deepcopy(converted_by_id.get(content_id, display_value))
        latest_display[content_id] = final
        video = videos.get(video_id)
        if video is None:
            continue
        if not final.get("cover_uri") and video.cover_uri:
            covers_to_fill += 1
            cover_fills[content_id] = video.cover_uri
        if (
            (final.get("title") is not None and final["title"] != video.display_name)
            or (final.get("description") is not None and final["description"] != video.description)
        ):
            conflicts += 1

    plan = {
        "apply": apply,
        "rename_column": rename_column,
        "add_outline_column": add_outline_column,
        "rows_to_convert": len(converted_by_id),
        "covers_to_fill": covers_to_fill,
        "conflicts": conflicts,
    }
    if not apply:
        return plan
    if conflicts:
        raise ValueError(
            f"{conflicts} video display row(s) conflict with video metadata; resolve before applying"
        )

    # DDL is deliberately separate from the data transaction; reruns are idempotent.
    if rename_column:
        with engine.begin() as connection:
            connection.execute(
                text("ALTER TABLE video_parse_content RENAME COLUMN summary TO display")
            )
    if add_outline_column:
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE video_parse_content ADD COLUMN outline JSON NULL"))

    writes: dict[str, dict] = {content_id: {"display": display}
                               for content_id, display in converted_by_id.items()}
    for content_id, detail in detail_renames.items():
        writes.setdefault(content_id, {})["detail"] = detail
    for content_id, cover_uri in cover_fills.items():
        display = dict(writes.get(content_id, {}).get("display", latest_display[content_id]))
        display["cover_uri"] = cover_uri
        writes.setdefault(content_id, {})["display"] = display

    now_ms = datetime_to_unix_time_ms(datetime.now(UTC))
    with Session(engine) as session, session.begin():
        for content_id, values in writes.items():
            session.execute(
                update(VideoParseContent)
                .where(VideoParseContent.id == content_id)
                .values(**values, update_time=now_ms)
            )
    return plan


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--writers-stopped", action="store_true")
    args = parser.parse_args()
    engine = create_database_engine(
        os.environ["SEEKINO_DATABASE_URL"],
        environment=DatabaseEnvironment(os.environ.get("SEEKINO_ENVIRONMENT", "development")),
    )
    try:
        print(json.dumps(migrate(engine, apply=args.apply, writers_stopped=args.writers_stopped)))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
