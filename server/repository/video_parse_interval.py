"""SQL access and DTO mapping for Interval task and content rows."""

from __future__ import annotations

from collections.abc import Sequence
from copy import deepcopy

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..model.dto.jobDTO import IntervalParseTaskData, IntervalParseContentData
from ..model.job import IntervalParseTask, IntervalParseContent


def task_data(row: IntervalParseTask) -> IntervalParseTaskData:
    return IntervalParseTaskData(
        id=row.id,
        chunk_task_id=row.chunk_task_id,
        video_task_id=row.video_task_id,
        chunk_task_ids=deepcopy(row.chunk_task_ids),
        start_sequence_no=row.start_sequence_no,
        chunk_count=row.chunk_count,
        ready_mask=row.ready_mask,
        status=row.status,
        next_execute_time=row.next_execute_time,
        retry_count=row.retry_count,
        lease_owner=row.lease_owner,
        lease_version=row.lease_version,
        lease_expire_time=row.lease_expire_time,
        start_time=row.start_time,
        finish_time=row.finish_time,
        last_error=deepcopy(row.last_error),
        create_time=row.create_time,
        create_date=row.create_date,
        update_time=row.update_time,
        update_date=row.update_date,
    )


def content_data(row: IntervalParseContent) -> IntervalParseContentData:
    return IntervalParseContentData(
        interval_task_id=row.interval_task_id,
        content=deepcopy(row.content),
        detail=deepcopy(row.detail),
        create_time=row.create_time,
        create_date=row.create_date,
        update_time=row.update_time,
        update_date=row.update_date,
    )


def get_task(
    session: Session, task_id: str, *, video_task_id: str | None = None,
    for_update: bool = False,
) -> IntervalParseTaskData | None:
    statement = select(IntervalParseTask).where(IntervalParseTask.id == task_id)
    if video_task_id is not None:
        statement = statement.where(IntervalParseTask.video_task_id == video_task_id)
    if for_update:
        statement = statement.with_for_update()
    row = session.scalar(statement)
    return task_data(row) if row is not None else None


def get_content(session: Session, interval_task_id: str) -> IntervalParseContentData | None:
    row = session.get(IntervalParseContent, interval_task_id)
    return content_data(row) if row is not None else None


def list_tasks(
    session: Session, video_task_id: str, *, for_update: bool = False,
) -> list[IntervalParseTaskData]:
    statement = select(IntervalParseTask).where(
        IntervalParseTask.video_task_id == video_task_id,
    ).order_by(IntervalParseTask.start_sequence_no, IntervalParseTask.id)
    if for_update:
        statement = statement.with_for_update()
    return [task_data(row) for row in session.scalars(statement)]


def list_content_ids(session: Session, interval_ids: Sequence[str], *, for_update: bool = False) -> set[str]:
    statement = select(IntervalParseContent.interval_task_id).where(
        IntervalParseContent.interval_task_id.in_(interval_ids),
    )
    if for_update:
        statement = statement.with_for_update()
    return set(session.scalars(statement))


def insert_task(session: Session, data: IntervalParseTaskData) -> None:
    session.add(IntervalParseTask(
        id=data.id,
        chunk_task_id=data.chunk_task_id,
        video_task_id=data.video_task_id,
        chunk_task_ids=deepcopy(data.chunk_task_ids),
        start_sequence_no=data.start_sequence_no,
        chunk_count=data.chunk_count,
        ready_mask=data.ready_mask,
        status=data.status,
        next_execute_time=data.next_execute_time,
        retry_count=data.retry_count,
        lease_owner=data.lease_owner,
        lease_version=data.lease_version,
        lease_expire_time=data.lease_expire_time,
        start_time=data.start_time,
        finish_time=data.finish_time,
        last_error=deepcopy(data.last_error),
        create_time=data.create_time,
        update_time=data.update_time,
    ))


def save_task(session: Session, data: IntervalParseTaskData) -> None:
    row = session.get(IntervalParseTask, data.id)
    if row is None:
        raise LookupError("IntervalParseTask row no longer exists")
    row.chunk_task_id = data.chunk_task_id
    row.video_task_id = data.video_task_id
    row.chunk_task_ids = deepcopy(data.chunk_task_ids)
    row.start_sequence_no = data.start_sequence_no
    row.chunk_count = data.chunk_count
    row.ready_mask = data.ready_mask
    row.status = data.status
    row.next_execute_time = data.next_execute_time
    row.retry_count = data.retry_count
    row.lease_owner = data.lease_owner
    row.lease_version = data.lease_version
    row.lease_expire_time = data.lease_expire_time
    row.start_time = data.start_time
    row.finish_time = data.finish_time
    row.last_error = deepcopy(data.last_error)
    row.update_time = data.update_time


def insert_content(session: Session, data: IntervalParseContentData) -> None:
    session.add(IntervalParseContent(
        interval_task_id=data.interval_task_id,
        content=deepcopy(data.content),
        detail=deepcopy(data.detail),
        create_time=data.create_time,
        update_time=data.update_time,
    ))
