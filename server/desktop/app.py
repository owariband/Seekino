"""Translate the native window's requests to the local library API."""
from dataclasses import asdict
from hashlib import sha256
import json
import os
from pathlib import Path

from ..constants.parse import RUNTIME_DETAIL_KEY
from ..service.mcp_evidence import StaleEvidenceError
from ..service.parse_control import ACTIVE_STATUSES, is_paused
from .host import enforce_single_process
from .runtime import open_library

MODEL_KEYS = ("ARK_API_KEY", "DEEPSEEK_API_KEY")


def _poster_id(video):
    # Keep a fallback frame distinct from a subsequently published cover or keyframe.
    version = video.cover_uri or next(iter(video.keyframe_uris), video.content_hash)
    return video.video_id + "/" + sha256(version.encode()).hexdigest()


def _video_item(library, video):
    cover = library.thumbnail(video)
    return {
        "id": video.video_id, "episode": video.episode_order,
        "title": video.display_name or video.original_filename,
        "durationMs": video.duration_seconds, "status": video.parse_status,
        "indexStatus": video.index_status, "progress": video.parse_progress,
        "cover": str(cover) if cover else None, "posterID": _poster_id(video),
    }


def _series_item(series):
    first = series.first_video
    representative = series.representative_video or first
    return {
        "id": series.series_id, "title": series.display_name or series.name,
        "count": series.video_count, "ready": series.ready_video_count, "description": series.description,
        "firstVideoID": first.video_id if first else "",
        "firstVideoTitle": (first.display_name or first.original_filename) if first else "",
        "posterID": _poster_id(representative) if representative else "",
    }


def _job_item(detail):
    state = detail.task
    paused = is_paused(state)
    runtime = state.detail.get(RUNTIME_DETAIL_KEY, {})
    can_pause = detail.is_latest and state.status in ACTIVE_STATUSES and runtime.get("accepted") is True
    can_retry = (
        detail.is_latest and not detail.has_active_execution_lease and not detail.has_active_prefix_lease
        and detail.upload_session_id is not None and state.status in {"PARSING_FAILED", "INDEXING_FAILED"}
    )
    error = state.last_error or {}
    reasons = {
        "model_timeout": "模型请求超时", "model_connection_error": "模型服务连接异常",
        "model_gateway_timeout": "模型服务网关超时", "model_service_unavailable": "模型服务暂不可用",
        "output_token_limit": "模型输出被截断", "invalid_model_response": "模型返回格式异常",
    }
    return {
        "id": state.video_parse_task_id, "series": detail.series_name, "videoID": state.video_id,
        "title": detail.original_filename, "episode": detail.episode_order, "status": state.status,
        "progress": (state.detail.get("progress") or {}).get("percent"), "elapsedMs": state.processing_duration_ms,
        "latest": detail.is_latest, "paused": paused, "canPause": can_pause, "canRetry": can_retry,
        "uploadID": detail.upload_session_id, "version": state.version, "leaseVersion": state.lease_version,
        "error": reasons.get(error.get("reason"), "发布索引失败" if state.status == "INDEXING_FAILED" else "视频解析失败")
        if error else None,
    }


class Application:
    def __init__(self, root, keys):
        self._root = Path(root)
        self._transfer_progress = None
        for name in MODEL_KEYS:
            if value := keys.get(name):
                os.environ[name] = value
        self.library = open_library(self._root)

    def call(self, action, args):
        library = self.library
        match action:
            case "close":
                library.close()
                return None
            case "seriesList":
                return [_series_item(item) for item in library.list_series(offset=args.get("offset", 0))]
            case "seriesDetail":
                state = library.series(args["id"])
                return [_video_item(library, item) for item in state.videos]
            case "createSeries":
                return _series_item(library.create_series(args["name"]))
            case "tasks":
                return [_job_item(task) for task in library.list_tasks(offset=args.get("offset", 0))]
            case "importProgress":
                return self._transfer_progress
            case "import":
                path = Path(args["path"]).resolve(strict=True)
                state = library.series(args["seriesID"])
                # Resume a prior import from the same source before reserving a new episode.
                key = sha256(os.fsencode(path.name)).hexdigest()
                size = path.stat().st_size
                pending = next((entry for entry in state.pending_uploads
                                if entry.original_filename == path.name and entry.total_size_bytes == size), None)
                order = pending.episode_order if pending else max(
                    (item.episode_order for item in (*state.videos, *state.pending_uploads)), default=0,
                ) + 1
                self._transfer_progress = None
                try:
                    result = library.import_file(
                        path, series_id=state.series_id, episode_order=order,
                        idempotency_key=f"{state.series_id}:{key}:{order}", on_progress=self._report_transfer,
                    )
                finally:
                    self._transfer_progress = None
                return {"videoID": result.video_id}
            case "pause":
                library.set_paused(args["id"], paused=args["paused"], expected_version=args["version"],
                                   expected_lease_version=args["leaseVersion"])
                return None
            case "retry":
                library.retry_parse(args["uploadID"], expected_job_version=args["version"],
                                    expected_lease_version=args["leaseVersion"])
                return None
            case "search":
                result = library.search(args["query"], series_ids=args["seriesIDs"], keywords_only=args["keywords"],
                                        top_k=30)
                videos = [_video_item(library, library.video(video_id))
                          for video_id in dict.fromkeys(hit.video_id for hit in result.hits)]
                return {"hits": [asdict(hit) for hit in result.hits], "partial": result.partial, "videos": videos}
            case "posterSource":
                video_id, _, _ = args["posterID"].partition("/")
                video = library.video(video_id)
                if _poster_id(video) != args["posterID"]:
                    raise ValueError("视频封面已更新")
                thumbnail = library.thumbnail(video)
                if thumbnail:
                    return {"image": str(thumbnail)}
                source = library.open_video(video_id)
                return {"original": str(source.path), "atMs": min(10000, source.duration_ms // 10)}
            case "openResult" | "openVideo":
                playback = (library.open_result(args["sourceRef"]) if action == "openResult"
                            else library.open_video(args["videoID"]))
                return {**asdict(playback), "path": str(playback.path)}
            case _:
                raise ValueError("未知的桌面应用操作")

    def _report_transfer(self, stored, total):
        self._transfer_progress = stored / total


_application = None


def dispatch(raw: str) -> str:
    global _application
    try:
        request = json.loads(raw)
        action, args = request["action"], request["args"]
        if action == "open":
            enforce_single_process()
            if _application is not None:
                _application.library.close()
                _application = None
            _application = Application(args["root"], args.get("keys", {}))
            result = {name: bool(os.environ.get(name)) for name in MODEL_KEYS}
        elif _application is None:
            raise RuntimeError("资料库尚未打开")
        else:
            result = _application.call(action, args)
            if action == "close":
                _application = None
        return json.dumps({"value": result}, ensure_ascii=False, allow_nan=False)
    except StaleEvidenceError:
        return json.dumps({"error": "来源版本已变化，请重新搜索。"}, ensure_ascii=False)
    except Exception as exc:
        message = str(exc) or type(exc).__name__
        for name in MODEL_KEYS:
            if secret := os.environ.get(name):
                message = message.replace(secret, "••••")
        return json.dumps({"error": message[:512]}, ensure_ascii=False)
