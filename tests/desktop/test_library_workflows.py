"""Regression checks for per-video resumption and user-visible parse management actions."""
import sys
from time import monotonic, sleep

import pytest

pytest.importorskip("PySide6")
from PySide6.QtCore import QCoreApplication

from desktop.qt_controller import Workspace, _request
from tests.desktop.test_workspace import MemoryCredentials
from tests.server.test_desktop_pipeline import _fixed_model_responses, _fixture_movie


def test_player_retains_each_episode_and_reads_legacy_settings(tmp_path):
    app = QCoreApplication.instance() or QCoreApplication([])
    root = tmp_path / "library"
    root.mkdir()
    workspace = Workspace(root, credentials=MemoryCredentials())
    workspace._set("ready", True)
    series = [{"id": "A"}, {"id": "B"}]
    original = {"videoID": "A1", "durationMs": 60_000, "positionMs": 12_000}
    workspace._watch_settings.setValue("recent", {"A": original})
    workspace._restore_playback(series)
    assert workspace.playbackByVideo["A1"] == original
    workspace.rememberPlayback("A", "A2", 32_000, 60_000)
    workspace.rememberPlayback("B", "B1", 4_500, 12_000)
    workspace.shutdown()
    restored = Workspace(root, credentials=MemoryCredentials())
    try:
        restored._restore_playback(series)
        assert restored.lastWatchedSeries == "B"
        assert restored.recentPlayback["A"]["videoID"] == "A2"
        assert {video: item["positionMs"] for video, item in restored.playbackByVideo.items()} == {
            "A1": 12_000, "A2": 32_000, "B1": 4_500,
        }
    finally:
        restored.shutdown()


@pytest.mark.skipif(sys.platform not in {"darwin", "win32"}, reason="requires a desktop media encoder")
def test_desktop_reparse_history_and_remove_import(tmp_path, monkeypatch):
    monkeypatch.setattr("server.desktop.app.enforce_single_process", lambda: None)
    _fixed_model_responses(monkeypatch)
    movie = tmp_path / "episode.mp4"
    _fixture_movie(movie)
    _request("open", root=str(tmp_path / "library"), keys={"ARK_API_KEY": "fixture", "DEEPSEEK_API_KEY": "fixture"})
    def completed(version):
        deadline = monotonic() + 45
        while monotonic() < deadline:
            for job in _request("tasks"):
                if job["version"] == version and job["status"] == "COMPLETE":
                    return job
                assert not job["error"], job
            sleep(0.05)
        pytest.fail(f"missing completed version {version}")
    try:
        series = _request("createSeries", name="任务验收")
        _request("import", path=str(movie), seriesID=series["id"])
        one = completed(1)
        assert one["canReparse"] and one["canDelete"]
        _request("reparse", id=one["id"], version=1)
        two = completed(2)
        old = next(item for item in _request("tasks") if item["id"] == one["id"])
        assert old["canDeleteVersion"] and not old["canDelete"]
        with pytest.raises(RuntimeError, match="版本"):
            _request("deleteVersion", id=old["id"], version=99)
        _request("deleteVersion", id=old["id"], version=1)
        _request("reparse", id=two["id"], version=2)
        three = completed(3)
        _request("deleteHistory", id=three["id"], version=3)
        assert [job["version"] for job in _request("tasks")] == [3]
        _request("deleteImport", id=three["id"], version=3)
        assert _request("tasks") == [] and _request("seriesDetail", id=series["id"]) == []
        assert movie.exists()
    finally:
        _request("close")
