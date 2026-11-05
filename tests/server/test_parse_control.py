from datetime import timedelta
from uuid import uuid4
import pytest
from server.database import create_session_factory
from server.model.task import VideoParseTask, ChunkParseTask
from server.model.video import Series, Video, VideoChunk
from server.model.upload import UploadSession, UploadSessionStatus
from server.model.parse_usage import ParseModelCall
from server.config.parse_profile import default_video_parse_v2_config
from server.repository.job import ParseJobRepository, ParseJobNotFoundError
from server.repository.library import LibraryRepository
from server.service.job import ParseJobService
from server.service.library import LibraryService
from server.mq.video_parse_queue import MySqlVideoParseQueue
from server.service.parse_control import set_paused, is_paused
from server.service.video_parse_errors import VideoParseConflictError
from server.utils.time import datetime_to_unix_time_ms
from tests.server.test_video_parse_queue import _task, _prepare, NOW


def test_pause_fences_active_work_frees_capacity_and_resumes_same_attempt(repository):
    factory = create_session_factory(repository[0])
    task_id = _task(factory, chunk_count=2, accepted=True)
    queue = MySqlVideoParseQueue(factory, video_concurrency=1)
    _, video_lease, _ = _prepare(factory, queue, task_id, chunk_count=2)
    child_lease = queue.claim_chunk('chunk', lease_duration=timedelta(minutes=1), now=NOW)
    assert child_lease is not None
    with factory.begin() as session:
        set_paused(session, task_id, tenant_id='mine', paused=True, expected_version=1,
                   expected_lease_version=video_lease.lease_version, now=NOW)
    assert not queue.heartbeat_video(video_lease, lease_duration=timedelta(minutes=1), now=NOW)
    assert not queue.heartbeat_chunk(child_lease, lease_duration=timedelta(minutes=1), now=NOW)
    assert queue.claim_video('worker', lease_duration=timedelta(minutes=1), now=NOW) is None
    assert queue.claim_chunk('worker', lease_duration=timedelta(minutes=1), now=NOW) is None
    with factory() as session:
        parent = session.get(VideoParseTask, task_id)
        assert is_paused(parent) and not parent.capacity_acquired
        assert parent.detail['_worker_runtime_v2']['accepted'] is True
        assert parent.version == 1
        lease_version = parent.lease_version
        child = session.get(ChunkParseTask, child_lease.chunk_parse_task_id)
        assert child.status == 'QUEUED' and child.retry_count == 0
    other = _task(factory, chunk_count=1, accepted=True)
    other_lease = queue.claim_video('other', lease_duration=timedelta(minutes=1), now=NOW)
    assert other_lease.video_parse_task_id == other
    with factory.begin() as session:
        set_paused(session, other, tenant_id='mine', paused=True, expected_version=1,
                   expected_lease_version=other_lease.lease_version, now=NOW)
        set_paused(session, task_id, tenant_id='mine', paused=False, expected_version=1,
                   expected_lease_version=lease_version, now=NOW)
    resumed = queue.claim_video('worker', lease_duration=timedelta(minutes=1), now=NOW)
    assert resumed.video_parse_task_id == task_id
    assert resumed.lease_version > video_lease.lease_version


@pytest.mark.parametrize('tenant,version,error', [('other',1,ParseJobNotFoundError), ('mine',2,VideoParseConflictError)])
def test_pause_scope_and_version(repository,tenant,version,error):
    factory=create_session_factory(repository[0])
    task_id=_task(factory,chunk_count=1,accepted=True)
    with pytest.raises(error), factory.begin() as session:
        set_paused(session,task_id,tenant_id=tenant,paused=True,expected_version=version,
                   expected_lease_version=0,now=NOW)


def test_pause_blocks_realtime_and_due_claims_even_without_hold_time(repository):
    factory=create_session_factory(repository[0])
    task_id=_task(factory,chunk_count=2,accepted=True)
    queue=MySqlVideoParseQueue(factory,video_concurrency=1)
    _,lease,prepared=_prepare(factory,queue,task_id,chunk_count=2)
    with factory.begin() as s:
        set_paused(s,task_id,tenant_id='mine',paused=True,expected_version=1,
                   expected_lease_version=lease.lease_version,now=NOW)
        p=s.get(VideoParseTask,task_id)
        p.next_execute_time=NOW
        p.capacity_acquired=True  # A stale capacity flag must not bypass user intent.
    assert queue.claim_video('x',lease_duration=timedelta(minutes=1),now=NOW) is None
    assert queue.claim_chunk('x',lease_duration=timedelta(minutes=1),now=NOW) is None
    with factory() as s:
        from sqlalchemy import select
        child=s.scalar(select(ChunkParseTask).where(ChunkParseTask.video_task_id==task_id))
        child_id=child.id
    with factory.begin() as s:
        s.get(ChunkParseTask,child_id).next_execute_time=NOW
    assert queue.claim_realtime_chunk(child_id,'x',lease_duration=timedelta(minutes=1),now=NOW) is None


