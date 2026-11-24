"""Translate the native window's requests to the local library API."""
from contextlib import contextmanager
from dataclasses import asdict
from hashlib import sha256
import json
import os
from pathlib import Path
from threading import Condition
from uuid import uuid4

from engine.rag.retrieval.contracts import EventIndexNotReadyError, SeriesNotReadyError

from ..constants.parse import RUNTIME_DETAIL_KEY
from ..service.mcp_evidence import StaleEvidenceError
from ..service.parse_control import ACTIVE_STATUSES, is_paused
from .host import enforce_single_process
from .runtime import open_library
from .model_catalogue import discover as discover_models
from .model_settings import ModelProfile, LEGACY_KEYS
from .usage import overview as usage_overview
from .watch_settings import prepare_watch, save_watch, watch_credentials, watch_view

MODEL_KEYS = LEGACY_KEYS


def _poster_id(video):
    # Keep a fallback frame distinct from a subsequently published cover or keyframe.
    version = video.cover_uri or next(iter(video.keyframe_uris), video.content_hash)
    return video.video_id + "/" + sha256(version.encode()).hexdigest()


def _video_item(library, video):
    cover = library.thumbnail(video)
    return {
        "id": video.video_id, "episode": video.episode_order, "seriesID": video.series_id,
        "filename": video.original_filename,
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


def _job_failure(error, status):
    """Only publish known diagnostics. Worker messages can contain filenames, keys or request bodies."""
    reason = error.get("reason")
    stage = {"understanding": "视频理解", "asr": "语音转写", "ocr": "画面文字识别",
             "visual": "画面准备", "commit": "结果保存"}.get(error.get("stage"), "任务执行")
    message = str(error.get("message") or "").lower()
    if (message.startswith("model source requires api key environment variable '")
            or message.startswith("model api key environment variable '") and message.endswith("' is not configured")):
        return "缺少模型凭据。请在设置中补齐后重试。", "settings"
    if message.startswith(("error code: 401", "http 401 unauthorized", "401 unauthorized")):
        return "服务拒绝了当前凭据。请核对密钥后重试。", "settings"
    if "invalid data found when processing input" in message or message == "source contains no video stream":
        return "媒体无法解码。请检查原片能否播放。", "source"
    if message.startswith("[errno 2] no such file or directory"):
        return "处理所需的文件未找到。请先检查原片。", "source"
    if reason == "timeout":
        return f"{stage}超时。请稍后重试。", ""
    reasons = {
        "model_timeout": "模型请求超时。请稍后重试。",
        "model_connection_error": "连接模型服务失败。请检查网络后重试。",
        "model_network_error": "模型网络请求失败。请检查网络后重试。",
        "model_gateway_timeout": "模型服务网关超时。请稍后重试。",
        "model_service_unavailable": "模型服务暂不可用。请稍后重试。",
        "output_token_limit": "模型输出被截断。请重新运行解析。",
        "invalid_model_response": "模型返回的结果格式异常。请重试。",
        "loop_limit": "已达到模型调用次数上限。请重试。",
        "required_tool_missing": "模型未提交所需结果。请重试。",
    }
    if reason in reasons:
        return reasons[reason], ""
    if status == "INDEXING_FAILED":
        return "检索索引未能发布，尚无具体原因。请重试。", ""
    if error.get("stage") in {"asr", "visual", "ocr", "understanding", "commit"}:
        return f"{stage}未完成，尚无具体原因。可重试此任务。", ""
    return "未获得具体失败原因。请确认原片可播放后重试。", "source"


def _job_item(detail):
    state = detail.task
    paused = is_paused(state)
    runtime = state.detail.get(RUNTIME_DETAIL_KEY, {})
    can_pause = detail.is_latest and state.status in ACTIVE_STATUSES and runtime.get("accepted") is True
    can_retry = (
        detail.is_latest and not detail.has_active_execution_lease and not detail.has_active_prefix_lease
        and detail.upload_session_id is not None and state.status in {"PARSING_FAILED", "INDEXING_FAILED"}
    )
    terminal = state.status in {"COMPLETE", "PARSING_FAILED", "INDEXING_FAILED"}
    pending_takeover = state.status == "PENDING" and runtime.get("accepted") is not True
    can_reparse = bool(detail.is_latest and detail.upload_session_id and not paused
                       and not detail.has_active_prefix_suffix_lease
                       and (terminal or (detail.pending_takeover_enabled if pending_takeover
                                         else not detail.has_active_execution_lease)))
    error = state.last_error or {}
    failed = state.status in {"PARSING_FAILED", "INDEXING_FAILED"}
    failure, action = _job_failure(error, state.status) if error or failed else (None, "")
    if error and not failed:
        failure = failure.partition("。")[0]
        action = ""
    if failed and not can_retry:
        failure = failure.partition("。")[0] + ("。当前暂不可重试。" if detail.is_latest else "。这是历史版本。")
        if not detail.is_latest:
            action = ""
    return {
        "id": state.video_parse_task_id, "series": detail.series_name, "videoID": state.video_id,
        "title": detail.original_filename, "episode": detail.episode_order, "status": state.status,
        "progress": (state.detail.get("progress") or {}).get("percent"), "elapsedMs": state.processing_duration_ms,
        "latest": detail.is_latest, "paused": paused, "canPause": can_pause, "canRetry": can_retry,
        "canReparse": can_reparse, "canDelete": bool(detail.is_latest and detail.upload_session_id),
        "canDeleteVersion": not detail.is_latest,
        "uploadID": detail.upload_session_id, "version": state.version, "leaseVersion": state.lease_version,
        "error": failure, "errorAction": action,
    }


class Application:
    def __init__(self, root, keys):
        self._root = Path(root)
        self._preview_scope = sha256(os.fsencode(self._root.resolve())).hexdigest()
        self._transfer_progress = None
        self.profile = ModelProfile.read(self._root)
        self.keys = self.profile.values(keys)
        slot = watch_view(self._root, self.profile, self.keys)["watchCredentialName"]
        if slot and keys.get(slot):
            self.keys[slot] = keys[slot]
        self.library = self._open(self.profile, self.keys)

    def _open(self, profile, keys, *, start=True):
        config, environ = profile.for_gateway(keys)
        # Existing failed tasks can be resumed with just their frozen model credentials.
        process = any(profile.ready_sources(keys).values())
        unconfigured = profile.view(keys)["mode"] == "new"
        library = open_library(self._root, gateway_config=config, gateway_environ=environ,
                               start_consumer=start and process and not unconfigured)
        if unconfigured:
            try:
                library.identify_unconfigured_samples()
            except Exception:
                library.close()
                raise
        return library

    def save_models(self, args):
        current = ModelProfile.read(self._root)
        self.profile.check_revision(current.revision)
        candidate = current.prepare(args)
        keys = candidate.values({**self.keys, **args.get("keys", {})})
        if candidate.form and not candidate.ready_sources(keys).get(candidate.source):
            raise ValueError("请为此服务填写 API Key。")
        changed = current.raw != candidate.raw
        # SQLite owns an exclusive file lock. Drain readers and workers before reopening.
        self.library.close()
        replacement = None
        try:
            replacement = self._open(candidate, keys, start=False)
            ModelProfile.read(self._root).check_revision(current.revision)
            if changed:
                candidate.persist()
            if candidate.view(keys)["mode"] != "new" and any(candidate.ready_sources(keys).values()):
                replacement.start_processing()
        except Exception:
            if replacement is not None:
                replacement.close()
            # Restore the profile if persistence succeeded but starting the consumer failed.
            if changed and candidate.path.exists() and candidate.path.read_bytes() == candidate.raw:
                if current.raw is None:
                    candidate.path.unlink()
                else:
                    current.persist()
            self.library = self._open(self.profile, self.keys)
            raise
        slot = watch_view(self._root, candidate, self.keys)["watchCredentialName"]
        if slot and self.keys.get(slot):
            keys[slot] = self.keys[slot]
        self.library, self.profile, self.keys = replacement, candidate, keys
        return candidate.view(keys) | watch_view(self._root, candidate, keys)

    def call(self, action, args):
        library = self.library
        match action:
            case "episodeTimeline":
                return library.episode_timeline(args["videoID"])
            case "episodeInterpret":
                from server.service.video_find_query import interpret_with_binding
                from .watch_settings import watch_connection
                connection = watch_connection(self._root, self.profile, self.keys)
                if connection is None:
                    raise ValueError("请先配置 Kino 问答使用的模型。")
                config, environ = connection
                binding = config.resolve_model_binding("vision", environ=environ)
                return interpret_with_binding(args["query"], config, binding, environ=environ,
                                              allow_local=True).model_dump()
            case "episodeRelated":
                return library.episode_related(args["videoID"], args["query"])
            case "askWatch" | "askLibrary":
                from .watch_conversation import answer_turn
                return answer_turn(self, action, args)
            case "watchHistory":
                from .watch_history import LIBRARY_CHAT
                video, thread = args["videoID"], args.get("threadID")
                if video != LIBRARY_CHAT:
                    library.open_video(video)
                if args.get("delete"):
                    return library.watch_history.delete(video, thread)
                return library.watch_history.view(video, thread)
            case "close":
                library.close()
                return None
            case "seriesList":
                return [_series_item(item) for item in library.list_series(offset=args.get("offset", 0))]
            case "seriesDetail":
                state = library.series(args["id"])
                return [_video_item(library, item) for item in state.videos]
            case "libraryMetadata" | "updateLibrary" | "changeEpisode" | "exportLibrary" | "backgroundImage":
                return library.manage(action, args)
            case "createSeries":
                return _series_item(library.create_series(args["name"]))
            case "tasks":
                available = self.profile.ready_sources(self.keys)
                return [{**_job_item(task), "retryReady": self.profile.retry_ready(task.task, available)}
                        for task in library.list_tasks(offset=args.get("offset", 0))]
            case "taskDetail" | "taskUsage":
                detail = library.task(args["id"])
                if action == "taskUsage":
                    from .usage import overview
                    return overview(library, detail.task.video_parse_task_id)
                from ..utils.time import datetime_to_unix_time_ms
                task = detail.task
                return {**_job_item(detail), "chunks": task.chunk_count, "facts": task.detail.get("progress") or {},
                        "timing": task.detail.get("timing") or {}, "updatedAt": datetime_to_unix_time_ms(task.update_time)}
            case "usage":
                return usage_overview(library)
            case "importProgress":
                return self._transfer_progress
            case "import":
                return self.import_local(args)
            case "pause":
                library.set_paused(args["id"], paused=args["paused"], expected_version=args["version"],
                                   expected_lease_version=args["leaseVersion"])
                return None
            case "retry":
                library.retry_parse(args["uploadID"], expected_job_version=args["version"],
                                    expected_lease_version=args["leaseVersion"])
                return None
            case "reparse" | "deleteImport" | "deleteVersion" | "deleteHistory":
                detail = library.task(args["id"])
                row = _job_item(detail)
                if row["version"] != args["version"]:
                    raise ValueError("任务版本已变化，请刷新后重试。")
                if action == "reparse" and row["canReparse"]:
                    if not self.profile.view(self.keys)["importReady"]:
                        raise ValueError("请先配置解析模型。")
                    library.reparse(row["uploadID"], expected_job_version=row["version"])
                elif action == "deleteImport" and row["canDelete"]:
                    library.delete_import(row["uploadID"])
                elif action == "deleteVersion" and row["canDeleteVersion"]:
                    library.delete_parse_history(task_id=row["id"], version=row["version"])
                elif action == "deleteHistory" and row["latest"]:
                    library.delete_parse_history(video_id=row["videoID"])
                else:
                    raise ValueError("这项操作不适用于当前任务状态，请刷新后检查。")
                return None
            case "search":
                if not args["keywords"] and not self.profile.view(self.keys)["searchReady"]:
                    raise ValueError("请先配置用于描述搜索的向量模型。")
                try:
                    result = library.search(args["query"], series_ids=args["seriesIDs"], keywords_only=args["keywords"],
                                            top_k=30)
                except (EventIndexNotReadyError, SeriesNotReadyError):
                    return {"hits": [], "partial": True, "indexUnavailable": True, "videos": [], "previewScope": self._preview_scope}
                videos = [_video_item(library, library.video(video_id))
                          for video_id in dict.fromkeys(hit.video_id for hit in result.hits)]
                return {"hits": [asdict(hit) for hit in result.hits], "partial": result.partial, "videos": videos,
                        "previewScope": self._preview_scope}
            case "hitPreview":
                if args["scope"] != self._preview_scope:
                    raise ValueError("搜索预览不属于当前资料库")
                source = library.open_result(args["sourceRef"])
                return {"original": str(source.path), "atMs": source.start_ms}
            case "posterSource":
                video_id, _, _ = args["posterID"].partition("/")
                video = library.video(video_id)
                if _poster_id(video) != args["posterID"]:
                    raise ValueError("视频封面已更新")
                surface = args.get("surface", "card")
                at = args.get("atMs", -1)
                if surface not in {"card", "hero", "shelf"} or type(at) is not int or at < -1:
                    raise ValueError("无效的视频预览")
                if at == -1:
                    thumbnail = library.thumbnail(video)
                    if thumbnail:
                        return {"image": str(thumbnail)}
                source = library.open_video(video_id)
                if at == -1:
                    if surface == "shelf":
                        at = round(source.duration_ms * .35)
                    else:
                        at = min(30000 if surface == "hero" else 10000, source.duration_ms // 10)
                return {"original": str(source.path), "atMs": max(0, min(at, source.duration_ms - 1))}
            case "openResult" | "openVideo":
                playback = (library.open_result(args["sourceRef"]) if action == "openResult"
                            else library.open_video(args["videoID"]))
                return {**asdict(playback), "path": str(playback.path), "watch": library.watch_history.view(playback.video_id)}
            case _:
                raise ValueError("未知的桌面应用操作")

    def import_local(self, args, on_progress=None):
        library = self.library
        from server.storage.sqlite_index import DEFERRED_UPLOAD_PREFIX
        model_view = self.profile.view(self.keys)
        if model_view["mode"] != "new" and not model_view["importReady"]:
            raise ValueError("已配置的解析模型缺少凭据，请先在设置中补齐。")
        path = Path(args["path"]).resolve(strict=True)
        state = library.series(args["seriesID"])
        # Resume a prior import from the same source before reserving a new episode.
        size = path.stat().st_size
        requested = args.get("episode")
        if requested is not None and (type(requested) is not int or not 0 < requested <= 2147483647):
            raise ValueError("集号必须是正整数。")
        pending = next((entry for entry in state.pending_uploads
                        if entry.original_filename == path.name and entry.total_size_bytes == size
                        and requested in (None, entry.episode_order)), None)
        order = requested or (pending.episode_order if pending else max(
            (item.episode_order for item in (*state.videos, *state.pending_uploads)), default=0,
        ) + 1)
        # Expired or cancelled transfers keep their idempotency records. Only
        # a resumable session should reuse a key.
        token = str(uuid4())
        if pending:
            token = library.transfer_token(pending.upload_session_id)
        elif model_view["mode"] == "new":
            token = DEFERRED_UPLOAD_PREFIX + token
        self._transfer_progress = None
        try:
            result = library.import_file(
                path, series_id=state.series_id, episode_order=order,
                idempotency_key=token, on_progress=on_progress or self._report_transfer,
            )
        except InterruptedError:
            # The transfer can only be interrupted before complete() publishes the video.
            for entry in library.series(state.series_id).pending_uploads:
                if entry.status == "UPLOADING" and library.transfer_token(entry.upload_session_id) == token:
                    library.delete_import(entry.upload_session_id)
                    break
            raise
        finally:
            self._transfer_progress = None
        return {"videoID": result.video_id}

    def _report_transfer(self, stored, total):
        self._transfer_progress = stored / total


_application = None
_access = Condition()
_readers = 0
_writer = False
_waiting_writers = 0


@contextmanager
def _using_library(exclusive):
    # Polling the progress of a long import is a concurrent reader.
    global _readers, _writer, _waiting_writers
    with _access:
        if exclusive:
            _waiting_writers += 1
            try:
                _access.wait_for(lambda: not _readers and not _writer)
                _writer = True
            finally:
                _waiting_writers -= 1
        else:
            _access.wait_for(lambda: not _writer and not _waiting_writers)
            _readers += 1
    try:
        yield
    finally:
        with _access:
            if exclusive:
                _writer = False
            else:
                _readers -= 1
            _access.notify_all()


def _dispatch(action, args):
    global _application
    if action in {"modelSettings", "prepareModelSettings", "prepareWatchSettings"}:
        root = Path(args["root"]).resolve()
        profile = ModelProfile.read(root)
        keys = _application.keys if _application and _application._root.resolve() == root else {}
        if action == "modelSettings":
            return profile.view(keys) | watch_view(root, profile, keys)
        if action == "prepareWatchSettings":
            return watch_credentials(prepare_watch(root, args))
        candidate = profile.prepare(args)
        slots = candidate.slots()
        return {"credentialName": slots.get(candidate.source, ""), "credentialNames": list(candidate.values({})),
                "revision": profile.revision}
    if action == "discoverModels":
        root = Path(args["root"]).resolve()
        keys = _application.keys if _application and _application._root.resolve() == root else {}
        return discover_models(root, purpose=args.get("purpose", "model"), protocol=args.get("protocol", ""),
                               base_url=args.get("baseUrl", ""), api_key=args.get("apiKey", ""),
                               revision=args.get("revision", ""), keys=keys)
    if action == "open":
        enforce_single_process()
        if _application is not None:
            _application.library.close()
            _application = None
        _application = Application(args["root"], args.get("keys", {}))
        return _application.profile.view(_application.keys)["credentials"]
    if _application is None:
        raise RuntimeError("资料库尚未打开")
    if action in {"saveModelSettings", "saveWatchSettings"}:
        if args.get("root") and Path(args["root"]).resolve() != _application._root.resolve():
            raise ValueError("模型设置不属于当前资料库")
        if action == "saveWatchSettings":
            root, profile = _application._root, _application.profile
            form = prepare_watch(root, args)
            slot = watch_credentials(form)["credentialName"]
            keys = profile.values(_application.keys)
            if slot:
                keys[slot] = (args.get("keys", {}).get(slot) or _application.keys.get(slot) or "").strip()
            save_watch(root, form, keys)
            _application.keys = keys
            return profile.view(keys) | watch_view(root, profile, keys)
        return _application.save_models(args)
    result = _application.call(action, args)
    if action == "close":
        _application = None
    return result


def import_from_window(args, on_progress):
    # The native importer can pause between segments without blocking Qt's UI thread.
    with _using_library(False):
        if _application is None:
            raise RuntimeError("资料库尚未打开")
        return _application.import_local(args, on_progress)


def dispatch(raw: str) -> str:
    args = {}
    try:
        request = json.loads(raw)
        action, args = request["action"], request["args"]
        with _using_library(action in {"open", "close", "saveModelSettings", "saveWatchSettings", "deleteImport", "deleteVersion", "deleteHistory"}):
            result = _dispatch(action, args)
        return json.dumps({"value": result}, ensure_ascii=False, allow_nan=False)
    except StaleEvidenceError:
        return json.dumps({"error": "来源版本已变化，请重新搜索。"}, ensure_ascii=False)
    except Exception as exc:
        message = str(exc) or type(exc).__name__
        supplied = args.get("keys", {}) if isinstance(args, dict) else {}
        secrets = list(supplied.values()) if isinstance(supplied, dict) else []
        if isinstance(args, dict) and isinstance(args.get("apiKey"), str):
            secrets.append(args["apiKey"])
        current = _application
        if current:
            secrets.extend(current.keys.values())
        for secret in secrets:
            if isinstance(secret, str) and secret:
                message = message.replace(secret, "••••")
        return json.dumps({"error": message[:512]}, ensure_ascii=False)
