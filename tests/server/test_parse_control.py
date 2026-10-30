from datetime import timedelta
import pytest
from server.database import create_session_factory
from server.model.job import VideoParseTask, ChunkParseTask
from server.mq.video_parse_queue import MySqlVideoParseQueue
from server.service.parse_control import set_paused, is_paused
from server.service.video_parse_errors import VideoParseConflictError
from server.repository.job import ParseJobNotFoundError
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