def test_pause_projection_and_asr_stage(repository):
    from server.api.jobs import project_video_parse_task
    from server.repository.job import ParseJobRepository
    factory=create_session_factory(repository[0])
    task_id=_task(factory,chunk_count=2,accepted=True)
    queue=MySqlVideoParseQueue(factory,video_concurrency=1)
    lease=queue.claim_video('x',lease_duration=timedelta(minutes=1),now=NOW)
    with factory.begin() as s:
        p=s.get(VideoParseTask,task_id)
        p.detail={**p.detail,'_worker_runtime_v2':{**p.detail['_worker_runtime_v2'],
            'execution_timing':{'entries':{f'asr:{task_id}:{lease.lease_version}':{'outcome':'running'}}}}}
    repo=ParseJobRepository(factory)
    view=project_video_parse_task(repo.get_video_parse_task_detail(task_id,tenant_id='mine'))
    assert view.active_stage=='asr' and view.can_pause and not view.can_resume
    with factory.begin() as s:
        set_paused(s,task_id,tenant_id='mine',paused=True,expected_version=1,
                   expected_lease_version=lease.lease_version,now=NOW)
    view=project_video_parse_task(repo.get_video_parse_task_detail(task_id,tenant_id='mine'))
    assert view.is_paused and view.can_resume and not view.can_pause
    assert view.active_stage is None and not view.can_reparse


def test_old_realtime_consumer_cannot_bypass_pause(repository, monkeypatch):
    factory=create_session_factory(repository[0])
    task_id=_task(factory,chunk_count=2,accepted=True)
    queue=MySqlVideoParseQueue(factory,video_concurrency=1)
    _,lease,_=_prepare(factory,queue,task_id,chunk_count=2)
    with factory.begin() as s:
        set_paused(s,task_id,tenant_id='mine',paused=True,expected_version=1,
                   expected_lease_version=lease.lease_version,now=NOW)
    from sqlalchemy import select
    with factory() as s:
        child_id=s.scalar(select(ChunkParseTask.id).where(ChunkParseTask.video_task_id==task_id))
    monkeypatch.setattr('server.mq.video_parse_queue.is_paused', lambda _: False)
    assert queue.claim_realtime_chunk(child_id,'old',lease_duration=timedelta(minutes=1),now=NOW) is None
    with factory() as s:
        assert not s.get(VideoParseTask,task_id).capacity_acquired


class _FakeIndexStore:
    def __init__(self):
        self.deleted = []
        self.documents = set()

    def bulk_delete(self, keys, *, refresh=False):
        self.deleted.append((tuple(keys), refresh))
        self.documents.difference_update(keys)

    def verify_absent(self, keys):
        return not self.documents.intersection(keys)


def _versioned_parse(repository, *, statuses=None):
    """Seed a Video with several parse versions, chunks and usage, plus a service."""
    factory = create_session_factory(repository[0])
    series_id, video_id, upload_id = str(uuid4()), str(uuid4()), str(uuid4())
    chunk_id = str(uuid4())
    config = default_video_parse_v2_config().model_dump(mode="json")
    version_count = len(statuses) if statuses else 2
    statuses = statuses or ["COMPLETE"] * version_count
    task_ids, usage_ids = [], []
    now_ms = datetime_to_unix_time_ms(NOW)
    with factory.begin() as session:
        session.add(Series(id=series_id, tenant_id="mine", name=f"Series {series_id}",
                           description=None, status="INDEXING", character_bible=[]))
        session.flush()
        session.add(Video(id=video_id, series_id=series_id, episode_order=1,
                          upload_sequence=1, filename="episode.mp4",
                          source_uri="artifact://test/episode.mp4", hash="a" * 64,
                          size_bytes=10, duration_ms=20_000, upload_status="COMPLETED",
                          parse_status="COMPLETE", upload_progress=100, parse_progress=100))
        session.flush()
        session.add(VideoChunk(id=chunk_id, video_id=video_id, sequence_no=0, start_ms=0,
                               end_ms=20_000, window_start_ms=0, window_end_ms=20_000,
                               chunk_media_ref="artifact://test/chunk.mp4",
                               analysis_window_media_ref="artifact://test/window.mp4"))
        session.add(UploadSession(id=upload_id, tenant_id="mine", series_id=series_id,
                                  episode_order=1, upload_sequence=1, filename="episode.mp4",
                                  media_type="video/mp4", size_bytes=10, segment_size_bytes=10,
                                  segment_count=1, version=1, status=UploadSessionStatus.COMPLETED.value,
                                  idempotency_key="versioned-parse", expire_time=NOW, finish_time=NOW,
                                  video_id=video_id, create_time=now_ms, update_time=now_ms))
        for version, status in enumerate(statuses, start=1):
            task_id = str(uuid4())
            usage_id = str(uuid4())
            session.add(VideoParseTask(id=task_id, video_id=video_id, pipeline_version="v2",
                                       status=status, parse_config=config, chunk_count=1,
                                       version=version, next_execute_time=NOW, attempt_count=1,
                                       max_attempts=3, lease_version=0, processing_duration_ms=0,
                                       processing_timing_complete=True, is_test=False, is_deleted=False,
                                       detail={"_worker_runtime_v2": {"accepted": True}},
                                       create_time=now_ms, update_time=now_ms))
            session.add(ChunkParseTask(id=str(uuid4()), video_task_id=task_id, chunk_id=chunk_id,
                                       phase=3, indexed_content_version=1, status="COMPLETE",
                                       next_execute_time=NOW, retry_count=0, lease_version=0,
                                       create_time=now_ms, update_time=now_ms))
            session.add(ParseModelCall(id=usage_id, video_task_id=task_id, execution_kind="chunk",
                                       execution_task_id=str(uuid4()), attempt_count=1, lease_version=1,
                                       tenant_id="mine", series_id=series_id, video_id=video_id,
                                       provider="ark", model="model", category="parse",
                                       operation="understand", start_time=NOW, status="completed"))
            task_ids.append(task_id)
            usage_ids.append(usage_id)
    repo = ParseJobRepository(factory)
    service = ParseJobService(
        repo,
        LibraryService(LibraryRepository(factory, execution_mode="v2")),
        execution_mode="v2",
    )
    return factory, repo, service, video_id, task_ids, chunk_id, usage_ids


