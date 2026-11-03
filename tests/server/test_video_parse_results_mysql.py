from __future__ import annotations

import os
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import URL

from server.caller.mysql import create_database_engine
from server.database import DatabaseEnvironment, create_schema, create_session_factory
from server.service.video_parse_persistence import VideoParsePersistenceService
from tests.server import test_video_parse_results as asr
from tests.server import test_video_parse_content_migration as migration
from tests.server import test_video_summarize_persistence as summary


@pytest.fixture
def mysql_repository():
    socket = os.environ.get('SEEKINO_ISOLATED_MYSQL_SOCKET')
    if not socket:
        pytest.skip('requires a dedicated temporary MySQL Unix socket')
    assert socket.startswith('/private/tmp/seekino-vpc-mysql-')
    database = f'vpc_{uuid4().hex}_test'
    url = URL.create('mysql+pymysql', username='root', database=database, query={'unix_socket': socket})
    import pymysql
    with pymysql.connect(user='root', unix_socket=socket, autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute(f'CREATE DATABASE `{database}` CHARACTER SET utf8mb4 COLLATE utf8mb4_bin')
    engine = create_database_engine(url.render_as_string(hide_password=False), environment=DatabaseEnvironment.TEST)
    try:
        create_schema(engine)
        yield engine, VideoParsePersistenceService(create_session_factory(engine))
    finally:
        engine.dispose()
        with pymysql.connect(user='root', unix_socket=socket, autocommit=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute(f'DROP DATABASE `{database}`')


@pytest.mark.parametrize('damage', ['checksum', 'schema', 'time_bounds', 'missing_body'])
def test_mysql_corruption_recovery(mysql_repository, damage):
    asr.test_corrupt_asr_is_rebuilt_without_erasing_summary(mysql_repository, damage)


@pytest.mark.parametrize('changed', ['media_sha256', 'schema_version', 'model_binding', 'producer'])
def test_mysql_identity_fencing(mysql_repository, changed):
    asr.test_changed_input_or_producer_cannot_reuse_or_replace_asr(mysql_repository, changed)


def test_mysql_idempotence_and_narrow_updates(mysql_repository):
    asr.test_no_completion_before_atomic_asr_commit_and_idempotent_recovery_does_not_rewrite_json(mysql_repository)


def test_mysql_migration(mysql_repository):
    migration.test_controlled_migration_dry_run_then_preserves_summary_and_task_audit(mysql_repository)


def test_mysql_migration_conflict(mysql_repository):
    migration.test_migration_preserves_existing_asr_and_rolls_back_on_conflicting_summary(mysql_repository)


def test_mysql_summary_rollback(mysql_repository, monkeypatch):
    migration.test_summary_failure_rolls_back_video_projection_and_result_together(mysql_repository, monkeypatch)


def test_mysql_summary_publication(mysql_repository):
    summary.test_publish_metadata_and_episode_order_atomically_and_preserve_runtime_siblings(mysql_repository)


def test_mysql_old_lease_cannot_publish_after_resume(mysql_repository):
    from datetime import timedelta
    from server.service.parse_control import set_paused
    from server.model.task import VideoParseTask
    from server.service.video_parse_errors import VideoParseLeaseLostError
    factory, queue, repo, task_id, lease, identity = asr._committed(mysql_repository[0])
    with factory.begin() as session:
        set_paused(session, task_id, tenant_id='mine', paused=True, expected_version=1,
                   expected_lease_version=lease.lease_version, now=asr.NOW)
        version = session.get(VideoParseTask, task_id).lease_version
        set_paused(session, task_id, tenant_id='mine', paused=False, expected_version=1,
                   expected_lease_version=version, now=asr.NOW)
    resumed = queue.claim_video('new-worker', lease_duration=timedelta(minutes=5), now=asr.NOW)
    assert repo.load_or_begin_video_asr(resumed, identity, now=asr.NOW) == asr.PAYLOAD
    with pytest.raises(VideoParseLeaseLostError):
        repo.commit_video_asr(lease, identity, asr.PAYLOAD, now=asr.NOW)
    repo.finish_video_asr_preparation(resumed, now=asr.NOW)


def test_mysql_terminal_understanding_failure_retry_keeps_asr(mysql_repository):
    from datetime import timedelta
    factory, queue, repo, task_id, lease, identity = asr._committed(mysql_repository[0])
    repo.finish_video_asr_preparation(lease, now=asr.NOW)
    asr.retry_after_understanding_failure(factory, queue, repo, task_id, lease)
    resumed = queue.claim_video('retry', lease_duration=timedelta(minutes=5), now=asr.NOW)
    assert repo.load_or_begin_video_asr(resumed, identity, now=asr.NOW) == asr.PAYLOAD
