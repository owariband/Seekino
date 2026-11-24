"""Shared, asynchronous window model for the macOS and Windows desktop shells."""
from concurrent.futures import ThreadPoolExecutor
from html import escape
import json
import os
from pathlib import Path
import re
import sys

from PySide6.QtCore import QObject, Property, QSettings, QTimer, QUrl, Qt, Signal, Slot

from .credentials import platform_store


def default_library_root() -> Path:
    if sys.platform == "win32":
        return Path(os.environ["LOCALAPPDATA"]) / "Seekino"
    return Path.home() / "Library/Application Support/Seekino"


def _request(action, **args):
    # Importing the backend initializes its model dependencies; keep this off the UI thread.
    from server.desktop.app import dispatch

    response = json.loads(dispatch(json.dumps({"action": action, "args": args})))
    if "error" in response:
        raise RuntimeError(response["error"])
    return response["value"]


def _pages(action):
    rows = []
    while True:
        batch = _request(action, offset=len(rows))
        rows.extend(batch)
        if len(batch) < 100:
            return rows


def _cover(item):
    if item.get("cover"):
        return QUrl.fromLocalFile(item["cover"]).toString()
    return "image://posters/" + item["posterID"] if item["posterID"] else ""


def _highlight_description(description, query):
    # Only our <b> tags can reach the Qt styled-text parser.
    words = sorted(set(query.split()), key=len, reverse=True)
    if not words:
        return escape(description)
    matches = re.finditer("|".join(re.escape(word) for word in words), description, re.IGNORECASE)
    pieces = []
    end = 0
    for match in matches:
        pieces.extend((escape(description[end:match.start()]), "<b>" + escape(match.group()) + "</b>"))
        end = match.end()
    return "".join(pieces) + escape(description[end:])


def _videos(series_id):
    items = _request("seriesDetail", id=series_id)
    for item in items:
        item["cover"] = _cover(item)
    return items


def _snapshot(loaded):
    series, jobs = _pages("seriesList"), _pages("tasks")
    for item in series:
        item["cover"] = _cover(item)
    titles = {"PENDING": "等待解析", "PREPARING": "正在准备", "PARSING": "正在解析", "INDEXING": "正在建立索引",
              "COMPLETE": "可搜索", "INDEXING_FAILED": "索引失败", "PARSING_FAILED": "解析失败"}
    for job in jobs:
        job["label"] = "已暂停" if job["paused"] else titles.get(job["status"], job["status"])
    children = {row["id"]: _videos(row["id"]) for row in series if row["id"] in loaded}
    return series, jobs, children


def _state_property(qt_type, key, signal):
    return Property(qt_type, lambda self: self._state[key], notify=signal)


