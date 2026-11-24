"""The desktop import queue owns transfer cancellation and preserves resumable staging."""
import hashlib
from pathlib import Path

import pytest

from server.desktop.app import import_from_window
from server.desktop.model_settings import LEGACY_KEYS
from tests.server.test_desktop_model_settings import form, request
from tests.server.test_desktop_pipeline import _fixture_movie


def test_cancel_interrupt_resume_and_manage_series(tmp_path, monkeypatch):
    monkeypatch.setattr("server.desktop.app.enforce_single_process", lambda: None)
    for name in LEGACY_KEYS:
        monkeypatch.delenv(name, raising=False)
    movie = tmp_path / "episode-03.mp4"
    _fixture_movie(movie)
    request("open", root=str(tmp_path / "library"))
    try:
        series = request("createSeries", name="选集")["id"]
        args = {"path": str(movie), "episode": 3, "seriesID": series}
        def cancel(*_):
            raise InterruptedError("取消")
        with pytest.raises(InterruptedError):
            import_from_window(args, cancel)
        meta = request("libraryMetadata", seriesID=series)
        assert not meta["videos"] and not meta["occupied"] and movie.exists()

        def suspend(*_):
            raise RuntimeError("退出")
        with pytest.raises(RuntimeError, match="退出"):
            import_from_window(args, suspend)
        assert request("libraryMetadata", seriesID=series)["occupied"] == {"3": movie.name}
        request("close")
        request("open", root=str(tmp_path / "library"))
        result = request("import", **args)
        assert Path(request("openVideo", videoID=result["videoID"])["path"]).read_bytes() == movie.read_bytes()

        from PIL import Image
        picture = tmp_path / "参考.png"
        Image.new("RGB", (80, 60), "green").save(picture)
        asset = request("backgroundImage", seriesID=series, path=str(picture))
        assert asset.startswith("asset://")
        background = {"format":"markdown", "content":f"![参考]({asset})"}
        request("updateLibrary", seriesID=series, changes={"important_extra_data":background})
        assert request("libraryMetadata", seriesID=series)["background"] == background

        with pytest.raises(ValueError):
            request("changeEpisode", seriesID=series, videoID=result["videoID"], episode=0)
        assert request("libraryMetadata", seriesID=series)["videos"][0]["episode"] == 3
    finally:
        request("close")


def test_first_model_choice_preserves_pause_on_legacy_demo(tmp_path, monkeypatch):
    from server.desktop.runtime import open_library
    from server.desktop.model_settings import ModelProfile
    monkeypatch.setattr("server.desktop.app.enforce_single_process", lambda: None)
    for name in LEGACY_KEYS:
        monkeypatch.delenv(name, raising=False)
    movie = tmp_path / "sample.mp4"
    root = tmp_path / "samples"
    _fixture_movie(movie)
    config, env = ModelProfile.read(root).for_gateway({})
    with open_library(root, gateway_config=config, gateway_environ=env, start_consumer=False) as library:
        series = library.create_series("示例")
        result = library.import_file(movie, series_id=series.series_id, episode_order=1,
                                     idempotency_key="sample-" + hashlib.sha256(movie.read_bytes()).hexdigest())
        task = library.task(result.video_parse_task_id).task
        library.set_paused(task.video_parse_task_id, paused=True, expected_version=task.version,
                           expected_lease_version=task.lease_version)
    request("open", root=str(root))
    try:
        settings = dict(root=str(root), form=form(auth="none"), revision=request("modelSettings", root=str(root))["revision"])
        from server.mq.video_parse_consumer import VideoParseConsumer
        def fail_start(_):
            raise RuntimeError("模拟启动失败")
        with monkeypatch.context() as startup:
            startup.setattr(VideoParseConsumer, "start", fail_start)
            with pytest.raises(ValueError, match="模拟启动失败"):
                request("saveModelSettings", **settings)
        assert request("modelSettings", root=str(root))["mode"] == "new"
        request("saveModelSettings", **settings)
        job, = request("tasks")
        assert job["paused"] and job["status"] == "PENDING"
        changed = dict(settings, form=form(auth="none",dimensions=128), revision=request("modelSettings",root=str(root))["revision"])
        with pytest.raises(ValueError, match="向量模型"):
            request("prepareModelSettings", **changed)
    finally:
        request("close")
