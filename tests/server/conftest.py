from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select

from server.config.parse_profile import default_video_parse_v2_config
from server.database import DatabaseEnvironment, create_database_engine, create_schema, create_session_factory
from server.model.job import ChunkParseContent, ChunkParseTask, VideoParseTask
from server.model.video import Series, Video, VideoChunk
from server.service.video_parse_persistence import INDEX_EMBEDDINGS_DETAIL_KEY, VideoParsePersistenceService
from server.utils.time import datetime_to_unix_time_ms


def set_series_parse_status(session, series_id, status="COMPLETE"):
    """Set current parse facts for Chat tests that stub retrieval itself."""
    tasks = session.scalars(select(VideoParseTask).join(Video).where(Video.series_id == series_id)).all()
    if not tasks:
        video = Video(
            id=str(uuid4()), series_id=series_id, episode_order=100, upload_sequence=100,
            filename="scope.mp4", source_uri="scope.mp4", hash=uuid4().hex * 2, size_bytes=1,
            duration_ms=1000, upload_status="COMPLETED", parse_status=status,
            upload_progress=100, parse_progress=100,
        )
        session.add(video)
        session.flush()
        task = VideoParseTask(
            id=str(uuid4()), video_id=video.id, pipeline_version="video-parse-v2",
            status=status, parse_config=default_video_parse_v2_config().model_dump(mode="json"),
            chunk_count=1, version=1, max_attempts=3, is_test=True,
        )
        session.add(task)
        tasks = [task]
    for task in tasks:
        task.status = status


@pytest.fixture
def repository():
    engine = create_database_engine("sqlite+pysqlite:///:memory:", environment=DatabaseEnvironment.TEST)
    create_schema(engine)
    yield engine, VideoParsePersistenceService(create_session_factory(engine))
    engine.dispose()


@pytest.fixture
def seed_video(tmp_path):
    """Seed current V2 facts for tests of the library, media and Chat boundaries."""
    def seed(factory, *, tenant_id="local-default", series_id=None, series_name="Test Series", episode_order=7, upload_sequence=None):
        video_id, task_id = str(uuid4()), str(uuid4())
        source = tmp_path / f"{video_id}.mp4"
        source.write_bytes(b"source-video")
        at = datetime(2026, 9, 29, tzinfo=UTC)
        timestamp = datetime_to_unix_time_ms(at)
        config = default_video_parse_v2_config().model_dump(mode="json")
        config.update(chunk_duration=10, interval_duration=30)
        config["indexing"]["index_generation"] = 1
        chunks = []
        with factory.begin() as session:
            if series_id is None:
                series_id = session.scalar(select(Series.id).where(
                    Series.tenant_id == tenant_id, Series.name == series_name,
                )) or str(uuid4())
            if session.get(Series, series_id) is None:
                session.add(Series(id=series_id, tenant_id=tenant_id, name=series_name,
                                   description="isolated fixture", status="INDEXING", character_bible=[]))
            session.add(Video(id=video_id, series_id=series_id, episode_order=episode_order,
                              upload_sequence=upload_sequence or episode_order, filename="episode.mp4",
                              source_uri=source.as_uri(), hash="a" * 64, size_bytes=source.stat().st_size,
                              duration_ms=20_000, upload_status="COMPLETED", parse_status="COMPLETE",
                              upload_progress=100, parse_progress=100))
            session.add(VideoParseTask(id=task_id, video_id=video_id, pipeline_version="video-parse-v2",
                                      status="COMPLETE", parse_config=config, chunk_count=2, version=1,
                                      next_execute_time=at, finish_time=at, max_attempts=3,
                                      is_test=True, is_deleted=False,
                                      detail={"_worker_runtime_v2": {"accepted": True}},
                                      create_time=timestamp, update_time=timestamp))
            for index in range(2):
                chunk_id, child_id = str(uuid4()), str(uuid4())
                start, end = index * 10_000, (index + 1) * 10_000
                path = tmp_path / f"{chunk_id}.mp4"
                path.write_bytes(f"canonical-{index}".encode())
                session.add(VideoChunk(id=chunk_id, video_id=video_id, sequence_no=index,
                                       start_ms=start, end_ms=end, window_start_ms=start, window_end_ms=end,
                                       chunk_media_ref=path.as_uri(), analysis_window_media_ref=path.as_uri(),
                                       transcript=[], keyframe_uris=[]))
                session.add(ChunkParseTask(id=child_id, video_task_id=task_id, chunk_id=chunk_id,
                                          phase=3, status="COMPLETE", indexed_content_version=1,
                                          next_execute_time=at, detail=None,
                                          create_time=timestamp, update_time=timestamp))
                receipt = {"parse_task_id": task_id, "video_chunk_id": chunk_id,
                           "content_version": 1, "index_generation": 1}
                session.add(ChunkParseContent(id=str(uuid4()), chunk_task_id=child_id, version=1,
                    content={"persons": [], "timeline": [{"event_type": "ACTION", "start_seconds": start + 1000,
                              "end_seconds": start + 3000, "description": f"第 {index + 1} 段动作",
                              "participants": [], "visible_persons": []}]},
                    detail={"compact": f"第 {index + 1} 段摘要", INDEX_EMBEDDINGS_DETAIL_KEY: {
                        "content_version": 1, "representations": {}, "receipt": receipt}},
                    create_time=timestamp, update_time=timestamp))
                chunks.append(SimpleNamespace(video_chunk_id=chunk_id, chunk_media_ref=path.as_uri(),
                                              analysis_window_media_ref=path.as_uri()))
        return SimpleNamespace(tenant_id=tenant_id, series_id=series_id, series_name=series_name,
                               video_id=video_id, video_parse_task_id=task_id,
                               video_chunk_ids=[chunk.video_chunk_id for chunk in chunks], chunks=tuple(chunks),
                               episode_order=episode_order, upload_sequence=upload_sequence or episode_order,
                               original_filename="episode.mp4", original_source_uri=source.as_uri(),
                               total_bytes=source.stat().st_size)
    return seed


@pytest.fixture
def parsed_video(repository, seed_video):
    return seed_video(create_session_factory(repository[0]))