def test_delete_single_historical_version_cleans_only_its_es_keys(repository):
    factory, repo, service, _video_id, task_ids, chunk_id, usage_ids = _versioned_parse(repository)
    v1_id, v2_id = task_ids
    index = _FakeIndexStore()
    service._video_parse_index_store = index
    result = service.delete_video_parse_task(
        tenant_id="mine", video_parse_task_id=v1_id, expected_job_version=1,
    )
    assert result == [v1_id]
    assert index.deleted == [(((v1_id, chunk_id),), "wait_for")]
    with factory() as session:
        v1 = session.get(VideoParseTask, v1_id)
        assert v1.is_deleted and not v1.detail["_worker_runtime_v2"]["accepted"]
        assert v1.lease_version == 1
        assert not session.get(VideoParseTask, v2_id).is_deleted
        assert session.get(ParseModelCall, usage_ids[0]) is not None
    listed = [item.task.video_parse_task_id
              for item in repo.list_video_parse_tasks_for_workspace("mine")]
    assert v2_id in listed and v1_id not in listed
    assert repo.count_video_parse_tasks_for_workspace("mine") == 1


def test_delete_history_scope_deletes_all_and_only_nonlatest_versions(repository):
    factory, repo, service, video_id, task_ids, _chunk_id, _usages = _versioned_parse(
        repository, statuses=["COMPLETE", "PARSING", "COMPLETE"],
    )
    service._video_parse_index_store = _FakeIndexStore()
    latest_id = task_ids[-1]
    result = service.delete_video_parse_task(tenant_id="mine", video_id=video_id)
    assert sorted(result) == sorted(task_ids[:-1])
    with factory() as session:
        for task_id in task_ids[:-1]:
            assert session.get(VideoParseTask, task_id).is_deleted
        assert not session.get(VideoParseTask, latest_id).is_deleted


def test_delete_latest_version_returns_conflict(repository):
    _factory, _repo, service, _video_id, task_ids, _chunk_id, _usages = _versioned_parse(repository)
    latest_id = task_ids[-1]
    with pytest.raises(VideoParseConflictError, match="当前版本"):
        service.delete_video_parse_task(
            tenant_id="mine", video_parse_task_id=latest_id, expected_job_version=2,
        )


def test_delete_version_requires_tenant_and_expected_version(repository):
    _factory, _repo, service, _video_id, task_ids, _chunk_id, _usages = _versioned_parse(repository)
    v1_id = task_ids[0]
    with pytest.raises(ParseJobNotFoundError):
        service.delete_video_parse_task(
            tenant_id="other", video_parse_task_id=v1_id, expected_job_version=1,
        )
    with pytest.raises(VideoParseConflictError, match="刷新"):
        service.delete_video_parse_task(
            tenant_id="mine", video_parse_task_id=v1_id, expected_job_version=99,
        )


def test_project_flags_for_running_latest_and_superseded_history(repository):
    from server.api.jobs import project_video_parse_task
    factory, repo, _service, _video_id, task_ids, _chunk_id, _usages = _versioned_parse(
        repository, statuses=["PARSING", "PARSING"],
    )
    v1_id, v2_id = task_ids
    with factory.begin() as session:
        latest = session.get(VideoParseTask, v2_id)
        latest.lease_owner = "worker"
        latest.lease_expire_time = NOW
        latest.heartbeat_time = NOW
    latest_view = project_video_parse_task(repo.get_video_parse_task_detail(v2_id, tenant_id="mine"))
    assert latest_view.can_delete is True
    assert latest_view.can_delete_version is False
    assert latest_view.is_superseded is False
    history_view = project_video_parse_task(repo.get_video_parse_task_detail(v1_id, tenant_id="mine"))
    assert history_view.can_delete is False
    assert history_view.can_delete_version is True
    assert history_view.is_superseded is True
