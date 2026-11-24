"""Native library operations backed by the same library and media services as Web."""
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

from sqlalchemy import select, text

from ..constants.tenant import DEFAULT_LOCAL_TENANT_ID
from ..model.dto.mediaDTO import ParseExportRequest, parse_export_event_types
from ..model.task import VideoParseTask
from ..model.upload import UploadSession
from ..model.video import Video
from ..repository.upload import _require_compatible_video_topology
from ..service.parse_control import is_paused
from ..storage.sqlite_index import UNBOUND_UPLOAD


def identify_unconfigured_samples(sessions):
    # Early desktop builds seeded the demonstration library with sample-<sha256>
    # and explicitly paused its initial tasks before any model had been chosen.
    with sessions.begin() as session:
        rows = session.execute(select(UploadSession, VideoParseTask).join(
            VideoParseTask, UploadSession.initial_task_id == VideoParseTask.id,
        ).where(UploadSession.idempotency_key == "sample-" + UploadSession.sha256,
                VideoParseTask.status == "PENDING", VideoParseTask.attempt_count == 0,
                VideoParseTask.lease_owner.is_(None)))
        for upload, task in rows:
            if is_paused(task) and "desktop_models_bound" not in (upload.detail or {}):
                upload.detail = {**(upload.detail or {}), "desktop_models_bound": False}


def start_with_deferred_uploads(sessions, uploads, start_consumer):
    """Assign the first chosen models, atomically, before the consumer is allowed to run."""
    with sessions.begin() as session:
        waiting = list(session.scalars(select(UploadSession).where(
            UploadSession.id.in_(text("SELECT u.id FROM upload_session u WHERE " + UNBOUND_UPLOAD)),
            UploadSession.status.in_(("UPLOADING", "COMPLETING", "COMPLETED")),
        )))
        snapshot = uploads.default_parse_snapshot() if waiting else None
        for upload in waiting:
            upload.parse_config = snapshot
            upload.detail = {**(upload.detail or {}), "desktop_models_bound": True}
            if not upload.initial_task_id:
                continue
            task = session.get(VideoParseTask, upload.initial_task_id)
            if task is None or task.status != "PENDING" or task.attempt_count or task.lease_owner:
                raise RuntimeError("待配置模型的任务状态已变化。")
            task.parse_config = snapshot
            task.chunk_count = _require_compatible_video_topology(session, session.get(Video, task.video_id), snapshot)
            task.max_attempts = snapshot["execution"]["max_retry_count"] + 1
        # SQLiteSessionFactory reserves the writer before this transaction begins.
        # The worker claims its first task after commit, and a startup error rolls
        # the binding back along with the pending model-settings replacement.
        session.flush()
        start_consumer()


def manage(services, action, args):
    lib = services.library
    tenant = DEFAULT_LOCAL_TENANT_ID
    series = lib.get_series(args["seriesID"], tenant_id=tenant)
    if action == "libraryMetadata":
        return {"name": series.display_name or "", "description": series.description or "",
                "language": series.transcript_language_hint or "", "background": series.important_extra_data or {},
                "eventTypes": list(parse_export_event_types()),
                "occupied": {row.episode_order: row.original_filename for row in series.pending_uploads},
                "videos": [{"id": v.video_id, "episode": v.episode_order, "title": v.display_name or v.original_filename}
                           for v in series.videos]}
    if action == "updateLibrary":
        lib.update_series(series.series_id, tenant_id=tenant, changes=args["changes"])
        return
    if action == "backgroundImage":
        maximum = services.media.image_upload_config.maximum_upload_size_bytes
        with Path(args["path"]).open("rb") as stream:
            image = stream.read(maximum + 1)
        if len(image) > maximum:
            raise ValueError(f"参考图不可超过 {maximum // (1024 * 1024)} MB。")
        asset = services.media.upload_series_image(series.series_id, image, tenant_id=tenant)
        return f"asset://{asset.image_id}"
    if action == "changeEpisode":
        if args["videoID"] not in {v.video_id for v in series.videos}:
            raise ValueError("视频已不在这个系列中。")
        lib.update_video_episode_order(args["videoID"], tenant_id=tenant, episode_order=args["episode"])
        return
    if action == "exportLibrary":
        request = ParseExportRequest(tenant_id=tenant, video_ids=args["videos"], event_types=args["types"])
        if not set(request.video_ids).issubset(v.video_id for v in series.videos):
            raise ValueError("所选视频已不在这个系列中。")
        dest = Path(args["destination"])
        if dest.suffix.lower() != ".jsonl":
            raise ValueError("请选择 .jsonl 文件。")
        result = services.media.export_parse_results(tenant_id=tenant, video_ids=request.video_ids,
                                                    event_types=request.event_types)
        if not result.record_count:
            raise ValueError("所选视频和事件类型没有可导出的解析记录。")
        with NamedTemporaryFile(dir=dest.parent, prefix=".seekino-export-", delete=False) as stream:
            temp = Path(stream.name)
            try:
                stream.write(result.content)
                stream.close()
                os.replace(temp, dest)
            finally:
                temp.unlink(missing_ok=True)
        return result.event_count
    raise ValueError("未知资料库操作")
