"""Application-owned download; Qt only publishes state on its UI thread."""
from http.client import HTTPException
import ssl
import threading
from urllib.request import Request, urlopen

from PySide6.QtCore import QObject, Property, Signal, Slot

from gateway.local import qwen_assets as assets


class EmbeddingDownload(QObject):
    changed = Signal()
    ready = Signal()
    updateReceived = Signal(str, float, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._state, self._progress, self._error = "missing", 0., ""
        self._cancel = threading.Event()
        self._thread = None
        self.updateReceived.connect(self._update)
        if assets.model_path().is_file():
            self._begin(check_only=True)

    @Property(str, notify=changed)
    def state(self):
        return self._state

    @Property(float, notify=changed)
    def progress(self):
        return self._progress

    @Property(str, notify=changed)
    def errorMessage(self):
        return self._error

    @Slot(str, float, str)
    def _update(self, state, progress, error):
        self._state, self._progress, self._error = state, progress, error
        self.changed.emit()
        if state == "ready":
            self.ready.emit()

    @Slot()
    def start(self):
        if self._state in ("missing", "error"):
            self._begin(check_only=False)

    def _begin(self, *, check_only):
        self._cancel = threading.Event()
        self._update("verifying" if check_only else "downloading", 0., "")
        self._thread = threading.Thread(target=self._run, args=(check_only, self._cancel),
                                        name="Seekino model download", daemon=True)
        self._thread.start()

    def _run(self, check_only, cancel):
        try:
            if check_only:
                assets.verified_model_path(cancel=cancel)
            else:
                # macOS and Windows proxy settings are honored by urllib's default opener.
                request = Request(assets.MODEL_URL, headers={"User-Agent": "Seekino-Desktop", "Accept-Encoding": "identity"})
                import certifi
                from server.utils.file_lock import exclusive_lock
                folder = assets.model_path().parent
                folder.mkdir(parents=True, exist_ok=True)
                with exclusive_lock(folder / "download.lock", blocking=False):
                    # No other instance owns a writer. Discard leftovers from an interrupted app exit.
                    for partial in folder.glob("qwen-download-*.part"):
                        partial.unlink(missing_ok=True)
                    if assets.local_model_ready():
                        self.updateReceived.emit("ready", 1., "")
                        return
                    tls = ssl.create_default_context(cafile=certifi.where())
                    with urlopen(request, timeout=20, context=tls) as response:
                        def report(received):
                            state = "verifying" if received == assets.MODEL_BYTES else "downloading"
                            self.updateReceived.emit(state, received / assets.MODEL_BYTES, "")
                        assets.fetch_weights(response, cancel, report)
            self.updateReceived.emit("ready", 1., "")
        except (assets.DownloadCancelled, OSError, HTTPException, ValueError) as exc:
            if cancel.is_set():
                self.updateReceived.emit("missing", 0., "")
            else:
                self.updateReceived.emit("error", 0., str(exc))

    @Slot()
    def cancel(self):
        if self._state in ("downloading", "verifying"):
            self._cancel.set()

    @Slot()
    def shutdown(self):
        self._cancel.set()
        if self._thread is not None:
            self._thread.join(timeout=.3)
