from __future__ import annotations

import json
from copy import deepcopy

import pytest
from sqlalchemy import inspect, text

from scripts.migrate_video_parse_display import migrate
from server.database import SchemaUpgradeRequiredError, create_schema, create_session_factory
from server.model.task import VideoParseTask
from server.model.video import Video
from server.repository import video_parse_content as result_store
from tests.server.test_video_parse_queue import _whole_video_asr_task


OLD_DISPLAY = {
    "submission": {
        "title": "旧标题", "title_kind": "official", "description": "旧简介",
        "episode_order": 1,
        "evidence": [{"field": "title", "chunk_parse_task_id": "source", "quote": "旧引用"}],
    },
    "cover_uri": None,
    "frame_receipts": [{"inspection_id": "receipt", "frames": []}],
}

CONVERTED = {
    "title": "旧标题", "title_kind": "official", "description": "旧简介",
    "episode_order": 1, "cover_uri": None,
    "evidence": [{"field": "title", "chunk_parse_task_id": "source", "quote": "旧引用"}],
    "frame_receipts": [{"inspection_id": "receipt", "frames": []}],
}


def _legacy_display_db(repository, *, cover_uri=None, display_name="旧标题", description="旧简介"):
    """Create a video/task/content row in the old summary column, then rename the column."""
    engine = repository[0]
    factory = create_session_factory(engine)
    task_id = _whole_video_asr_task(factory)
    with factory.begin() as session:
        video_id = session.get(VideoParseTask, task_id).video_id
        video = session.get(Video, video_id)
        video.cover_uri = cover_uri
        video.display_name = display_name
        video.description = description
        result_store.save_result(
            session, task_id, field="display", result=deepcopy(OLD_DISPLAY),
            detail={"summary": {"audit": "preserved"}}, now_ms=1,
        )
    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE video_parse_content RENAME COLUMN display TO summary"))
    return factory, task_id, video_id


def test_startup_guard_rejects_legacy_summary_column(repository):
    engine = repository[0]
    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE video_parse_content RENAME COLUMN display TO summary"))
    with pytest.raises(SchemaUpgradeRequiredError, match="migrate_video_parse_display"):
        create_schema(engine)


def test_display_migration_preview_rename_convert_and_cover_fill(repository):
    engine = repository[0]
    factory, task_id, _video_id = _legacy_display_db(
        repository, cover_uri="artifact://cover", display_name="旧标题", description="旧简介",
    )
    plan = migrate(engine)
    assert plan == {
        "apply": False, "rename_column": True, "rows_to_convert": 1,
        "covers_to_fill": 1, "conflicts": 0,
    }
    assert {"summary"} <= {column["name"] for column in inspect(engine).get_columns("video_parse_content")}
    with pytest.raises(ValueError, match="writers-stopped"):
        migrate(engine, apply=True)
    applied = migrate(engine, apply=True, writers_stopped=True)
    assert applied["rename_column"] is True and applied["rows_to_convert"] == 1
    columns = {column["name"] for column in inspect(engine).get_columns("video_parse_content")}
    assert "display" in columns and "summary" not in columns
    with factory() as session:
        assert result_store.load_result(session, task_id, "display") == {
            **CONVERTED, "cover_uri": "artifact://cover"}
        assert result_store.get_metadata(session, task_id).detail == {"display": {"audit": "preserved"}}


def test_display_migration_conflict_aborts_apply(repository):
    engine = repository[0]
    factory, task_id, _video_id = _legacy_display_db(
        repository, display_name="不同的标题", description="旧简介",
    )
    plan = migrate(engine)
    assert plan["conflicts"] == 1 and plan["rows_to_convert"] == 1
    with pytest.raises(ValueError, match="conflict"):
        migrate(engine, apply=True, writers_stopped=True)
    columns = {column["name"] for column in inspect(engine).get_columns("video_parse_content")}
    assert "summary" in columns and "display" not in columns
    with factory() as session:
        raw = session.execute(text("SELECT summary FROM video_parse_content")).scalar()
        assert json.loads(raw) == deepcopy(OLD_DISPLAY)


def test_display_migration_idempotent_rerun(repository):
    engine = repository[0]
    factory, task_id, _video_id = _legacy_display_db(
        repository, cover_uri="artifact://cover", display_name="旧标题", description="旧简介",
    )
    assert migrate(engine, apply=True, writers_stopped=True)["rows_to_convert"] == 1
    rerun = migrate(engine)
    assert rerun == {
        "apply": False, "rename_column": False, "rows_to_convert": 0,
        "covers_to_fill": 0, "conflicts": 0,
    }
    assert migrate(engine, apply=True, writers_stopped=True)["rows_to_convert"] == 0
    with factory() as session:
        assert result_store.load_result(session, task_id, "display") == {
            **CONVERTED, "cover_uri": "artifact://cover"}
