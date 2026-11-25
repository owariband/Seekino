"""Exercise the shared provider window using real Qt pointer and keyboard events."""
import os
from pathlib import Path
import subprocess
import sys
import time


def test_provider_preferences(tmp_path):
    import pytest

    pytest.importorskip("PySide6")
    env = {**os.environ, "QT_QPA_PLATFORM": "offscreen", "QT_QUICK_BACKEND": "software",
           "QML_DISABLE_DISK_CACHE": "1"}
    subprocess.run([sys.executable, "-B", __file__, str(tmp_path)], env=env, check=True, timeout=45)


def exercise(src, tmp):
    from contextlib import ExitStack
    import hashlib
    import io
    from PySide6.QtCore import QEvent, QEventLoop, QPointF, QSettings, Qt, QTimer, QUrl
    from PySide6.QtGui import QGuiApplication, QKeyEvent, QMouseEvent
    from PySide6.QtQml import QQmlApplicationEngine, QQmlComponent
    from PySide6.QtQuickControls2 import QQuickStyle
    from desktop.locale import DesktopLocale
    from desktop.embedding_download import EmbeddingDownload
    from desktop.main import PlatformPreferences, configure_interface_font
    from desktop.posters import PosterProvider
    from desktop.qt_controller import Workspace
    from desktop.qt_player import register_player
    from gateway.local import qwen_assets
    from unittest.mock import patch
    import httpx

    class Credentials:
        def __init__(self):
            self.keys = {}

        def read(self, name):
            return self.keys.get(name, "")

        def write(self, name, value):
            self.keys[name] = value

        def delete(self, name):
            self.keys.pop(name, None)

    QQuickStyle.setStyle("Basic")
    app = QGuiApplication(["provider-settings-test"])
    app.setOrganizationName("SeekinoTests")
    app.setApplicationName("ProviderPreferences")
    QSettings.setDefaultFormat(QSettings.IniFormat)
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, str(tmp))
    configure_interface_font(app)
    register_player()
    engine = QQmlApplicationEngine()
    engine.addImageProvider("posters", PosterProvider())
    platform = PlatformPreferences(app)
    locale = DesktopLocale(app)
    workspace = Workspace(tmp / "library", credentials=Credentials())
    model_file = tmp / "models" / qwen_assets.MODEL_FILENAME
    asset_patches = ExitStack()
    asset_patches.enter_context(patch.object(qwen_assets, "model_path", return_value=model_file))
    downloader = EmbeddingDownload()
    engine.rootContext().setContextProperty("embeddingDownload", downloader)
    for key, obj in (("workspace", workspace), ("desktopPlatform", platform), ("desktopLocale", locale)):
        engine.rootContext().setContextProperty(key, obj)
    component = QQmlComponent(engine, QUrl.fromLocalFile(str(src / "desktop/ui/Main.qml")))
    assert component.isReady(), [e.toString() for e in component.errors()]
    window = component.create()
    window.resize(1080, 700)
    window.show()
    window.requestActivate()

    def wait(ms=40):
        loop = QEventLoop()
        QTimer.singleShot(ms, loop.quit)
        loop.exec()

    def until(pred, label):
        deadline = time.monotonic() + 8
        while not pred():
            assert time.monotonic() < deadline, (label, workspace.error)
            wait()

    def walk(node):
        yield node
        for child in node.childItems():
            yield from walk(child)

    def item(name):
        return next((i for i in walk(window.contentItem()) if i.objectName() == name and i.isVisible()), None)

    def click(name):
        target = item(name) if isinstance(name, str) else name
        assert target and target.isEnabled(), name
        scroll = item("settingsScroll")
        if scroll and scroll.isAncestorOf(target):
            top = target.mapToItem(scroll, QPointF()).y()
            if top < 0 or top + target.height() > scroll.height():
                extent = max(0, scroll.property("contentHeight") - scroll.height())
                scroll.setProperty("contentY", min(max(0, scroll.property("contentY") + top - 12), extent))
                wait()
        pt = target.mapToScene(QPointF(target.width() / 2, target.height() / 2))
        assert 0 <= pt.x() <= window.width() and 0 <= pt.y() <= window.height(), (name, pt)
        for event, held in ((QEvent.MouseButtonPress, Qt.LeftButton), (QEvent.MouseButtonRelease, Qt.NoButton)):
            app.sendEvent(window, QMouseEvent(event, pt, pt, Qt.LeftButton, held, Qt.NoModifier))
        wait(65)

    def key(value, text="", modifier=Qt.NoModifier):
        for event in (QEvent.KeyPress, QEvent.KeyRelease):
            app.sendEvent(window, QKeyEvent(event, value, modifier, text))

    def type_in(name, text):
        click(name)
        key(Qt.Key_A, "a", Qt.ControlModifier)
        for c in text:
            key(ord(c.upper()), c)
        assert item(name).property("text") == text

    def shot(name):
        if directory := os.environ.get("QA_SHOTS"):
            wait(180)
            assert window.grabWindow().save(str(Path(directory) / (name + ".png")))

    def select(name, index):
        picker = item(name)
        click(name)
        if name == "providerType":
            shot("provider-menu")
        key(Qt.Key_Home)
        for _ in range(index):
            key(Qt.Key_Down)
        key(Qt.Key_Return)
        wait(150)
        assert picker.property("currentIndex") == index, name

    try:
        until(lambda: workspace.ready, "open fixture library")
        window.openSettings()
        until(lambda: item("addService"), "open unified settings")
        wait(160)
        click("settingsNav_roles")
        assert item("embeddingUseApi").property("chosen")
        click("embeddingUseLocal")
        assert item("localEmbeddingDownloadAction").isEnabled()
        assert not item("applyRole").isEnabled()
        click("settingsNav_services")
        click("addService")
        type_in("connectionName", "Studio A")
        select("providerType", 1)
        assert item("serviceUrl").property("readOnly")
        type_in("connectionKey", "demo-credential")
        shot("provider-ark")
        click("saveService")
        until(lambda: len(workspace.modelConfig["serviceProfiles"]) == 1, "save Ark")

        click("addService")
        type_in("connectionName", "Studio B")
        type_in("serviceUrl", "http://localhost:8765/v1")
        click("serviceLocalAuth")
        shot("provider-compatible")
        click("saveService")
        until(lambda: len(workspace.modelConfig["serviceProfiles"]) == 2, "save compatible endpoint")
        services = {p["name"]: p["id"] for p in workspace.modelConfig["serviceProfiles"]}
        compatible = services["Studio B"]
        type_in("serviceSearch", "Studio B")
        assert item("editService_" + compatible) and not item("editService_" + services["Studio A"])
        click("settingsNav_roles")
        select("roleService_vision", 1)
        select("roleService_transcription", 0)
        select("roleService_embedding", 1)
        def models(request):
            assert str(request.url) == "http://localhost:8765/v1/models"
            assert "authorization" not in request.headers
            return httpx.Response(200, json={"data": [{"id": "video-v1"}]})

        with patch("gateway.model_discovery.endpoint_http_client", return_value=httpx.Client(transport=httpx.MockTransport(models))):
            click("chooseModel_vision")
            def result_button():
                return next((i for i in walk(item("settingsScroll"))
                             if i.isVisible() and i.property("text") == "使用" and i.metaObject().indexOfSignal("clicked()") >= 0), None)
            until(result_button, "discover this role's models")
            shot("provider-discovery")
            click(result_button())
        assert item("visionModel").property("text") == "video-v1"
        for name, value in (("transcriptionModel", "asr-v1"),
                            ("embeddingModel", "embed-v1"), ("embeddingDimensions", "8")):
            type_in(name, value)
        shot("provider-roles")
        click("applyRole")
        until(lambda: workspace.modelConfig["parseRoleServices"].get("embedding") == compatible, "apply roles")
        # Only the downloaded bytes are fixtures. The dialog, controller, checksum and save are real.
        click("embeddingUseLocal")
        assert not item("embeddingModel") and not item("embeddingDimensions")
        assert not item("applyRole").isEnabled()
        click("embeddingUseApi")
        assert item("embeddingModel").property("text") == "embed-v1"
        click("embeddingUseLocal")
        payload = b"isolated model download fixture"
        asset_patches.enter_context(patch.object(qwen_assets, "MODEL_BYTES", len(payload)))
        asset_patches.enter_context(patch.object(qwen_assets, "MODEL_SHA256", hashlib.sha256(payload).hexdigest()))
        with patch("desktop.embedding_download.urlopen", return_value=io.BytesIO(b"truncated")):
            click("localEmbeddingDownloadAction")
            until(lambda: downloader.state == "error", "reject a truncated download")
        assert item("localEmbeddingDownloadAction").property("text") == "重试"
        assert not model_file.exists() and not item("applyRole").isEnabled()
        with patch("desktop.embedding_download.urlopen", return_value=io.BytesIO(payload)):
            click("localEmbeddingDownloadAction")
            until(lambda: downloader.state == "ready", "verify the retried download")
        assert model_file.read_bytes() == payload and not item("localEmbeddingDownloadAction")
        click("applyRole")
        until(lambda: workspace.modelConfig["form"].get("embeddingMode") == "local", "save local embedding")
        assert "embedding" not in workspace.modelConfig["parseRoleServices"]
        click("settingsDone")
        wait(180)
        window.openSettings()
        click("settingsNav_roles")
        assert item("embeddingUseLocal").property("chosen")
        assert "1024" in item("localEmbeddingStatus").property("text")
        assert item("visionModel").property("text") == "video-v1"
        assert item("transcriptionModel").property("text") == "asr-v1"
        click("embeddingUseApi")
        select("roleService_embedding", 1)
        type_in("embeddingModel", "embed-v1")
        type_in("embeddingDimensions", "8")
        click("applyRole")
        until(lambda: workspace.modelConfig["parseRoleServices"].get("embedding") == compatible, "restore API")
        click("watchRole")
        select("roleConnection", 2)
        type_in("watchModelId", "watch-v1")
        click("applyRole")
        until(lambda: workspace.modelConfig["watchSettings"].get("model") == "watch-v1", "apply watch")
        click("settingsDone")
        wait(180)

        window.resize(640, 500)
        window.openSettings()
        wait(220)
        assert item("settingsCategory")
        click("editService_" + compatible)
        type_in("serviceUrl", "http://localhost:8766/v1")
        click("serviceLocalAuth")
        click("saveService")
        until(lambda: any("watch" in p["pendingRoles"] for p in workspace.modelConfig["serviceProfiles"]), "pending change")
        select("settingsCategory", 1)
        assert item("roleService_vision").property("currentIndex") == item("roleService_embedding").property("currentIndex") == 1
        assert item("roleService_transcription").property("currentIndex") == 0
        assert item("applyRole").isEnabled()  # optional rerank can continue inheriting vision
        click("watchRole")
        assert item("watchModelId").property("text") == "watch-v1"
        assert any(i.isVisible() and "8765" in str(i.property("text")) for i in walk(item("settingsScroll")))
        shot("provider-pending-narrow")
        click("applyRole")
        effective = lambda: workspace.modelConfig["effectiveRoleServices"]
        until(lambda: ":8766/" in effective()["watch"]["baseUrl"], "apply watch endpoint")
        assert ":8765/" in effective()["embedding"]["baseUrl"]
        click("settingsDone")
        print("PASS: shared protocol editor, searchable registry, per-role and watch assignments, pending reapply, narrow window", flush=True)
    finally:
        downloader.shutdown()
        asset_patches.close()
        workspace.shutdown()
        window.close()


if __name__ == "__main__":
    # Bundle verification loads all Seekino and Qt modules from the .app.
    if bundle := os.environ.get("QA_APP"):
        root = Path(bundle) / "Contents/Resources"
        sys.path[:0] = [str(root), str(root / "site-packages")]
        import desktop.qt_controller
        from PySide6 import QtCore

        for path in (desktop.qt_controller.__file__, QtCore.__file__):
            assert Path(path).resolve().is_relative_to(root.parent), path
    else:
        root = Path(__file__).resolve().parents[2]
        sys.path.insert(0, str(root))
    exercise(root, Path(sys.argv[1]))
