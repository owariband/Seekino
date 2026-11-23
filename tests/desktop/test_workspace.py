"""Drive the desktop's asynchronous UI contract against its real local library."""
import sys
from time import monotonic, sleep

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("sqlite_vec")
from PySide6.QtCore import QCoreApplication, QSize, QUrl

from desktop.posters import PosterProvider
from desktop.qt_controller import Workspace
from tests.server.test_desktop_pipeline import _fixed_model_responses, _fixture_movie


class MemoryCredentials(dict):
    def read(self, name):
        return self.get(name, "")

    def write(self, name, value):
        self[name] = value


@pytest.mark.skipif(sys.platform not in {"win32", "darwin"}, reason="requires a packaged desktop media encoder")
def test_import_search_and_open_hit_through_window_model(tmp_path, monkeypatch):
    app = QCoreApplication.instance() or QCoreApplication([])
    source = tmp_path / "桌面 搜索.mp4"
    _fixture_movie(source)
    _fixed_model_responses(monkeypatch)
    # The packaged app installs its process-wide audit hook; keep pytest itself reusable.
    monkeypatch.setattr("server.desktop.app.enforce_single_process", lambda: None)
    monkeypatch.delenv("ARK_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    credentials = MemoryCredentials()
    workspace = Workspace(tmp_path / "library", credentials=credentials)

    def wait(predicate):
        deadline = monotonic() + 50
        while not predicate():
            app.processEvents()
            assert not workspace.error, workspace.error
            assert monotonic() < deadline, workspace.jobs
            sleep(0.01)

    try:
        workspace.start()
        wait(lambda: workspace.ready)
        workspace.createSeries("桌面验收")
        wait(lambda: workspace.series and workspace.selectedSeries)
        series_id = workspace.selectedSeries
        settings = []
        workspace.settingsNeeded.connect(lambda: settings.append(True))
        workspace.importFiles([QUrl.fromLocalFile(str(source))], series_id)
        assert settings and not workspace.busyImport
        workspace.saveKeys("fixture", "fixture")
        wait(lambda: workspace.jobs and workspace.jobs[0]["status"] == "COMPLETE")
        assert workspace.modelReady and workspace.videosBySeries[series_id][0]["title"]
        for lexical in (True, False):
            workspace.search("红旗" if lexical else "挥动旗帜", lexical, series_id)
            wait(lambda: not workspace.searching)
            hit, = workspace.hits
            assert (hit["startMs"], hit["endMs"]) == (300, 1200)
        series, = workspace.series
        assert series["firstVideoID"] == hit["videoID"]
        assert hit["cover"] == series["cover"] == "image://posters/" + series["posterID"]
        poster = PosterProvider().requestImage(series["posterID"], QSize(), QSize())
        assert poster.size() == QSize(128, 96)
        media = []
        workspace.playbackReady.connect(lambda *args: media.append(args))
        workspace.openResult(hit["sourceRef"], hit["title"])
        workspace.cancelOpenMedia()  # A page change while source verification is queued.
        workspace.openResult(hit["sourceRef"], hit["title"])
        wait(lambda: media)
        assert media[0][1:3] == (300, 1200) and media[0][-1] is True
        workspace.openVideo(hit["videoID"], hit["title"])
        wait(lambda: len(media) == 2)
        assert len(media) == 2
        assert media[1][0] == media[0][0] and media[1][1] == 0 and media[1][-1] is False
    finally:
        workspace.shutdown()

    workspace = Workspace(tmp_path / "library", credentials=credentials)
    initial = []
    workspace.readyChanged.connect(lambda: initial.append((len(workspace.series), len(workspace.jobs))))
    try:
        workspace.start()
        wait(lambda: workspace.ready)
        assert initial == [(1, 1)]
    finally:
        workspace.shutdown()
