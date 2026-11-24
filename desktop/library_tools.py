"""UI state for staged imports and asynchronous library editing."""
from pathlib import Path
import re
from threading import Condition

from PySide6.QtCore import QObject, Signal, Slot, QUrl


class LibraryTools(QObject):
    metadataReady = Signal(str, int, "QVariantMap")
    completed = Signal(str, str)
    failed = Signal(str, str)
    progress = Signal(int, float)
    fileFinished = Signal(int, str, str)

    def __init__(self, workspace, request):
        super().__init__(workspace)
        self.workspace = workspace
        self.request = request
        self.gate = Condition()
        self.control = "running"
        self.progress.connect(self._record_progress)
        self.current = -1

    @Slot(int, float)
    def _record_progress(self, index, value):
        if index == self.current:
            self.workspace._set("importProgress", value)

    @Slot(str, int)
    def load(self, series, generation):
        def loaded(value, error):
            if error:
                self.failed.emit("load", error)
            else:
                self.metadataReady.emit(series, generation, value)
        self.workspace._submit(lambda: self.request("libraryMetadata", seriesID=series), loaded, report_error=False)

    @Slot("QVariantList", result="QVariantList")
    def files(self, urls):
        rows = []
        for url in urls:
            url = url if isinstance(url, QUrl) else QUrl(str(url))
            path = Path(url.toLocalFile() if url.isLocalFile() else url.toString())
            if path.suffix.lower() not in {".mp4", ".mov", ".avi"} or not path.is_file():
                self.failed.emit("prepare", "请选择本机的 MP4、MOV 或 AVI 文件。")
                return []
            numbers = re.findall(r"\d{1,3}", path.stem)
            rows.append({"path": str(path), "name": path.name, "hint": int(numbers[-1]) if numbers else 0})
        return rows

    @Slot(str, result=str)
    def localPath(self, url):
        return QUrl(url).toLocalFile()

    @Slot(str, "QVariantMap")
    def manage(self, action, args):
        if action not in {"exportLibrary", "updateLibrary", "changeEpisode", "backgroundImage"}:
            return
        def done(result, error):
            if error:
                self.failed.emit(action, error)
                return
            if action in {"updateLibrary", "changeEpisode"}:
                self.workspace.refresh()
            self.completed.emit(action, f"已导出 {result} 条事件" if action == "exportLibrary" else result or "")
        self.workspace._submit(lambda: self.request(action, **args), done, report_error=False)

    @Slot(int, str, str, int)
    def transfer(self, index, path, series, episode):
        ws = self.workspace
        if not ws.ready or ws.busyImport or ws.savingKeys or self.current != -1:
            self.fileFinished.emit(index, "failed", "当前操作尚未结束。")
            return
        self.current = index
        self.control = "running"
        ws._set("busyImport", True)
        ws._set_import_label(Path(path).name)

        def checkpoint(stored, total):
            with self.gate:
                self.gate.wait_for(lambda: self.control != "paused")
                if self.control == "stop":
                    raise InterruptedError("已取消导入")
                if self.control == "shutdown":
                    raise RuntimeError("本次传输已暂停，下次选择相同文件即可继续。")
            self.progress.emit(index, stored / total)
        def run():
            from server.desktop.app import import_from_window
            from server.repository.upload import UploadConflictError
            target = series or self.request("createSeries", name="我的素材")["id"]
            try:
                import_from_window({"path": path, "seriesID": target, "episode": episode}, checkpoint)
            except InterruptedError:
                return target, "cancelled", ""
            except UploadConflictError as exc:
                if "already identifies another Video" in str(exc):
                    return target, "failed", "这个视频已经在该系列中。可在系列页调整集号。"
                return target, "failed", str(exc)
            except Exception as exc:
                return target, "failed", str(exc)
            return target, "done", ""
        def done(value, error):
            with self.gate:
                self.current = -1
            ws._set("busyImport", False)
            ws._set_import_label("")
            if error:
                self.fileFinished.emit(index, "failed", error)
                return
            target, status, message = value
            ws.loadSeries(target)
            ws.refresh()
            self.completed.emit("destination", target)
            self.fileFinished.emit(index, status, message)
        ws._submit(run, done, executor=ws._transfer, report_error=False)

    @Slot(int, str)
    def controlFile(self, index, mode):
        if mode in ("running", "paused", "stop"):
            with self.gate:
                if index == self.current:
                    self.control = mode
                    self.gate.notify_all()

    def shutting_down(self):
        with self.gate:
            self.control = "shutdown"
            self.gate.notify_all()
