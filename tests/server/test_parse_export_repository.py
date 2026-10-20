from __future__ import annotations

from copy import deepcopy
from uuid import uuid4

import pytest
from sqlalchemy import event, select

from server.constants.parse import RUNTIME_DETAIL_KEY
from server.database import create_session_factory
from server.model.job import ChunkParseContent, ChunkParseTask, ChunkTaskPhase, VideoParseTask
from server.model.video import Series, Video, VideoChunk
from server.repository.video_parse import (
    VideoParseConflictError,
    VideoParseRepository,
    VideoParseTaskNotFoundError,
)


def _content(sequence_no):
    return {
        "persons": ["爱音"],
        "timeline": [{
            "event_type": "DIALOGUE", "start_seconds": sequence_no * 10_000 + 500,
            "end_seconds": sequence_no * 10_000 + 1500, "description": "一句问候",
            "participants": ["爱音"], "visible_persons": ["爱音"], "speaker": None,
            "text_variants": [
                {"language": "ja", "role": "ORIGINAL", "text": "こんにちは。"},
                {"language": "zh-Hans", "role": "TRANSLATION", "text": "你好。"},
            ],
        }],
    }


def _seed(factory, *, tenant_id="mine", video_id=None, version=1, status="INDEXING_FAILED"):
    task_id = str(uuid4())
    with factory.begin() as session:
        if video_id is None:
            series_id, video_id = str(uuid4()), str(uuid4())
            session.add(Series(id=series_id, tenant_id=tenant_id, name=series_id,
                               status="INDEXING", character_bible=[]))
            session.flush()
            session.add(Video(
                id=video_id, series_id=series_id, episode_order=1, upload_sequence=1,
                filename="episode.mp4", source_uri="artifact://missing/no-media-read.mp4",
                hash=video_id.replace("-", "") * 2, size_bytes=1, duration_ms=20_000,
                upload_status="COMPLETED", parse_status=status,
                upload_progress=100, parse_progress=0,
            ))
            session.flush()
            for sequence_no in range(2):
                session.add(VideoChunk(
                    id=str(uuid4()), video_id=video_id, sequence_no=sequence_no,
                    start_ms=sequence_no * 10_000, end_ms=(sequence_no + 1) * 10_000,
                    window_start_ms=sequence_no * 10_000, window_end_ms=(sequence_no + 1) * 10_000,
                    chunk_media_ref="missing", analysis_window_media_ref="missing",
                ))
        session.add(VideoParseTask(
            id=task_id, video_id=video_id, pipeline_version="v2", status=status,
            parse_config={"compact_enabled": False}, chunk_count=2, version=version,
            max_attempts=3, is_test=False, is_deleted=False,
            detail={RUNTIME_DETAIL_KEY: {"accepted": True}},
        ))
        session.flush()
        for chunk in session.scalars(select(VideoChunk).where(VideoChunk.video_id == video_id)):
            child = ChunkParseTask(id=str(uuid4()), video_task_id=task_id, chunk_id=chunk.id,
                                   phase=ChunkTaskPhase.INDEX, status="FAILED")
            session.add(child)
            session.flush()
            session.add(ChunkParseContent(id=str(uuid4()), chunk_task_id=child.id,
                                          content=_content(chunk.sequence_no), version=3))
    return video_id, task_id


def _first_content(session, task_id):
    return session.scalar(select(ChunkParseContent).join(
        ChunkParseTask, ChunkParseTask.id == ChunkParseContent.chunk_task_id,
    ).join(VideoChunk, VideoChunk.id == ChunkParseTask.chunk_id).where(
        ChunkParseTask.video_task_id == task_id,
    ).order_by(VideoChunk.sequence_no))


