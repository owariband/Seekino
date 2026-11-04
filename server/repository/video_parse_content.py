"""Narrow access to video-level parse results, separate from task polling."""
from __future__ import annotations

from copy import deepcopy
from typing import Literal
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..model.dto.parseContentDTO import VideoParseContentData
from ..model.parse_content import VideoParseContent
from ..model.task import VideoParseTask


def get_metadata(session: Session, task_id: str, *, for_update: bool = False) -> VideoParseContentData | None:
    statement = select(VideoParseContent.id, VideoParseContent.video_task_id,
                       VideoParseContent.version, VideoParseContent.detail).where(
                           VideoParseContent.video_task_id == task_id)
    if for_update:
        statement = statement.with_for_update()
    row = session.execute(statement).first()
    return VideoParseContentData(id=row.id, video_task_id=row.video_task_id,
                                 version=row.version, detail=deepcopy(row.detail or {})) if row else None


def load_result(session: Session, task_id: str, field: Literal["asr", "display", "diarization"]) -> dict | None:
    if field not in {"asr", "display", "diarization"}:
        raise ValueError("unsupported video result")
    value = session.scalar(select(getattr(VideoParseContent, field)).where(
        VideoParseContent.video_task_id == task_id))
    return deepcopy(value)


def save_result(session: Session, task_id: str, *, field: Literal["asr", "display", "diarization"],
                result: dict | None, detail: dict, now_ms: int) -> None:
    """Caller locks the parent task before this row; never rewrites the sibling body."""
    if field not in {"asr", "display", "diarization"}:
        raise ValueError("unsupported video result")
    current = get_metadata(session, task_id, for_update=True)
    if current is None:
        session.add(VideoParseContent(id=str(uuid4()), video_task_id=task_id,
                                     **{field: deepcopy(result)}, detail=deepcopy(detail), version=1,
                                     create_time=now_ms, update_time=now_ms))
    else:
        session.execute(update(VideoParseContent).where(VideoParseContent.id == current.id).values(
            **{field: deepcopy(result)}, detail=deepcopy(detail), version=current.version + 1,
            update_time=now_ms))
    session.flush()


def previous_audio_metadata(session: Session, video_id: str, task_id: str):
    """Same-Video scope supplies ownership; never search another tenant's media."""
    return session.execute(select(VideoParseContent.video_task_id, VideoParseContent.detail).join(
        VideoParseTask, VideoParseTask.id == VideoParseContent.video_task_id).where(
            VideoParseTask.video_id == video_id, VideoParseTask.id != task_id,
            VideoParseTask.is_deleted.is_(False),
        ).order_by(VideoParseTask.version.desc())).all()
