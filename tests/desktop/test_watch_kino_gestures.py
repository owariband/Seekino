"""Exercise the watch launcher with window-level input, including canceled pointer grabs."""
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("scene", ["companion", "fullscreen"])
def test_watch_kino_pointer_sequence(tmp_path, scene):
    pytest.importorskip("PySide6")
    if scene == "fullscreen":
        pytest.importorskip("av")
    env = {**os.environ, "QT_QPA_PLATFORM": "offscreen", "QML_DISABLE_DISK_CACHE": "1"}
    subprocess.run([sys.executable, "-B", __file__, str(tmp_path), scene], env=env, cwd=ROOT,
                   check=True, timeout=30)


def exercise(settings_dir, scene):
    from PySide6.QtCore import QEvent, QEventLoop, QObject, QPointF, QSettings, Qt, QTimer, QUrl
    from PySide6.QtGui import QGuiApplication, QKeyEvent, QMouseEvent
    from PySide6.QtQml import QQmlComponent, QQmlEngine, QQmlPropertyMap
    from PySide6.QtQuick import QQuickWindow

    app = QGuiApplication(["kino-pointer-test"])
    if scene == "fullscreen":
        from desktop.qt_player import register_player
        register_player()
    app.setOrganizationName("SeekinoTests")
    app.setApplicationName("WatchPointer")
    QSettings.setDefaultFormat(QSettings.IniFormat)
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, settings_dir)
    # Existing releases store whole seconds and the chosen sidebar width under these keys.
    previous = QSettings()
    previous.setValue("workspace/kinoFadeSeconds", 11)
    previous.setValue("watch-companion/dockWidth", 390)
    previous.sync()
    engine = QQmlEngine()
    platform = QQmlPropertyMap()
    platform.insert("reducedMotion", False)
    platform.insert("inputIdle", False)
    fixture = QQmlComponent(engine)
    fixture.setData(b"""import QtQuick
QtObject {
    property var watchMessages: []
    property var watchThreads: []
    property bool watchModelReady: true
    property bool watchBusy: false
    property string watchThreadID: ""
    property string watchStreamingText: ""
    property string watchPhase: ""
    property string watchError: ""
    property var playbackContext: ({})
    property var videosBySeries: ({})
    property var episodeFind: ({})
    signal watchTurnStarted()
    signal watchTurnFinished(bool failed)
}""", QUrl())
    workspace = fixture.create()
    engine.rootContext().setContextProperty("desktopPlatform", platform)
    engine.rootContext().setContextProperty("workspace", workspace)
    qml = "WatchPane.qml" if scene == "fullscreen" else "WatchCompanion.qml"
    component = QQmlComponent(engine, QUrl.fromLocalFile(str(ROOT / "desktop/ui" / qml)))
    assert component.isReady(), [err.toString() for err in component.errors()]
    win = QQuickWindow()
    win.resize(1024, 720)
    companion = component.createWithInitialProperties({"width": 1024, "height": 720})
    companion.setParentItem(win.contentItem())
    win.show()
    win.requestActivate()

    def wait(ms):
        loop = QEventLoop()
        QTimer.singleShot(ms, loop.quit)
        loop.exec()

    def pointer(event_type, point, held=False):
        press = event_type in (QEvent.MouseButtonPress, QEvent.MouseButtonRelease)
        event = QMouseEvent(event_type, point, win.mapToGlobal(point.toPoint()).toPointF(),
                            Qt.LeftButton if press else Qt.NoButton,
                            Qt.LeftButton if held else Qt.NoButton, Qt.NoModifier)
        QGuiApplication.sendEvent(win, event)
        app.processEvents()

    def click(point):
        pointer(QEvent.MouseButtonPress, point, True)
        pointer(QEvent.MouseButtonRelease, point)

    def center(item):
        return item.mapToScene(QPointF(item.width() / 2, item.height() / 2))

    def named(name):
        obj = companion.findChild(QObject, name)
        assert obj is not None, name
        return obj

    wait(400)
    if scene == "fullscreen":
        click(center(named("fullscreenButton")))
        wait(250)
        assert companion.property("cinema") and win.visibility() == QQuickWindow.FullScreen
        stage = named("videoStage")
        assert stage.width() == win.width() and stage.height() == win.height()
        click(center(named("kinoEntry")))
        wait(220)
        click(center(named("watchOptionsButton")))
        wait(160)
        assert named("watchOptionsPopup").property("opened")
        # A popup behind the fullscreen video still reports opened. Click through
        # the actual window and verify its control receives the pointer event.
        fade = named("watchAutoFade")
        assert fade.property("checked")
        click(center(fade))
        assert not fade.property("checked") and not named("watchFadeSeconds").isEnabled()
        app.sendEvent(win, QKeyEvent(QEvent.KeyPress, Qt.Key_Escape, Qt.NoModifier))
        wait(150)
        assert not named("watchOptionsPopup").property("opened") and companion.property("chatOpen")
        click(center(named("watchChatClose")))
        wait(170)
        click(center(named("fullscreenButton")))
        assert not companion.property("cinema")
        win.close()
        return
    entry = named("kinoEntry")
    sprite = named("watchEntrySprite")
    grip = named("watchEntryDrag")
    panel = named("watchChatPanel")
    start = center(entry)
    pointer(QEvent.MouseMove, start)
    assert sprite.property("mood") == "look"
    pointer(QEvent.MouseButtonPress, start, True)
    pointer(QEvent.MouseMove, start + QPointF(-2, -2), True)
    assert not grip.property("moving")
    pointer(QEvent.MouseMove, start + QPointF(-8, 0), True)
    assert grip.property("moving") and sprite.property("mood") == "dashLeft"
    pointer(QEvent.MouseMove, start + QPointF(-30, -8), True)
    pointer(QEvent.MouseMove, start + QPointF(-26, -7), True)
    assert sprite.property("mood") == "dashLeft"  # 4 px reversal is hand tremor
    pointer(QEvent.MouseMove, start + QPointF(-24, -8), True)
    assert sprite.property("mood") == "dashRight"  # 6 px is a turn
    drop = QPointF(320, 210)
    pointer(QEvent.MouseMove, drop, True)
    assert center(entry) == drop, (center(entry), drop)
    pointer(QEvent.MouseButtonRelease, drop)
    assert not companion.property("opened")
    assert sprite.property("pose") == "jump"
    wait(40)
    assert 24 < entry.x() < drop.x() - 32, entry.x()
    wait(430)
    assert entry.x() == 24 and center(entry).y() == drop.y(), center(entry)
    settled = center(entry)

    pointer(QEvent.MouseButtonPress, settled, True)
    pointer(QEvent.MouseMove, QPointF(915, 300), True)
    assert grip.property("moving")
    grip.ungrabMouse()
    wait(400)
    assert not grip.property("moving") and center(entry) == settled
    assert not companion.property("opened")

    # Reduced motion follows the pointer, then docks immediately at the clamped edge.
    platform.insert("reducedMotion", True)
    pointer(QEvent.MouseButtonPress, settled, True)
    pointer(QEvent.MouseMove, QPointF(910, 719), True)
    pointer(QEvent.MouseButtonRelease, QPointF(910, 719))
    assert entry.x() == 1024 - 24 - 64 and entry.y() == 720 - 16 - 64
    click(center(entry))
    wait(220)
    assert companion.property("opened")
    # Like a native split view with its navigation sidebar open: this 778 px
    # player still has a 960 px window, which Web treats as a desktop viewport.
    win.resize(960, 720)
    companion.setWidth(778)
    assert companion.property("floating")
    popup = named("watchOptionsPopup")
    click(center(named("watchOptionsButton")))
    wait(100)
    assert popup.property("opened")
    delay = named("watchFadeSeconds")
    assert delay.property("text") == "11"
    click(center(delay))
    delay.selectAll()
    for key, text in ((Qt.Key_1, "1"), (Qt.Key_Period, "."), (Qt.Key_2, "2")):
        app.sendEvent(win, QKeyEvent(QEvent.KeyPress, key, Qt.NoModifier, text))
    app.sendEvent(win, QKeyEvent(QEvent.KeyPress, Qt.Key_Return, Qt.NoModifier))
    assert delay.property("text") == "1.2", delay.property("text")
    app.sendEvent(win, QKeyEvent(QEvent.KeyPress, Qt.Key_Escape, Qt.NoModifier))
    wait(100)
    assert not popup.property("opened") and companion.property("opened")
    outside = QPointF(100, 100)
    pointer(QEvent.MouseMove, outside)
    wait(400)
    assert panel.opacity() == 1
    wait(950)
    assert panel.opacity() == .12
    pointer(QEvent.MouseMove, center(panel))
    assert panel.opacity() == 1
    click(center(named("watchOptionsButton")))
    fade = named("watchAutoFade")
    click(center(fade))
    assert not fade.property("checked") and not delay.isEnabled()
    app.sendEvent(win, QKeyEvent(QEvent.KeyPress, Qt.Key_Escape, Qt.NoModifier))
    pointer(QEvent.MouseMove, outside)
    wait(1350)
    assert panel.opacity() == 1
    click(center(named("watchOptionsButton")))
    click(center(fade))
    click(center(named("watchChooseDocked")))
    assert not companion.property("floating") and named("watchDockResize").isVisible()
    assert companion.property("reservedWidth") == panel.width()
    click(center(named("watchChooseFloating")))
    assert companion.property("floating") and companion.property("reservedWidth") == 0
    app.sendEvent(win, QKeyEvent(QEvent.KeyPress, Qt.Key_Escape, Qt.NoModifier))
    wait(100)
    # Editing a draft or waiting for the assistant keeps it readable away from the pointer.
    question = named("watchChatQuestion")
    click(center(question))
    app.sendEvent(win, QKeyEvent(QEvent.KeyPress, Qt.Key_A, Qt.NoModifier, "a"))
    pointer(QEvent.MouseMove, outside)
    wait(1350)
    assert question.property("text") == "a" and panel.opacity() == 1
    question.clear()
    workspace.setProperty("watchBusy", True)
    wait(1350)
    assert panel.opacity() == 1
    workspace.setProperty("watchBusy", False)
    win.resize(1024, 720)
    companion.setWidth(1024)
    header = named("watchPanelMove")
    header_start = header.mapToScene(QPointF(78, 21))
    before = panel.position()
    pointer(QEvent.MouseButtonPress, header_start, True)
    pointer(QEvent.MouseMove, header_start + QPointF(120, 35), True)
    pointer(QEvent.MouseButtonRelease, header_start + QPointF(120, 35))
    assert panel.position() == QPointF(min(1024 - panel.width() - 8, before.x() + 120),
                                     min(720 - panel.height() - 8, before.y() + 35)), (before, panel.position())
    companion.setWidth(900)
    assert panel.x() == 900 - 24 - panel.width()
    click(center(named("watchChatDock")))
    handle = named("watchDockResize")
    assert handle.isVisible()
    dock_width = panel.width()
    assert dock_width == 390
    thumb = center(handle)
    pointer(QEvent.MouseButtonPress, thumb, True)
    pointer(QEvent.MouseMove, thumb - QPointF(26, 0), True)
    pointer(QEvent.MouseButtonRelease, thumb - QPointF(26, 0))
    assert panel.width() == dock_width + 26
    handle.forceActiveFocus()
    QGuiApplication.sendEvent(win, QKeyEvent(QEvent.KeyPress, Qt.Key_Right, Qt.NoModifier))
    assert panel.width() == dock_width + 2
    click(center(named("watchChatClose")))
    wait(220)
    assert not companion.property("opened")
    click(center(entry))
    assert companion.property("opened")  # the first tap after a drag still opens chat
    companion.setParentItem(None)
    companion.deleteLater()
    wait(100)
    companion = component.createWithInitialProperties({"width": 900, "height": 720})
    companion.setParentItem(win.contentItem())
    restored = named("kinoEntry")
    assert restored.x() == 900 - 24 - 64 and restored.y() == 720 - 16 - 64
    click(center(restored))
    assert named("watchDockResize").isVisible() and named("watchChatPanel").width() == dock_width + 2
    companion.setParentItem(None)
    win.close()


if __name__ == "__main__":
    # pytest's configured pythonpath only applies to the parent process.
    sys.path.insert(0, str(ROOT))
    exercise(sys.argv[1], sys.argv[2])
