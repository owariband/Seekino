"""Drive the desktop's asynchronous UI contract against its real local library."""
import os
import socket
import sys
from pathlib import Path
from time import monotonic, sleep

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("sqlite_vec")
from PySide6.QtCore import QCoreApplication, QSize, QUrl

from desktop.posters import PosterProvider
from desktop.qt_controller import Workspace, _highlight_description, _request
from tests.server.test_desktop_pipeline import _fixed_model_responses, _fixture_movie


@pytest.fixture(autouse=True)
def offline_models(monkeypatch):
    connect = socket.socket.connect

    def local_only(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            raise AssertionError("desktop model replay must stay offline")
        return connect(sock, address)

    monkeypatch.setattr(socket.socket, "connect", local_only)


class MemoryCredentials(dict):
    def read(self, name):
        return self.get(name, "")

    def write(self, name, value):
        self[name] = value

    def delete(self, name):
        self.pop(name, None)


@pytest.mark.skipif(sys.platform not in {"win32", "darwin"}, reason="requires a packaged desktop media encoder")
def test_import_search_and_open_hit_through_window_model(tmp_path, monkeypatch):
    app = QCoreApplication.instance() or QCoreApplication([])
    from engine.common.config import VideoParseRuntimeConfig
    from server.desktop import runtime

    source = tmp_path / "桌面 搜索.mp4"
    _fixture_movie(source)
    _fixed_model_responses(monkeypatch, on_chunk=lambda: None)
    monkeypatch.setattr(runtime, "VideoParseRuntimeConfig", lambda **kwargs: VideoParseRuntimeConfig(
        chunk_duration_seconds=1, context_overlap_seconds=0, **kwargs,
    ))
    # The packaged app installs its process-wide audit hook; keep pytest itself reusable.
    monkeypatch.setattr("server.desktop.app.enforce_single_process", lambda: None)
    monkeypatch.delenv("ARK_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    original_environment = dict(os.environ)
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
        workspace.settingsNeeded.connect(settings.append)
        workspace.libraryTools.transfer(0, str(source), series_id, 1)
        wait(lambda: not workspace.busyImport and workspace.videosBySeries.get(series_id))
        assert not settings and workspace.jobs[0]["status"] == "PENDING"
        video_id = workspace.videosBySeries[series_id][0]["id"]
        assert Path(_request("openVideo", videoID=video_id)["path"]).read_bytes() == source.read_bytes()
        assert workspace.modelConfig["mode"] == "new"
        workspace.saveModelConfig({
            "name": "验收模型", "protocol": "ark", "baseUrl": "https://ark.cn-beijing.volces.com/api/v3",
            "vision": "doubao-seed-2-0-lite-260428", "transcription": "doubao-seed-2-0-lite-260428",
            "embedding": "doubao-embedding-vision-250615", "dimensions": 1024,
            "encoderModel": "", "rerank": "", "revision": workspace.modelConfig["revision"],
        }, {"connection": "fixture"})
        wait(lambda: workspace.jobs and workspace.jobs[0]["status"] == "COMPLETE")
        assert workspace.importModelReady and workspace.searchModelReady
        assert len(credentials) == 1 and "fixture" not in str(workspace.modelConfig)
        assert all(os.environ.get(name) == original_environment.get(name) for name in credentials)
        assert "ARK_API_KEY" not in os.environ and "DEEPSEEK_API_KEY" not in os.environ
        assert "fixture" not in (workspace.root / "models.yaml").read_text()
        assert workspace.videosBySeries[series_id][0]["title"]
        for lexical in (True, False):
            workspace.search("红旗" if lexical else "挥动旗帜", lexical, series_id)
            wait(lambda: not workspace.searching)
            hits = sorted(workspace.hits, key=lambda item: item["startMs"])
            assert [(row["startMs"], row["endMs"]) for row in hits] == [(300, 1000), (1300, 2000)]
        hit = hits[0]
        assert hit["seriesTitle"] == "桌面验收" and hit["filename"] == source.name and hit["episode"] == 1
        series, = workspace.series
        assert series["firstVideoID"] == hit["videoID"]
        assert series["cover"] == "image://posters/" + series["posterID"]
        provider = PosterProvider()
        previews = [QUrl(row["cover"]).path().lstrip("/") for row in hits]
        pictures = [provider.requestImage(key, QSize(), QSize()) for key in previews]
        assert all(image.size() == QSize(128, 96) for image in pictures)
        assert pictures[1].pixelColor(40, 40).red() - pictures[0].pixelColor(40, 40).red() > 65
        for invalid in (previews[0] + "/../file", "hit/../private/local",
                        "hit/" + "0" * 64 + "/" + hit["sourceRef"]):
            assert provider.requestImage(invalid, QSize(), QSize()).isNull()
        poster = PosterProvider().requestImage(series["posterID"], QSize(), QSize())
        assert poster.size() == QSize(128, 96)
        # History artwork must use the original at the saved position even with a thumbnail.
        def home_picture(surface, position):
            return provider.requestImage(f"{surface}/{series['posterID']}/{position}", QSize(), QSize())
        early = home_picture("hero", 100)
        later = home_picture("hero", 1400)
        assert early.size() == later.size() == QSize(128, 96)
        assert later.pixelColor(40, 40).red() - early.pixelColor(40, 40).red() > 65
        assert home_picture("hero", 100).cacheKey() == early.cacheKey()
        shelf = home_picture("shelf", 100)
        assert shelf.size() == early.size()
        assert home_picture("shelf", 100).cacheKey() == shelf.cacheKey()
        assert home_picture("hero", -1).size() == QSize(128, 96)
        assert home_picture("shelf", -1).size() == QSize(128, 96)
        assert home_picture("hero", "bad").isNull()
        media = []
        workspace.playbackReady.connect(lambda *args: media.append(args))
        workspace.openResult(hit["sourceRef"], hit["title"])
        workspace.cancelOpenMedia()  # A page change while source verification is queued.
        workspace.openResult(hit["sourceRef"], hit["title"])
        wait(lambda: media)
        assert media[0][1:3] == (300, 1000) and media[0][-1] is True
        assert workspace.playbackContext == {"videoID": hit["videoID"], "seriesID": series_id,
                                             "description": hit["description"], "sourceRef": hit["sourceRef"],
                                         "askKino": False, "clipStartMs": 300, "clipEndMs": 1000}
        workspace.openVideo(hit["videoID"], hit["title"])
        wait(lambda: len(media) == 2)
        assert len(media) == 2
        assert media[1][0] == media[0][0] and media[1][1] == 0 and media[1][-1] is False
        assert workspace.playbackContext["description"] == ""
        # A cached frame cannot bypass the current original or a closed library.
        original = Path(media[0][0])
        missing = original.with_suffix(".missing")
        original.rename(missing)
        try:
            assert provider.requestImage(previews[0], QSize(), QSize()).isNull()
            assert home_picture("hero", 100).isNull()
            assert home_picture("shelf", 100).isNull()
        finally:
            missing.rename(original)
        workspace.rememberPlayback(series_id, hit["videoID"], 750, 2000)
    finally:
        workspace.shutdown()

    assert provider.requestImage(previews[0], QSize(), QSize()).isNull()
    workspace = Workspace(tmp_path / "library", credentials=credentials)
    initial = []
    workspace.readyChanged.connect(lambda: initial.append((len(workspace.series), len(workspace.jobs))))
    try:
        workspace.start()
        wait(lambda: workspace.ready)
        assert initial == [(1, 1)]
        assert workspace.modelConfig["mode"] == "managed"
        assert workspace.importModelReady and workspace.searchModelReady
        assert workspace.recentPlayback[series_id] == {"videoID": hit["videoID"], "positionMs": 750, "durationMs": 2000}
        workspace.ensureSeriesLoaded(series_id)
        wait(lambda: series_id in workspace.videosBySeries)
        assert workspace.selectedSeries == ""
        workspace.rememberPlayback(series_id, hit["videoID"], 2000, 2000)
        assert workspace.recentPlayback[series_id]["positionMs"] == 2000
    finally:
        workspace.shutdown()


def test_highlight_keeps_media_and_query_text_literal():
    text = "<img src='file:///private'> C++ 红旗 & 台词"
    result = _highlight_description(text, "红旗 C++ [.*]")
    assert "<img" not in result and "&lt;img" in result and "&amp;" in result
    assert "<b>C++</b>" in result and "<b>红旗</b>" in result


def test_model_operations_use_separate_readiness_and_resume_only_the_requested_call(tmp_path, monkeypatch):
    QCoreApplication.instance() or QCoreApplication([])
    workspace = Workspace(tmp_path)
    requests = []
    prompts = []
    resumed = []
    workspace.settingsNeeded.connect(prompts.append)
    workspace.deferredActionResumed.connect(lambda: resumed.append(True))
    workspace._set("ready", True)
    workspace._set("series", [{"id": "series", "title": "系列"}])
    workspace._set("jobs", [{"id": "older", "canRetry": True, "retryReady": False}])
    workspace._update_models({"importReady": False, "searchReady": True})

    def request(action, **args):
        requests.append((action, args))
        if action == "search":
            return {"hits": [], "videos": [], "partial": False}
        return None

    monkeypatch.setattr("desktop.qt_controller._request", request)
    monkeypatch.setattr(workspace, "_submit", lambda run, done, **_: done(run(), ""))
    try:
        workspace.search("画面", False, "series")
        assert requests[-1][0] == "search" and not prompts
        workspace.importFiles([QUrl.fromLocalFile(str(tmp_path / "test.mp4"))], "series")
        assert prompts == ["import"]
        workspace.cancelDeferredImport()
        workspace._update_models({"importReady": True, "searchReady": False})
        workspace.search("台词", True, "series")
        assert requests[-1][1]["keywords"] is True
        workspace.search("画面", False, "series")
        assert prompts[-1] == "search"
        workspace._update_models({"importReady": False, "searchReady": True})
        workspace._resume_model_action()
        assert resumed == [True] and requests[-1] == ("search", {"query": "画面", "seriesIDs": ["series"], "keywords": False})
        workspace.retryTask("older")
        assert prompts[-1] == "retry"
        workspace._resume_model_action()
        assert len(resumed) == 1
        workspace._set("jobs", [{"id": "older", "canRetry": True, "retryReady": True}])
        retried = []
        monkeypatch.setattr(workspace, "_update_job", lambda job, action: retried.append((job, action)))
        workspace._resume_model_action()
        assert retried == [("older", "retry")] and len(resumed) == 2
    finally:
        workspace.shutdown()


def test_model_settings_scope_vault_writes_and_restore_after_failure(tmp_path, monkeypatch):
    app = QCoreApplication.instance() or QCoreApplication([])
    vault = MemoryCredentials({"old-service": "old-token"})
    workspace = Workspace(tmp_path, credentials=vault)
    workspace._set("ready", True)
    workspace._update_models({"revision": "before", "importReady": True, "searchReady": True})
    attempts = []
    allow = False

    def dispatch(action, **args):
        if action == "prepareModelSettings":
            assert args["revision"] == "before"
            return {"credentialNames": ["new-service"], "credentialName": "new-service"}
        assert action == "saveModelSettings"
        attempts.append(args["keys"])
        if not allow:
            raise RuntimeError("模型配置未应用")
        return {"revision": "after", "importReady": True, "searchReady": True}

    def wait():
        deadline = monotonic() + 5
        while workspace.savingKeys and monotonic() < deadline:
            app.processEvents()
            sleep(.005)
        assert not workspace.savingKeys

    monkeypatch.setattr("desktop.qt_controller._request", dispatch)
    monkeypatch.setattr(workspace, "refresh", lambda: None)
    try:
        workspace.saveModelConfig({"protocol": "openai_chat"}, {"connection": "new-token"})
        wait()
        assert vault == {"old-service": "old-token"} and workspace.modelConfig["revision"] == "before"
        vault["new-service"] = "prior-for-this-service"
        workspace.saveModelConfig({"protocol": "openai_chat"}, {"connection": "replacement"})
        wait()
        assert vault["new-service"] == "prior-for-this-service"
        allow = True
        workspace.clearError()
        workspace.saveModelConfig({"protocol": "openai_chat"}, {"connection": "new-token"})
        wait()
        assert not workspace.error and workspace.modelConfig["revision"] == "after"
        assert attempts == [{"new-service": token} for token in ["new-token", "replacement", "new-token"]]
        assert vault == {"old-service": "old-token", "new-service": "new-token"}
    finally:
        workspace.shutdown()


def test_loopback_openai_profile_can_save_without_a_credential(tmp_path, monkeypatch):
    app = QCoreApplication.instance() or QCoreApplication([])
    monkeypatch.setattr("server.desktop.app.enforce_single_process", lambda: None)
    monkeypatch.delenv("ARK_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    vault = MemoryCredentials()
    workspace = Workspace(tmp_path / "library", credentials=vault)

    def wait(predicate):
        deadline = monotonic() + 20
        while not predicate():
            app.processEvents()
            assert not workspace.error, workspace.error
            assert monotonic() < deadline
            sleep(.01)

    try:
        workspace.start()
        wait(lambda: workspace.ready)
        workspace.saveModelConfig({
            "name": "本地测试", "protocol": "openai_chat", "baseUrl": "http://127.0.0.1:23491/v1",
            "auth": "none", "vision": "local-vision", "transcription": "local-asr",
            "embedding": "local-embedding", "dimensions": 256.0, "rerank": "", "encoderModel": "",
            "revision": workspace.modelConfig["revision"],
        }, {})
        wait(lambda: not workspace.savingKeys)
        assert not workspace.error and workspace.modelConfig["mode"] == "managed"
        assert workspace.importModelReady and workspace.searchModelReady
        assert workspace.modelConfig["form"]["baseUrl"] == "http://127.0.0.1:23491/v1"
        assert workspace.modelConfig["form"]["dimensions"] == 256 and not vault
    finally:
        workspace.shutdown()


def test_switching_managed_endpoints_and_updating_custom_credentials(tmp_path, monkeypatch):
    import yaml
    from server.desktop.model_settings import EXAMPLE

    app = QCoreApplication.instance() or QCoreApplication([])
    monkeypatch.setattr("server.desktop.app.enforce_single_process", lambda: None)
    monkeypatch.delenv("ARK_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    vault = MemoryCredentials()
    workspace = Workspace(tmp_path / "managed", credentials=vault)

    def wait(test):
        deadline = monotonic() + 10
        while not test():
            assert monotonic() < deadline, workspace.error
            app.processEvents()
            sleep(.005)

    try:
        workspace.start()
        wait(lambda: workspace.ready)
        form = {"name": "显式连接", "protocol": "openai_chat", "auth": "key",
                "baseUrl": "http://127.0.0.1:23491/v1", "vision": "vision", "transcription": "asr",
                "embedding": "text-embedding", "dimensions": 256, "rerank": "", "encoderModel": ""}
        workspace.saveModelConfig(form, {"connection": "endpoint-a-private"})
        wait(lambda: not workspace.savingKeys)
        assert not workspace.error
        first_slot, = vault
        new_form = dict(form, baseUrl="http://127.0.0.1:23492/v1")
        workspace.saveModelConfig(new_form, {})
        wait(lambda: not workspace.savingKeys)
        assert workspace.error and workspace.modelConfig["form"]["baseUrl"] == form["baseUrl"]
        assert vault == {first_slot: "endpoint-a-private"}
        workspace.clearError()
        workspace.saveModelConfig(new_form, {"connection": "endpoint-b-private"})
        wait(lambda: not workspace.savingKeys)
        assert not workspace.error and len(vault) == 2
        target = next(row for row in workspace.modelConfig["connections"] if row["baseUrl"] == new_form["baseUrl"])
        second_slot = target["credentialName"]
        assert second_slot != first_slot and vault[second_slot] == "endpoint-b-private"
        assert all(name not in os.environ for name in vault)
    finally:
        workspace.shutdown()

    root = tmp_path / "operator"
    root.mkdir()
    document = yaml.safe_load(EXAMPLE.read_text())
    document["model_sources"]["deepseek_api"]["base_url"] = "https://operator.example/v1"
    original = "# operator profile\n" + yaml.safe_dump(document, allow_unicode=True)
    path = root / "models.yaml"
    path.write_text(original)
    vault["DEEPSEEK_API_KEY"] = "legacy-official-endpoint-only"
    workspace = Workspace(root, credentials=vault)
    try:
        workspace.start()
        wait(lambda: workspace.ready)
        config = workspace.modelConfig
        assert config["mode"] == "custom"
        target = next(item for item in config["connections"] if item["name"] == "deepseek_api")
        slot = target["credentialName"]
        assert slot != "DEEPSEEK_API_KEY" and not config["credentials"][slot]
        workspace.saveModelConfig({"retainConfig": True, "revision": config["revision"]},
                                  {slot: "custom-endpoint-only"})
        wait(lambda: not workspace.savingKeys)
        assert not workspace.error and workspace.modelConfig["credentials"][slot]
        assert vault[slot] == "custom-endpoint-only" and path.read_text() == original
        assert vault["DEEPSEEK_API_KEY"] == "legacy-official-endpoint-only"
    finally:
        workspace.shutdown()


def test_watch_discards_old_video_completions_and_restores_conversation(tmp_path, monkeypatch):
    from threading import Event
    from desktop import qt_controller

    app = QCoreApplication.instance() or QCoreApplication([])
    workspace = Workspace(tmp_path)
    workspace._set("ready", True)
    entered, release = Event(), Event()
    calls = []

    def request(action, **args):
        if action == "openVideo":
            return dict(video_id=args["videoID"], series_id="series", start_ms=0, end_ms=5000,
                        duration_ms=5000, description="", path="/fixture")
        assert action == "askWatch"
        calls.append(args)
        if args["question"] == "等待":
            entered.set()
            assert release.wait(5)
        if args["question"] == "失败":
            raise RuntimeError("模型服务拒绝了请求")
        return {"text": "收到" + args["question"], "positionMs": args["positionMs"]}

    def wait(predicate):
        timeout = monotonic() + 5
        while not predicate():
            app.processEvents()
            assert monotonic() < timeout
            sleep(0.005)

    monkeypatch.setattr(qt_controller, "_request", request)
    try:
        workspace.openVideo("one", "一")
        wait(lambda: workspace.playbackContext.get("videoID") == "one")
        settings = []
        workspace.settingsNeeded.connect(settings.append)
        assert not workspace.askWatch("草稿", 500) and settings == ["watch"]
        assert not workspace.watchMessages and not calls
        workspace._set("watchModelReady", True)
        assert workspace.askWatch("第一问", 500)
        wait(lambda: not workspace.watchBusy)
        first_turn = workspace.watchMessages
        assert [t["role"] for t in first_turn] == ["user", "assistant"]
        assert workspace.askWatch("等待", 700)
        wait(entered.is_set)
        workspace.leaveWatch()
        workspace.openVideo("two", "二")
        wait(lambda: workspace.playbackContext.get("videoID") == "two")
        assert not workspace.watchMessages and not workspace.watchBusy
        release.set()
        assert workspace.askWatch("失败", 800)
        wait(lambda: bool(workspace.watchError))
        assert workspace.watchError == "模型服务拒绝了请求" and not workspace.error
        assert not calls[-1]["history"]
        assert all(turn["text"] != "收到等待" for turn in workspace.watchMessages)
        workspace.openVideo("one", "一")
        wait(lambda: workspace.playbackContext.get("videoID") == "one")
        assert workspace.watchMessages == first_turn and not workspace.watchError
        assert workspace.askWatch("继续", 1000)
        wait(lambda: not workspace.watchBusy)
        assert calls[-1]["history"] == first_turn
        assert workspace.watchMessages[-1]["text"] == "收到继续"
    finally:
        release.set()
        workspace.shutdown()