class Workspace(QObject):
    readyChanged = Signal()
    refreshingChanged = Signal()
    savingKeysChanged = Signal()
    recentPlaybackChanged = Signal()
    playbackByVideoChanged = Signal()
    lastWatchedSeriesChanged = Signal()
    busyImportChanged = Signal()
    importLabelChanged = Signal()
    importProgressChanged = Signal()
    searchingChanged = Signal()
    lastQueryChanged = Signal()
    partialChanged = Signal()
    searchIndexUnavailableChanged = Signal()
    modelConfigChanged = Signal()
    modelsDiscovered = Signal(str, object, bool)
    modelDiscoveryFailed = Signal(str, str)
    usageChanged = Signal()
    usageLoadingChanged = Signal()
    usageErrorChanged = Signal()
    importModelReadyChanged = Signal()
    searchModelReadyChanged = Signal()
    errorChanged = Signal()
    selectedSeriesChanged = Signal()
    seriesChanged = Signal()
    videosBySeriesChanged = Signal()
    taskPanelLoaded = Signal(str, str, int, "QVariantMap", str)
    jobsChanged = Signal()
    hitsChanged = Signal()
    episodeFindChanged = Signal()
    episodeFind = _state_property("QVariantMap", "episodeFind", episodeFindChanged)
    watchThreadsChanged = Signal()
    watchThreadIDChanged = Signal()
    watchThreads = _state_property("QVariantList", "watchThreads", watchThreadsChanged)
    watchThreadID = _state_property(str, "watchThreadID", watchThreadIDChanged)
    watchBusyChanged = Signal()
    watchErrorChanged = Signal()
    watchModelReadyChanged = Signal()
    watchMessagesChanged = Signal()
    watchStreamingTextChanged = Signal()
    watchStreamingText = _state_property(str, "watchStreamingText", watchStreamingTextChanged)
    watchBusy = _state_property(bool, "watchBusy", watchBusyChanged)
    watchModelReady = _state_property(bool, "watchModelReady", watchModelReadyChanged)
    watchError = _state_property(str, "watchError", watchErrorChanged)
    watchMessages = _state_property("QVariantList", "watchMessages", watchMessagesChanged)
    settingsNeeded = Signal(str)
    deferredActionResumed = Signal()
    credentialsSaved = Signal()
    watchConnectionSaved = Signal()
    watchConnectionFailed = Signal(str)
    seriesCreated = Signal(str)
    playbackContextChanged = Signal()
    playbackContext = _state_property("QVariantMap", "playbackContext", playbackContextChanged)
    playbackReady = Signal(str, "qlonglong", "qlonglong", "qlonglong", str, bool)
    _completed = Signal(object, object, object, bool)
    _watch_delta = Signal(int, str)
    _importing = Signal(str)

    ready = _state_property(bool, "ready", readyChanged)
    refreshing = _state_property(bool, "refreshing", refreshingChanged)
    savingKeys = _state_property(bool, "savingKeys", savingKeysChanged)
    playbackByVideo = _state_property("QVariantMap", "playbackByVideo", playbackByVideoChanged)
    lastWatchedSeries = _state_property(str, "lastWatchedSeries", lastWatchedSeriesChanged)
    recentPlayback = _state_property("QVariantMap", "recentPlayback", recentPlaybackChanged)
    busyImport = _state_property(bool, "busyImport", busyImportChanged)
    importLabel = _state_property(str, "importLabel", importLabelChanged)
    importProgress = _state_property(float, "importProgress", importProgressChanged)
    searching = _state_property(bool, "searching", searchingChanged)
    lastQuery = _state_property(str, "lastQuery", lastQueryChanged)
    partial = _state_property(bool, "partial", partialChanged)
    searchIndexUnavailable = _state_property(bool, "searchIndexUnavailable", searchIndexUnavailableChanged)
    modelConfig = _state_property("QVariantMap", "modelConfig", modelConfigChanged)
    usage = _state_property("QVariantMap", "usage", usageChanged)
    usageLoading = _state_property(bool, "usageLoading", usageLoadingChanged)
    usageError = _state_property(str, "usageError", usageErrorChanged)
    importModelReady = _state_property(bool, "importModelReady", importModelReadyChanged)
    searchModelReady = _state_property(bool, "searchModelReady", searchModelReadyChanged)
    error = _state_property(str, "error", errorChanged)
    selectedSeries = _state_property(str, "selectedSeries", selectedSeriesChanged)
    series = _state_property("QVariantList", "series", seriesChanged)
    videosBySeries = _state_property("QVariantMap", "videosBySeries", videosBySeriesChanged)
    jobs = _state_property("QVariantList", "jobs", jobsChanged)
    hits = _state_property("QVariantList", "hits", hitsChanged)

    def __init__(self, root=None, parent=None, *, credentials=None):
        super().__init__(parent)
        self.root = Path(root) if root is not None else default_library_root()
        from .library_tools import LibraryTools
        self._library_tools = LibraryTools(self, _request)
        self._credentials = credentials
        self._state = {
            "refreshing": False, "savingKeys": False, "recentPlayback": {}, "playbackByVideo": {}, "lastWatchedSeries": "",
            "ready": False, "busyImport": False, "importLabel": "", "importProgress": -1.0,
            "searching": False, "lastQuery": "", "partial": False, "searchIndexUnavailable": False, "importModelReady": False, "searchModelReady": False, "error": "",
            "selectedSeries": "", "series": [], "videosBySeries": {}, "jobs": [], "hits": [],
            "modelConfig": {}, "playbackContext": {}, "watchModelReady": False,
            "episodeFind": {}, "watchMessages": [], "watchError": "", "watchBusy": False,
            "watchThreads": [], "watchThreadID": "", "watchStreamingText": "",
            "usage": {}, "usageLoading": False, "usageError": "",
        }
        self._closing = self._starting = self._refresh_again = False
        self._watch_settings = QSettings(str(self.root / "watch-history.ini"), QSettings.Format.IniFormat)
        self._history = []
        self._backend_opened = False
        self._loaded = set()
        self._working_jobs = set()
        self._find_generation = 0
        self._find_timeline = None
        self._find_future = None
        self._play_request = 0
        self._deferred = None
        self._watch_requests = ThreadPoolExecutor(1, thread_name_prefix="SeekinoWatch")
        self._watch_generation = 0
        self._watch_video = None
        self._watch_future = None
        self._watch_control = None
        self._watch_threads = {}
        self._requests = ThreadPoolExecutor(1, thread_name_prefix="SeekinoLibrary")
        self._transfer = ThreadPoolExecutor(1, thread_name_prefix="SeekinoImport")
        self._queries = ThreadPoolExecutor(1, thread_name_prefix="SeekinoSearch")
        self._completed.connect(self._finish, Qt.ConnectionType.QueuedConnection)
        self._watch_delta.connect(self._show_partial_watch, Qt.ConnectionType.QueuedConnection)
        self._importing.connect(self._set_import_label, Qt.ConnectionType.QueuedConnection)
        self._poll = QTimer(self, interval=1500)
        self._poll.timeout.connect(self._poll_jobs)
        self._progress = QTimer(self, interval=250)
        self._progress.timeout.connect(self._poll_transfer)

    def _set(self, key, value):
        if self._state[key] != value:
            self._state[key] = value
            getattr(self, key + "Changed").emit()

    def _submit(self, work, done, *, executor=None, report_error=True):
        if self._closing:
            return

        def run():
            try:
                value = work()
            except Exception as exc:
                self._completed.emit(done, None, str(exc) or type(exc).__name__, report_error)
            else:
                self._completed.emit(done, value, "", report_error)

        return (executor or self._requests).submit(run)

    @Slot(object, object, object, bool)
    def _finish(self, done, value, error, report_error):
        if not self._closing:
            if error and report_error:
                self._set("error", error)
            done(value, error)

    @Slot(str)
    def _set_import_label(self, value):
        self._set("importLabel", value)
        self._set("importProgress", -1.0)

    @Property(QObject, constant=True)
    def libraryTools(self):
        return self._library_tools

    @Slot()
    def clearError(self):
        self._set("error", "")

    def _read_credentials(self, names, *, warnings=None):
        if not names:
            return {}
        if self._credentials is None:
            self._credentials = platform_store()
        keys = {}
        for name in names:
            try:
                keys[name] = self._credentials.read(name)
            except (OSError, RuntimeError) as exc:
                if warnings is None:
                    raise
                warnings.append(str(exc))
        return keys

    def _update_models(self, settings):
        self._set("modelConfig", settings)
        self._set("importModelReady", settings["importReady"])
        self._set("searchModelReady", settings["searchReady"])
        self._set("watchModelReady", settings.get("watchReady", False))

    @Slot()
    def start(self):
        if self.ready or self._starting or self._closing:
            return
        self._starting = True

        def open_library():
            settings = _request("modelSettings", root=str(self.root))
            keys = {}
            warnings = []
            try:
                names = list(settings["credentialNames"])
                if settings.get("watchCredentialName"):
                    names.append(settings["watchCredentialName"])
                keys = self._read_credentials(names, warnings=warnings)
            except (OSError, RuntimeError) as exc:
                warnings.append(str(exc))
            _request("open", root=str(self.root), keys=keys)
            self._backend_opened = True
            return _request("modelSettings", root=str(self.root)), "\n".join(dict.fromkeys(warnings)), _snapshot(set())

        def opened(value, error):
            self._starting = False
            if not error:
                settings, warning, (series, jobs, children) = value
                self._update_models(settings)
                self._set("series", series)
                self._set("jobs", jobs)
                self._set("videosBySeries", children)
                self._restore_playback(series)
                self._set("ready", True)
                if warning:
                    self._set("error", warning)
                self._poll.start()

        self._submit(open_library, opened)

    @Slot()
    def refresh(self):
        if not self.ready:
            self.start()
            return
        if self.refreshing:
            self._refresh_again = True
            return
        self._set("refreshing", True)
        loaded = self._loaded.copy()

        def refreshed(value, error):
            self._set("refreshing", False)
            if not error:
                series, jobs, children = value
                valid = {row["id"] for row in series}
                self._loaded.intersection_update(valid)
                self._set("series", series)
                self._set("jobs", jobs)
                self._set("videosBySeries", {k: v for k, v in (self.videosBySeries | children).items() if k in valid})
                if self.selectedSeries not in valid:
                    self._set("selectedSeries", "")
                self._resume_model_action()
            if self._refresh_again:
                self._refresh_again = False
                self.refresh()

        self._submit(lambda: _snapshot(loaded), refreshed)

    def _poll_jobs(self):
        active = {"PENDING", "PREPARING", "PARSING", "INDEXING"}
        if not self.refreshing and any(j["latest"] and not j["paused"] and j["status"] in active for j in self.jobs):
            self.refresh()

    def _poll_transfer(self):
        # importProgress reads only the progress number published by the import worker.
        if self.busyImport:
            value = _request("importProgress")
            self._set("importProgress", value if value is not None else -1.0)

    @Slot(str, str, int)
    def loadTaskPanel(self, task_id, section, generation):
        if self.ready and section in {"taskDetail", "taskUsage"}:
            self._submit(lambda: _request(section, id=task_id),
                         lambda value, error: self.taskPanelLoaded.emit(task_id, section, generation, value or {}, error or ""),
                         report_error=False)

    @Slot()
    def loadUsage(self):
        if not self.ready or self.usageLoading or self._closing:
            return
        self._set("usageLoading", True)
        self._set("usageError", "")

        def loaded(value, error):
            self._set("usageLoading", False)
            if error:
                self._set("usageError", error)
            else:
                self._set("usage", value)

        self._submit(lambda: _request("usage"), loaded, report_error=False)

    @Slot(str)
    def loadSeries(self, series_id):
        self._set("selectedSeries", series_id)
        self.ensureSeriesLoaded(series_id)

    @Slot(str)
    def ensureSeriesLoaded(self, series_id):
        if not self.ready or not series_id or series_id in self._loaded:
            return
        self._loaded.add(series_id)

        def loaded(rows, error):
            if error:
                self._loaded.discard(series_id)
            else:
                self._set("videosBySeries", self.videosBySeries | {series_id: rows})

        self._submit(lambda: _videos(series_id), loaded)

    def _restore_playback(self, series):
        entries = self._watch_settings.value("videos")
        if entries is None:
            # 0.2.12 stored one position per series without a timestamp.
            legacy = self._watch_settings.value("recent", {})
            entries = [dict(item, seriesID=key) for key, item in legacy.items()
                       if isinstance(item, dict)] if isinstance(legacy, dict) else []
        valid_series = {row["id"] for row in series}
        self._publish_playback([row for row in (entries if isinstance(entries, list) else [])
                                if isinstance(row, dict) and row.get("seriesID") in valid_series
                                and isinstance(row.get("videoID"), str)
                                and type(row.get("positionMs")) is int and type(row.get("durationMs")) is int
                                and row["durationMs"] > 0 and 0 <= row["positionMs"] <= row["durationMs"]])

    def _publish_playback(self, entries):
        recent, by_video = {}, {}
        for row in entries:
            item = {k: row[k] for k in ("videoID", "positionMs", "durationMs")}
            by_video[row["videoID"]] = item
            recent.setdefault(row["seriesID"], item)
        self._history = entries
        self._set("playbackByVideo", by_video)
        self._set("recentPlayback", recent)
        self._set("lastWatchedSeries", entries[0]["seriesID"] if entries else "")

    @Slot(str, str, "qlonglong", "qlonglong")
    def rememberPlayback(self, series_id, video_id, position, duration):
        if not self.ready or not series_id or not video_id or duration <= 0:
            return
        entries = [row for row in self._history if row["videoID"] != video_id]
        position = max(0, min(position, duration))
        entries.insert(0, {"seriesID": series_id, "videoID": video_id, "positionMs": position, "durationMs": duration})
        entries = entries[:100]
        if self._history != entries:
            self._publish_playback(entries)
            self._watch_settings.setValue("videos", entries)

    @Slot(str)
    def createSeries(self, name):
        name = name.strip()
        if not self.ready or not name:
            return

        def created(item, error):
            if not error:
                self.loadSeries(item["id"])
                self.refresh()
                self.seriesCreated.emit(item["id"])

        self._submit(lambda: _request("createSeries", name=name), created)

    @Slot("QVariantList", str)
    def importFiles(self, urls, series_id):
        if not self.ready or self.busyImport or self.savingKeys or not urls:
            return
        files = []
        for entry in urls:
            url = entry if isinstance(entry, QUrl) else QUrl(str(entry))
            path = url.toLocalFile() if url.isLocalFile() else str(entry)
            if Path(path).suffix.lower() not in {".mp4", ".mov", ".avi"}:
                self._set("error", "请选择 MP4、MOV 或 AVI 视频")
                return
            files.append(path)
        if not self.importModelReady and self.modelConfig.get("mode") != "new":
            self._deferred = ("import", (urls, series_id))
            self.settingsNeeded.emit("import")
            return
        self._set("busyImport", True)
        self._set_import_label("准备导入")
        self._progress.start()
        fallback = next((row["id"] for row in self.series if row["title"] == "我的素材"), "")

        def transfer():
            target = series_id or fallback or _request("createSeries", name="我的素材")["id"]
            errors = []
            for index, path in enumerate(files, 1):
                if self._closing:
                    break
                self._importing.emit(f"{index}/{len(files)} · {Path(path).name}")
                try:
                    _request("import", path=path, seriesID=target)
                except RuntimeError as exc:
                    errors.append(f"{Path(path).name} · {exc}")
            return target, errors

        def imported(value, error):
            self._progress.stop()
            self._set("busyImport", False)
            self._set_import_label("")
            if not error:
                target, errors = value
                self.loadSeries(target)
                if errors:
                    self._set("error", "\n".join(errors))
            self.refresh()

        self._submit(transfer, imported, executor=self._transfer)

    @Slot()
    def cancelDeferredImport(self):
        self._deferred = None

    @Slot("QVariantMap", "QVariantMap")
    def saveModelConfig(self, draft, secrets):
        self._save_connection(draft, secrets, watch=False)

    @Slot("QVariantMap", str)
    def saveWatchConnection(self, draft, key):
        self._save_connection(draft, {"connection": key}, watch=True)

    def _save_connection(self, draft, secrets, *, watch):
        if not self.ready or self.savingKeys or self.busyImport or self.searching or self._working_jobs:
            message = "请在当前操作结束后保存模型设置"
            if watch:
                self.watchConnectionFailed.emit(message)
            else:
                self._set("error", message)
            return
        draft = dict(draft)
        # QML Number arrives in a QVariantMap as a double even for whole dimensions.
        dimensions = draft.get("dimensions")
        if isinstance(dimensions, float) and dimensions.is_integer():
            draft["dimensions"] = int(dimensions)
        revision = draft.pop("revision", self.modelConfig.get("watchRevision" if watch else "revision"))
        retain = draft.pop("retainConfig", False)
        change = {"revision": revision, **({"retainConfig": True} if retain else {"form": draft})}
        self._set("savingKeys", True)

        def save():
            prepared = _request("prepareWatchSettings" if watch else "prepareModelSettings", root=str(self.root), **change)
            allowed = set(prepared["credentialNames"])
            prior = self._read_credentials(allowed)
            updates = {}
            for key, value in secrets.items():
                if not isinstance(value, str) or not value.strip():
                    continue
                name = prepared.get("credentialName") if key == "connection" and not retain else key
                if name not in allowed:
                    raise ValueError("凭据与当前服务地址不匹配，请重新打开设置")
                if value.strip() != prior[name]:
                    updates[name] = value.strip()
            written = []
            try:
                for name, value in updates.items():
                    self._credentials.write(name, value)
                    written.append(name)
                settings = _request("saveWatchSettings" if watch else "saveModelSettings", root=str(self.root), keys=prior | updates, **change)
            except Exception:
                rollback_failed = False
                for name in reversed(written):
                    try:
                        if prior[name]:
                            self._credentials.write(name, prior[name])
                        else:
                            self._credentials.delete(name)
                    except (OSError, RuntimeError):
                        rollback_failed = True
                if rollback_failed:
                    raise RuntimeError("模型设置未应用，部分凭据未能恢复。请检查系统凭据存储后重新保存。") from None
                raise
            return settings

        def saved(settings, error):
            self._set("savingKeys", False)
            if error:
                if watch:
                    self.watchConnectionFailed.emit(error)
                return
            self._update_models(settings)
            if watch:
                self.watchConnectionSaved.emit()
                return
            self.credentialsSaved.emit()
            # Retry readiness belongs to the original task's frozen model contract.
            self.refresh()

        self._submit(save, saved, report_error=not watch)

    @Slot(str, str, str, str, str, str)
    def discoverModels(self, request_id, purpose, protocol, base_url, api_key, revision):
        if self._closing:
            return

        def discovered(value, error):
            if error:
                self.modelDiscoveryFailed.emit(request_id, error)
            else:
                self.modelsDiscovered.emit(request_id, value.get("modelIds") or [],
                                           bool(value.get("listAvailable", True)))

        self._submit(lambda: _request("discoverModels", root=str(self.root), purpose=purpose, protocol=protocol,
                                      baseUrl=base_url, apiKey=api_key, revision=revision),
                     discovered, report_error=False)

    def _resume_model_action(self):
        if not self._deferred or self.savingKeys:
            return
        action, args = self._deferred
        if action == "retry":
            job = next((row for row in self.jobs if row["id"] == args[0]), None)
            if not job or not job["canRetry"] or not job["retryReady"]:
                return
        elif (action in {"import", "reparse"} and not self.importModelReady) or (action == "search" and not self.searchModelReady):
            return
        self._deferred = None
        self.deferredActionResumed.emit()
        if action == "import":
            self.importFiles(*args)
        elif action == "search":
            self.search(*args)
        elif action == "reparse":
            self.manageTask(args[0], "reparse")
        else:
            self.retryTask(*args)

    @Slot(str, bool, "QVariantList")
    def searchScopes(self, query, keywords, identifiers):
        self.search(query, keywords, identifiers)

    @Slot(str, bool, str)
    def search(self, query, keywords, series_id):
        query = query.strip()
        if not self.ready or self.searching or self.savingKeys or not query:
            return
        if not keywords and not self.searchModelReady:
            self._deferred = ("search", (query, keywords, series_id))
            self.settingsNeeded.emit("search")
            return
        selected = [series_id] if isinstance(series_id, str) else list(series_id)
        selected = [identifier for identifier in selected if identifier]
        scope = [item["id"] for item in self.series if not selected or item["id"] in selected]
        self._set("lastQuery", query)
        self._set("hits", [])
        self._set("partial", False)
        self._set("searchIndexUnavailable", False)
        if not scope:
            return
        self._set("searching", True)

        def searched(result, error):
            if not error:
                videos = {video["id"]: video for video in result["videos"]}
                series_names = {item["id"]: item["title"] for item in self.series}
                rows = []
                for hit in result["hits"]:
                    video = videos[hit["video_id"]]
                    rows.append({"sourceRef": hit["source_ref"], "videoID": video["id"], "title": video["title"],
                                 "cover": f"image://posters/hit/{result['previewScope']}/{hit['source_ref']}",
                                 "seriesID": video["seriesID"], "seriesTitle": series_names.get(video["seriesID"], ""),
                                 "episode": video["episode"], "filename": video["filename"],
                                 "highlighted": _highlight_description(hit["description"], query),
                                 "startMs": hit["start_ms"], "endMs": hit["end_ms"], "description": hit["description"]})
                self._set("hits", rows)
                self._set("partial", result["partial"])
                self._set("searchIndexUnavailable", result.get("indexUnavailable", False))
            self._set("searching", False)

        self._submit(lambda: _request("search", query=query, seriesIDs=scope, keywords=keywords), searched,
                     executor=self._queries)

    @Slot()
    def closeEpisodeFind(self):
        self._find_generation += 1
        self._find_timeline = None
        if self._find_future:
            self._find_future.cancel()
        self._set("episodeFind", {})

    @Slot(str, bool, bool)
    def findEpisode(self, query, visuals, smart):
        from .episode_find import episode_matches

        self._find_generation += 1
        generation = self._find_generation
        if self._find_future:
            self._find_future.cancel()
        video_id = self.playbackContext.get("videoID")
        query = query.strip()[:500]
        if not query or not video_id:
            self._set("episodeFind", {})
            return
        self._set("episodeFind", {"loading": True})

        def current():
            return generation == self._find_generation and video_id == self.playbackContext.get("videoID")

        def loaded(timeline, error):
            if not current():
                return
            if error:
                self._set("episodeFind", {"error": error})
                return
            self._find_timeline = (video_id, timeline)
            missing = not timeline["text"]["ready"] or (visuals and not timeline["visual"]["ready"])
            rows, can_search = episode_matches(timeline, query, visuals)
            result = {"matches": rows, "missing": missing}

            def interpreted(terms, error):
                if not current():
                    return
                if error:
                    recall(result | {"interpretUnavailable": True})
                    return
                expansions = list(dict.fromkeys(term for term in [terms["query"], *terms["synonyms"]] if term != query))
                if not expansions:
                    recall(result)
                    return
                expanded, _ = episode_matches(timeline, query, visuals, expansions=expansions)
                self._set("episodeFind", result | {"matches": expanded, "expansions": expansions})

            def recall(local):
                if not can_search or len(query) < 2:
                    self._set("episodeFind", local)
                    return
                if not self.searchModelReady:
                    self._set("episodeFind", local | {"relatedUnavailable": True})
                    return

                def related(cues, error):
                    if not current():
                        return
                    if error:
                        self._set("episodeFind", local | {"relatedUnavailable": True})
                        return
                    rows, _ = episode_matches(timeline, query, visuals, cues["cues"])
                    self._set("episodeFind", local | {"matches": rows, "relatedUnavailable": not cues["ready"]})

                self._set("episodeFind", local | {"loading": True})
                self._find_future = self._submit(lambda: _request("episodeRelated", videoID=video_id, query=query),
                                                 related, executor=self._queries, report_error=False)

            if smart and self.watchModelReady:
                self._set("episodeFind", result | {"interpreting": True})
                self._find_future = self._submit(lambda: _request("episodeInterpret", query=query), interpreted,
                                                 executor=self._queries, report_error=False)
            else:
                recall(result | {"interpretUnavailable": True} if smart else result)

        if self._find_timeline and self._find_timeline[0] == video_id:
            loaded(self._find_timeline[1], "")
        else:
            self._find_future = self._submit(lambda: _request("episodeTimeline", videoID=video_id),
                                             loaded, report_error=False)

    @Slot()
    def leaveWatch(self):
        self._watch_generation += 1
        self._watch_video = None
        if self._watch_control:
            self._watch_control.cancel()
            self._watch_control = None
        self._set("watchThreadID", "")
        self._set("watchStreamingText", "")
        self._set("watchThreads", [])
        if self._watch_future is not None:
            self._watch_future.cancel()
            self._watch_future = None
        self._set("watchBusy", False)
        self._set("watchError", "")
        self._set("watchMessages", [])

    def _show_watch(self, view):
        self._set("watchThreadID", view["threadID"])
        self._set("watchThreads", view["threads"])
        self._set("watchMessages", view["messages"])

    @Slot("QVariantList")
    def openLibraryConversation(self, selected):
        self.leaveWatch()
        self._watch_video = "@library"
        self._chat_scope = [item["id"] for item in self.series if not selected or item["id"] in selected]
        self.selectWatchThread(None, False)

    @Slot()
    def leaveLibraryConversation(self):
        if self._watch_video == "@library":
            self.leaveWatch()

    @Slot(str, bool)
    def selectWatchThread(self, thread, delete):
        if self.watchBusy or not self._watch_video:
            return
        video = self._watch_video
        self._watch_generation += 1
        generation = self._watch_generation
        self._set("watchBusy", True)
        def loaded(view, error):
            if generation != self._watch_generation:
                return
            self._set("watchBusy", False)
            self._set("watchError", error)
            if not error:
                self._show_watch(view)
        self._submit(lambda: _request("watchHistory", videoID=video, threadID=thread, delete=delete),
                     loaded, report_error=False)

    @Slot(int, str)
    def _show_partial_watch(self, generation, text):
        if generation != self._watch_generation or not self.watchBusy:
            return
        messages = list(self.watchMessages)
        if not messages:
            return
        self._set("watchStreamingText", text)
        if messages[-1]["role"] != "assistant":
            messages.append({"role": "assistant", "text": text, "streaming": True,
                             "positionMs": messages[-1]["positionMs"]})
            self._set("watchMessages", messages)

    @Slot()
    def stopWatch(self):
        if self._watch_control:
            self._watch_control.cancel()

    @Slot(str, "qlonglong", "QUrl", result=bool)
    @Slot(str, "qlonglong", result=bool)
    def askWatch(self, question, position_ms, attachment=None):
        question = question.strip()
        if not self.ready or self._closing or not self._watch_video or self.watchBusy or not question:
            return False
        if len(question) > 4000:
            self._set("watchError", "问题请控制在 4000 字以内。")
            return False
        if not self.watchModelReady:
            self.settingsNeeded.emit("watch")
            return False
        from gateway.usage import control_model_calls
        from .watch_control import WatchCallControl

        generation = self._watch_generation
        thread_id = self.watchThreadID
        control = self._watch_control = WatchCallControl()
        in_library = self._watch_video == "@library"
        reference = "" if in_library else self.playbackContext.get("sourceRef", "")
        attachment_path = attachment.toLocalFile() if attachment else ""
        video_id = self._watch_video
        series_id = self.playbackContext.get("seriesID", "")
        scope = self._chat_scope if in_library else []
        history = self.watchMessages
        pending = [*history, {"role": "user", "text": question, "positionMs": position_ms}]
        self._set("watchError", "")
        self._set("watchStreamingText", "")
        self._set("watchMessages", pending)
        self._set("watchBusy", True)

        def answer():
            from server.desktop.watch_history import watch_updates
            token = watch_updates.set(lambda text: self._watch_delta.emit(generation, text))
            try:
                with control_model_calls(control):
                    if in_library:
                        return _request("askLibrary", question=question, seriesIDs=scope, threadID=thread_id,
                                        attachmentPath=attachment_path, stream=True)
                    return _request("askWatch", videoID=video_id, seriesID=series_id, question=question,
                                    positionMs=position_ms, history=history[-8:], sourceRef=reference,
                                    threadID=thread_id, attachmentPath=attachment_path, stream=True)
            finally:
                watch_updates.reset(token)

        def answered(result, failure):
            if generation != self._watch_generation:
                return
            self._watch_future = None
            self._watch_control = None
            if result and result.get("watch"):
                self._show_watch(result["watch"])
                failure = result.get("watchFailure", "")
            if failure:
                self._set("watchError", "已停止回答。" if control.cancelled else failure)
            elif not result.get("watch"):
                messages = [*pending, {"role": "assistant", **result}]
                self._watch_threads[video_id] = messages
                self._set("watchMessages", messages)
            self._set("watchBusy", False)

        self._watch_future = self._submit(answer, answered, executor=self._watch_requests, report_error=False)
        return True

    def _open_media(self, action, args, title, *, ask_kino=False):
        if not self.ready:
            return
        self._play_request += 1
        request = self._play_request

        def opened(media, error):
            if not error and request == self._play_request:
                self.leaveWatch()
                self._watch_video = media["video_id"]
                if "watch" in media:
                    self._show_watch(media["watch"])
                else:
                    self._set("watchMessages", self._watch_threads.get(self._watch_video, []))
                self._set("playbackContext", {"videoID": media["video_id"], "seriesID": media["series_id"],
                                              "description": media["description"], "askKino": ask_kino,
                                              "sourceRef": args.get("sourceRef", ""), "clipStartMs": media["start_ms"],
                                              "clipEndMs": media["end_ms"]})
                self.playbackReady.emit(media["path"], media["start_ms"], media["end_ms"], media["duration_ms"],
                                        title, action == "openResult")

        self._submit(lambda: _request(action, **args), opened)

    @Slot()
    def cancelOpenMedia(self):
        self._play_request += 1

    @Slot(str, str)
    def openVideo(self, video_id, title):
        self._open_media("openVideo", {"videoID": video_id}, title)

    @Slot(str, str)
    def askAboutResult(self, reference, title):
        self._open_media("openResult", {"sourceRef": reference}, title, ask_kino=True)

    @Slot(str, str)
    def openResult(self, reference, title):
        self._open_media("openResult", {"sourceRef": reference}, title)

    @Slot(str)
    def pauseTask(self, job_id):
        self._update_job(job_id, "pause")

    @Slot(str)
    def retryTask(self, job_id):
        job = next((row for row in self.jobs if row["id"] == job_id), None)
        if not job or not job["canRetry"]:
            return
        if not job["retryReady"]:
            self._deferred = ("retry", (job_id,))
            self.settingsNeeded.emit("retry")
            return
        self._update_job(job_id, "retry")

    @Slot(str, str)
    def manageTask(self, job_id, action):
        if action not in {"reparse", "deleteImport", "deleteVersion", "deleteHistory"}:
            return
        if action == "reparse" and not self.importModelReady:
            self._deferred = ("reparse", (job_id,))
            self.settingsNeeded.emit("reparse")
            return
        self._update_job(job_id, action)

    def _update_job(self, job_id, action):
        if job_id in self._working_jobs or not self.ready:
            return
        job = next((row for row in self.jobs if row["id"] == job_id), None)
        if not job:
            return
        args = {"version": job["version"], "leaseVersion": job["leaseVersion"]}
        if action == "pause" and job["canPause"]:
            args.update(id=job_id, paused=not job["paused"])
        elif action in {"reparse", "deleteImport", "deleteVersion", "deleteHistory"}:
            args.update(id=job_id)
        elif action == "retry" and job["canRetry"]:
            args.update(uploadID=job["uploadID"])
        else:
            return
        self._working_jobs.add(job_id)

        def updated(_, error):
            self._working_jobs.discard(job_id)
            if not error and action == "deleteImport":
                history = [row for row in self._history if row["videoID"] != job["videoID"]]
                self._publish_playback(history)
                self._watch_settings.setValue("videos", history)
                self._set("hits", [hit for hit in self.hits if hit.get("videoID") != job["videoID"]])
            self.refresh()

        self._submit(lambda: _request(action, **args), updated)

    @Slot()
    def shutdown(self):
        if self._closing:
            return
        self._closing = True
        self._library_tools.shutting_down()
        if self._watch_control:
            self._watch_control.cancel()
        self._watch_settings.sync()
        self._poll.stop()
        self._progress.stop()
        for executor in (self._transfer, self._queries, self._watch_requests, self._requests):
            executor.shutdown(wait=True, cancel_futures=True)
        if self._backend_opened:
            _request("close")
            self._backend_opened = False