def test_export_preserves_formal_content_without_index_or_media(repository):
    engine, _ = repository
    factory = create_session_factory(engine)
    video_id, task_id = _seed(factory)
    statements = []

    def record_sql(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", record_sql)
    try:
        snapshots = VideoParseRepository(factory).load_export_snapshot(tenant_id="mine", video_ids=[video_id])
    finally:
        event.remove(engine, "before_cursor_execute", record_sql)

    assert len(statements) == 1 and statements[0].lstrip().upper().startswith("SELECT")
    assert len(snapshots) == 2
    assert [item.chunk_sequence_no for item in snapshots] == [0, 1]
    assert all(item.video_id == video_id and item.parse_task_id == task_id for item in snapshots)
    assert snapshots[0].content_version == 3
    assert snapshots[0].content.model_dump(mode="json") == _content(0)
    assert snapshots[1].content.timeline[0].start_seconds == 10_500

    with factory.begin() as session:
        stored = _first_content(session, task_id)
        stored.content = {"persons": [], "timeline": []}
        stored.version += 1
    assert snapshots[0].content_version == 3
    assert snapshots[0].content.timeline[0].text_variants[0].text == "こんにちは。"
    refreshed = VideoParseRepository(factory).load_export_snapshot(tenant_id="mine", video_ids=[video_id])
    assert refreshed[0].content_version == 4 and refreshed[0].content.timeline == []


@pytest.mark.parametrize("by_video", [False, True])
@pytest.mark.parametrize("case", ["unknown", "other_tenant", "deleted"])
def test_export_rejects_missing_or_out_of_scope_source(repository, by_video, case):
    factory = create_session_factory(repository[0])
    video_id, task_id = _seed(factory, tenant_id="other" if case == "other_tenant" else "mine")
    if case == "deleted":
        with factory.begin() as session:
            session.get(VideoParseTask, task_id).is_deleted = True
    source_id = video_id if by_video else task_id
    if case == "unknown":
        source_id = str(uuid4())
    with pytest.raises(VideoParseTaskNotFoundError, match="does not exist in this workspace"):
        VideoParseRepository(factory).load_export_snapshot(
            tenant_id="mine", **{"video_ids" if by_video else "video_parse_task_ids": [source_id]},
        )


@pytest.mark.parametrize("case", ["test", "unaccepted", "unfinished", "prefix", "recorded_failure"])
def test_export_requires_complete_formal_parsing(repository, case):
    factory = create_session_factory(repository[0])
    video_id, task_id = _seed(factory)
    with factory.begin() as session:
        task = session.get(VideoParseTask, task_id)
        if case == "test":
            task.is_test = True
        elif case == "unaccepted":
            task.detail = {RUNTIME_DETAIL_KEY: {"accepted": False, "realtime_only": True}}
        elif case == "unfinished":
            task.status = "PENDING"
        elif case == "prefix":
            # COMPLETE refers to indexing; Prefix can still update formal content.
            task.status = "COMPLETE"
            task.parse_config = {"compact_enabled": True}
            task.detail = {RUNTIME_DETAIL_KEY: {"accepted": True, "prefix": {"next_chunk_sequence_no": 1}}}
        else:
            task.detail = {RUNTIME_DETAIL_KEY: {"accepted": True, "failure_status": "PARSING_FAILED"}}
    with pytest.raises(VideoParseConflictError, match="not (a formal accepted parse|complete)"):
        VideoParseRepository(factory).load_export_snapshot(tenant_id="mine", video_ids=[video_id])


def test_export_accepts_finished_prefix_and_explicit_historical_task(repository):
    factory = create_session_factory(repository[0])
    video_id, old_task_id = _seed(factory)
    _, new_task_id = _seed(factory, video_id=video_id, version=2, status="PENDING")
    with factory.begin() as session:
        task = session.get(VideoParseTask, old_task_id)
        task.parse_config = {"compact_enabled": True}
        task.detail = {RUNTIME_DETAIL_KEY: {"accepted": True, "prefix": {"next_chunk_sequence_no": 2}}}
    repo = VideoParseRepository(factory)
    with pytest.raises(VideoParseConflictError, match="parsing is not complete"):
        repo.load_export_snapshot(tenant_id="mine", video_ids=[video_id])
    assert all(item.parse_task_id == old_task_id for item in repo.load_export_snapshot(
        tenant_id="mine", video_parse_task_ids=[old_task_id],
    ))
    with factory.begin() as session:
        session.get(VideoParseTask, new_task_id).is_deleted = True
    assert all(item.parse_task_id == old_task_id for item in repo.load_export_snapshot(
        tenant_id="mine", video_ids=[video_id],
    ))


@pytest.mark.parametrize("case", ["missing_chunk", "missing_content", "sequence_gap", "unfinished_refine"])
def test_export_rejects_incomplete_chunk_set(repository, case):
    factory = create_session_factory(repository[0])
    video_id, task_id = _seed(factory)
    with factory.begin() as session:
        stored = _first_content(session, task_id)
        child = session.get(ChunkParseTask, stored.chunk_task_id)
        if case == "missing_chunk":
            session.delete(stored)
            session.delete(child)
        elif case == "missing_content":
            session.delete(stored)
        elif case == "sequence_gap":
            session.get(VideoChunk, child.chunk_id).sequence_no = 3
        else:
            child.phase = ChunkTaskPhase.REFINE
            child.status = "WAITING"
    with pytest.raises(VideoParseConflictError, match="(topology is incomplete|content is missing|refinement is not complete)"):
        VideoParseRepository(factory).load_export_snapshot(tenant_id="mine", video_ids=[video_id])


def test_export_preserves_cross_chunk_event_and_canonical_person_rules(repository):
    factory = create_session_factory(repository[0])
    video_id, task_id = _seed(factory)
    content = _content(0)
    content["timeline"][0].update(start_seconds=9_500, end_seconds=20_000)
    content["timeline"].append({
        "event_type": "ACTION", "start_seconds": 19_000, "end_seconds": 20_000,
        "description": "未确定归属的观察", "participants": ["未知人物"], "visible_persons": [],
    })
    with factory.begin() as session:
        _first_content(session, task_id).content = content
    snapshot = VideoParseRepository(factory).load_export_snapshot(tenant_id="mine", video_ids=[video_id])[0]
    assert snapshot.content.model_dump(mode="json") == content
    assert snapshot.content.persons == ["爱音"]


@pytest.mark.parametrize("case", ["invalid_contract", "outside_video", "invalid_dialogue_person"])
def test_export_rejects_invalid_formal_content(repository, case):
    factory = create_session_factory(repository[0])
    video_id, task_id = _seed(factory)
    with factory.begin() as session:
        stored = _first_content(session, task_id)
        content = deepcopy(stored.content)
        if case == "invalid_contract":
            content["timeline"][0]["text_variants"] = []
        elif case == "outside_video":
            content["timeline"][0]["end_seconds"] = 20_001
        else:
            content["persons"] = []
        stored.content = content
    with pytest.raises(VideoParseConflictError, match="content version 3 is invalid"):
        VideoParseRepository(factory).load_export_snapshot(tenant_id="mine", video_ids=[video_id])


@pytest.mark.parametrize("by_video", [False, True])
def test_export_multi_source_is_ordered_deduplicated_and_all_or_error(repository, by_video):
    factory = create_session_factory(repository[0])
    first_video, first_task = _seed(factory)
    second_video, second_task = _seed(factory)
    key = "video_ids" if by_video else "video_parse_task_ids"
    requested = [second_video, first_video] if by_video else [second_task, first_task]
    repo = VideoParseRepository(factory)
    snapshots = repo.load_export_snapshot(tenant_id="mine", **{key: [*requested, requested[0]]})
    assert [item.parse_task_id for item in snapshots] == [second_task, second_task, first_task, first_task]
    with pytest.raises(VideoParseTaskNotFoundError):
        repo.load_export_snapshot(tenant_id="mine", **{key: [*requested, str(uuid4())]})
    with factory.begin() as session:
        session.get(VideoParseTask, first_task).status = "PENDING"
    with pytest.raises(VideoParseConflictError):
        repo.load_export_snapshot(tenant_id="mine", **{key: requested})


@pytest.mark.parametrize("sources", [
    {}, {"video_ids": [str(uuid4())], "video_parse_task_ids": [str(uuid4())]},
    {"video_ids": ["bad-id"]}, {"video_parse_task_ids": ["bad-id"]},
])
def test_export_rejects_invalid_source_selection(repository, sources):
    with pytest.raises(ValueError):
        VideoParseRepository(create_session_factory(repository[0])).load_export_snapshot(tenant_id="mine", **sources)
