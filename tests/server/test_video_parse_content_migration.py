from __future__ import annotations

from copy import deepcopy

import pytest
from sqlalchemy import inspect, select

from scripts.migrate_video_parse_content import migrate
from server.constants.parse import RUNTIME_DETAIL_KEY
from server.database import SchemaUpgradeRequiredError, create_schema, create_session_factory
from server.model.task import VideoParseTask
from server.model.parse_content import VideoParseContent
from server.repository import video_parse_content as result_store
from tests.server.test_video_parse_queue import _whole_video_asr_task
from tests.server.test_video_parse_results import PAYLOAD


LEGACY = {
    "status": "COMPLETE", "lease_version": 7, "source_fingerprint": "source-chunks",
    "finished_at_ms": 1770000000000, "retry_count": 1,
    "result": {"description": "historical summary", "cover": {"inspection_id": "receipt", "frame_id": "frame"}},
    "frame_receipts": [{"inspection_id": "receipt", "frames": [{"requested_timestamp_ms": 1000}]}],
}


def _legacy(engine):
    factory = create_session_factory(engine)
    task_id = _whole_video_asr_task(factory)
    with factory.begin() as session:
        row = session.get(VideoParseTask, task_id)
        row.detail = {"keep": "audit", RUNTIME_DETAIL_KEY: {
            "accepted": True, "summarize": deepcopy(LEGACY), "other": {"preserve": True}}}
    with factory() as session:
        row = session.get(VideoParseTask, task_id)
        original = {name: deepcopy(getattr(row, name)) for name in (
            "status", "parse_config", "version", "lease_version", "create_time", "create_date", "update_time", "update_date")}
    return factory, task_id, original


def test_controlled_migration_dry_run_then_preserves_summary_and_task_audit(repository):
    engine = repository[0]
    factory, task_id, original = _legacy(engine)
    VideoParseContent.__table__.drop(engine)
    before = inspect(engine).get_table_names()
    plan = migrate(engine)
    assert plan == {"apply": False, "table_exists": False, "summary_candidates": 1, "migrated": 0, "add_diarization_column": False}
    assert inspect(engine).get_table_names() == before
    with pytest.raises(SchemaUpgradeRequiredError, match="video_parse_content"):
        create_schema(engine)
    with pytest.raises(ValueError, match="writers-stopped"):
        migrate(engine, apply=True)
    applied = migrate(engine, apply=True, writers_stopped=True)
    assert applied["migrated"] == 1
    create_schema(engine)
    with factory() as session:
        task = session.get(VideoParseTask, task_id)
        assert {name: getattr(task, name) for name in original} == original
        runtime = task.detail[RUNTIME_DETAIL_KEY]
        assert runtime["summarize"] == {k: v for k, v in LEGACY.items() if k not in {"result", "frame_receipts"}}
        assert runtime["other"] == {"preserve": True} and task.detail["keep"] == "audit"
        assert result_store.load_result(session, task_id, "summary") == {
            "submission": LEGACY["result"], "frame_receipts": LEGACY["frame_receipts"], "cover_uri": None}
        assert result_store.load_result(session, task_id, "asr") is None
        assert result_store.get_metadata(session, task_id).detail["summary"]["legacy_cover_uri_unavailable"] is True
    assert migrate(engine, apply=True, writers_stopped=True)["migrated"] == 0


def test_migration_preserves_existing_asr_and_rolls_back_on_conflicting_summary(repository):
    engine = repository[0]
    factory, task_id, _ = _legacy(engine)
    with factory.begin() as session:
        result_store.save_result(session, task_id, field="asr", result=PAYLOAD,
                                 detail={"asr": {"audit": "original"}}, now_ms=1)
    assert migrate(engine, apply=True, writers_stopped=True)["migrated"] == 1
    with factory() as session:
        assert result_store.load_result(session, task_id, "asr") == PAYLOAD
        assert result_store.get_metadata(session, task_id).detail["asr"] == {"audit": "original"}
    with factory.begin() as session:
        task = session.get(VideoParseTask, task_id)
        task.detail = {RUNTIME_DETAIL_KEY: {"summarize": {**LEGACY, "result": {"description": "different"}}}}
    with pytest.raises(ValueError, match="differs"):
        migrate(engine, apply=True, writers_stopped=True)
    with factory() as session:
        assert session.get(VideoParseTask, task_id).detail[RUNTIME_DETAIL_KEY]["summarize"]["result"] == {"description": "different"}
        assert result_store.load_result(session, task_id, "summary")["submission"] == LEGACY["result"]


def test_summary_failure_rolls_back_video_projection_and_result_together(repository, monkeypatch):
    from tests.server.test_video_summarize_persistence import _ready, _claim, _result, AT
    from server.model.video import Video

    factory, _, repo, task_id, _ = _ready(repository)
    lease = _claim(repo)
    with factory.begin() as session:
        result_store.save_result(session, task_id, field="asr", result=PAYLOAD,
                                 detail={"asr": {"audit": "original"}}, now_ms=1)
    save = result_store.save_result
    def failure(*args, **kwargs):
        save(*args, **kwargs)
        raise RuntimeError("database write interrupted")
    monkeypatch.setattr(result_store, "save_result", failure)
    with pytest.raises(RuntimeError, match="interrupted"):
        repo.complete_summary(lease, _result(), cover_uri="artifact://cover", now=AT)
    with factory() as session:
        video = session.get(Video, lease.video_id)
        assert video.display_name is None and video.cover_uri is None and video.episode_order == 1
        assert session.get(VideoParseTask, task_id).detail[RUNTIME_DETAIL_KEY]["summarize"]["status"] == "RUNNING"
        assert result_store.load_result(session, task_id, "summary") is None
        assert result_store.load_result(session, task_id, "asr") == PAYLOAD
    monkeypatch.setattr(result_store, "save_result", save)
    assert repo.complete_summary(lease, _result(), cover_uri="artifact://cover", now=AT)
    with factory() as session:
        assert result_store.load_result(session, task_id, "asr") == PAYLOAD
        assert result_store.get_metadata(session, task_id).detail["asr"] == {"audit": "original"}


def test_startup_rejects_video_result_constraint_drift(repository, monkeypatch):
    import server.database as database

    engine = repository[0]
    inspector = inspect(engine)
    original = inspector.get_unique_constraints
    monkeypatch.setattr(inspector, "get_unique_constraints", lambda table: [] if table == "video_parse_content" else original(table))
    monkeypatch.setattr(database, "inspect", lambda _: inspector)
    with pytest.raises(SchemaUpgradeRequiredError, match="video_parse_content.unique"):
        create_schema(engine)
