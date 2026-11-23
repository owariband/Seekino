"""Shared, asynchronous window model for the macOS and Windows desktop shells."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sys

from PySide6.QtCore import QObject, Property, QTimer, QUrl, Qt, Signal, Slot

from .credentials import KEYS, platform_store


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
    busyImportChanged = Signal()
    importLabelChanged = Signal()
    importProgressChanged = Signal()
    searchingChanged = Signal()
    lastQueryChanged = Signal()
    partialChanged = Signal()
    modelReadyChanged = Signal()
    errorChanged = Signal()
    selectedSeriesChanged = Signal()
    seriesChanged = Signal()
    videosBySeriesChanged = Signal()
    jobsChanged = Signal()
    hitsChanged = Signal()
    modelKeysChanged = Signal()
    settingsNeeded = Signal()
    credentialsSaved = Signal()
    seriesCreated = Signal(str)
    playbackReady = Signal(str, "qlonglong", "qlonglong", "qlonglong", str, bool)
    _completed = Signal(object, object, object)
    _importing = Signal(str)

    ready = _state_property(bool, "ready", readyChanged)
    busyImport = _state_property(bool, "busyImport", busyImportChanged)
    importLabel = _state_property(str, "importLabel", importLabelChanged)
    importProgress = _state_property(float, "importProgress", importProgressChanged)
    searching = _state_property(bool, "searching", searchingChanged)
    lastQuery = _state_property(str, "lastQuery", lastQueryChanged)
    partial = _state_property(bool, "partial", partialChanged)
    modelReady = _state_property(bool, "modelReady", modelReadyChanged)
    error = _state_property(str, "error", errorChanged)
    selectedSeries = _state_property(str, "selectedSeries", selectedSeriesChanged)
    series = _state_property("QVariantList", "series", seriesChanged)
    videosBySeries = _state_property("QVariantMap", "videosBySeries", videosBySeriesChanged)
    jobs = _state_property("QVariantList", "jobs", jobsChanged)
    hits = _state_property("QVariantList", "hits", hitsChanged)
    modelKeys = _state_property("QVariantMap", "modelKeys", modelKeysChanged)

    def __init__(self, root=None, parent=None, *, credentials=None):
        super().__init__(parent)
        self.root = Path(root) if root is not None else default_library_root()
        self._credentials = credentials
        self._state = {
            "ready": False, "busyImport": False, "importLabel": "", "importProgress": -1.0,
            "searching": False, "lastQuery": "", "partial": False, "modelReady": False, "error": "",
            "selectedSeries": "", "series": [], "videosBySeries": {}, "jobs": [], "hits": [],
            "modelKeys": dict.fromkeys(KEYS, False),
        }
        self._closing = self._starting = self._refreshing = self._refresh_again = False
        self._saving = False
        self._backend_opened = False
        self._loaded = set()
        self._working_jobs = set()
        self._play_request = 0
        self._deferred = None
        self._requests = ThreadPoolExecutor(1, thread_name_prefix="SeekinoLibrary")
        self._transfer = ThreadPoolExecutor(1, thread_name_prefix="SeekinoImport")
        self._queries = ThreadPoolExecutor(1, thread_name_prefix="SeekinoSearch")
        self._completed.connect(self._finish, Qt.ConnectionType.QueuedConnection)
        self._importing.connect(self._set_import_label, Qt.ConnectionType.QueuedConnection)
        self._poll = QTimer(self, interval=1500)
        self._poll.timeout.connect(self._poll_jobs)
        self._progress = QTimer(self, interval=250)
        self._progress.timeout.connect(self._poll_transfer)

    def _set(self, key, value):
        if self._state[key] != value:
            self._state[key] = value
            getattr(self, key + "Changed").emit()

    def _submit(self, work, done, *, executor=None):
        if self._closing:
            return

        def run():
            try:
                value = work()
            except Exception as exc:
                self._completed.emit(done, None, str(exc) or type(exc).__name__)
            else:
                self._completed.emit(done, value, "")

        (executor or self._requests).submit(run)

    @Slot(object, object, object)
    def _finish(self, done, value, error):
        if not self._closing:
            if error:
                self._set("error", error)
            done(value, error)

    @Slot(str)
    def _set_import_label(self, value):
        self._set("importLabel", value)
        self._set("importProgress", -1.0)

    @Slot()
    def clearError(self):
        self._set("error", "")

    def _open_library(self, keys):
        self._backend_opened = False
        flags = _request("open", root=str(self.root), keys=keys)
        self._backend_opened = True
        return flags

    @Slot()
    def start(self):
        if self.ready or self._starting or self._closing:
            return
        self._starting = True

        def open_library():
            keys = {}
            warning = ""
            try:
                if self._credentials is None:
                    self._credentials = platform_store()
                keys = {name: self._credentials.read(name) for name in KEYS}
            except (OSError, RuntimeError) as exc:
                warning = str(exc)
            keys = self._open_library(keys)
            return keys, warning, _snapshot(set())

        def opened(value, error):
            self._starting = False
            if not error:
                keys, warning, (series, jobs, children) = value
                self._update_keys(keys)
                self._set("series", series)
                self._set("jobs", jobs)
                self._set("videosBySeries", children)
                self._set("ready", True)
                if warning:
                    self._set("error", warning)
                self._poll.start()

        self._submit(open_library, opened)

    def _update_keys(self, keys):
        self._set("modelKeys", keys)
        self._set("modelReady", all(keys.get(name) for name in KEYS))

    @Slot()
    def refresh(self):
        if not self.ready:
            self.start()
            return
        if self._refreshing:
            self._refresh_again = True
            return
        self._refreshing = True
        loaded = self._loaded.copy()

        def refreshed(value, error):
            self._refreshing = False
            if not error:
                series, jobs, children = value
                valid = {row["id"] for row in series}
                self._loaded.intersection_update(valid)
                self._set("series", series)
                self._set("jobs", jobs)
                self._set("videosBySeries", {k: v for k, v in (self.videosBySeries | children).items() if k in valid})
                if self.selectedSeries not in valid:
                    self._set("selectedSeries", "")
            if self._refresh_again:
                self._refresh_again = False
                self.refresh()

        self._submit(lambda: _snapshot(loaded), refreshed)

    def _poll_jobs(self):
        active = {"PENDING", "PREPARING", "PARSING", "INDEXING"}
        if not self._refreshing and any(j["latest"] and not j["paused"] and j["status"] in active for j in self.jobs):
            self.refresh()

    def _poll_transfer(self):
        # importProgress reads only the progress number published by the import worker.
        if self.busyImport:
            value = _request("importProgress")
            self._set("importProgress", value if value is not None else -1.0)

    @Slot(str)
    def loadSeries(self, series_id):
        self._set("selectedSeries", series_id)
        if not self.ready or not series_id or series_id in self._loaded:
            return
        self._loaded.add(series_id)

        def loaded(rows, error):
            if error:
                self._loaded.discard(series_id)
            else:
                self._set("videosBySeries", self.videosBySeries | {series_id: rows})

        self._submit(lambda: _videos(series_id), loaded)

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
        if not self.ready or self.busyImport or self._saving or not urls:
            return
        files = []
        for entry in urls:
            url = entry if isinstance(entry, QUrl) else QUrl(str(entry))
            path = url.toLocalFile() if url.isLocalFile() else str(entry)
            if Path(path).suffix.lower() not in {".mp4", ".mov", ".avi"}:
                self._set("error", "请选择 MP4、MOV 或 AVI 视频")
                return
            files.append(path)
        if not self.modelReady:
            self._deferred = (urls, series_id)
            self.settingsNeeded.emit()
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

    @Slot(str, str)
    def saveKeys(self, ark, deepseek):
        if self._saving or self.busyImport or self.searching or self._starting:
            self._set("error", "请在当前操作结束后保存模型设置")
            return
        self._saving = True
        changes = dict(zip(KEYS, (ark.strip(), deepseek.strip()), strict=True))

        def save():
            if self._credentials is None:
                self._credentials = platform_store()
            for name, key in changes.items():
                if key:
                    self._credentials.write(name, key)
            keys = {name: self._credentials.read(name) for name in KEYS}
            return self._open_library(keys)

        def saved(keys, error):
            self._saving = False
            if error:
                self._set("ready", self._backend_opened)
            else:
                self._update_keys(keys)
                self._set("ready", True)
                self._poll.start()
                pending, self._deferred = self._deferred, None
                self.credentialsSaved.emit()
                self.refresh()
                if pending:
                    self.importFiles(*pending)

        self._submit(save, saved)

    @Slot(str, bool, str)
    def search(self, query, keywords, series_id):
        query = query.strip()
        if not self.ready or self.searching or self._saving or not query:
            return
        if not keywords and not self.modelReady:
            self.settingsNeeded.emit()
            return
        scope = [series_id] if series_id else [item["id"] for item in self.series]
        self._set("lastQuery", query)
        self._set("hits", [])
        self._set("partial", False)
        if not scope:
            return
        self._set("searching", True)

        def searched(result, error):
            if not error:
                videos = {video["id"]: video for video in result["videos"]}
                rows = []
                for hit in result["hits"]:
                    video = videos[hit["video_id"]]
                    rows.append({"sourceRef": hit["source_ref"], "videoID": video["id"], "title": video["title"],
                                 "cover": _cover(video),
                                 "startMs": hit["start_ms"], "endMs": hit["end_ms"], "description": hit["description"]})
                self._set("hits", rows)
                self._set("partial", result["partial"])
            self._set("searching", False)

        self._submit(lambda: _request("search", query=query, seriesIDs=scope, keywords=keywords), searched,
                     executor=self._queries)

    def _open_media(self, action, args, title):
        if not self.ready:
            return
        self._play_request += 1
        request = self._play_request

        def opened(media, error):
            if not error and request == self._play_request:
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
    def openResult(self, reference, title):
        self._open_media("openResult", {"sourceRef": reference}, title)

    @Slot(str)
    def pauseTask(self, job_id):
        self._update_job(job_id, "pause")

    @Slot(str)
    def retryTask(self, job_id):
        if not self.modelReady:
            self.settingsNeeded.emit()
            return
        self._update_job(job_id, "retry")

    def _update_job(self, job_id, action):
        if job_id in self._working_jobs or not self.ready:
            return
        job = next((row for row in self.jobs if row["id"] == job_id), None)
        if not job:
            return
        args = {"version": job["version"], "leaseVersion": job["leaseVersion"]}
        if action == "pause" and job["canPause"]:
            args.update(id=job_id, paused=not job["paused"])
        elif action == "retry" and job["canRetry"]:
            args.update(uploadID=job["uploadID"])
        else:
            return
        self._working_jobs.add(job_id)

        def updated(_, error):
            self._working_jobs.discard(job_id)
            self.refresh()

        self._submit(lambda: _request(action, **args), updated)

    @Slot()
    def shutdown(self):
        if self._closing:
            return
        self._closing = True
        self._poll.stop()
        self._progress.stop()
        for executor in (self._transfer, self._queries, self._requests):
            executor.shutdown(wait=True, cancel_futures=True)
        if self._backend_opened:
            _request("close")
            self._backend_opened = False
