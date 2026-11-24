"""Exercise the watch launcher with window-level input, including canceled pointer grabs."""
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]


def test_watch_kino_pointer_sequence(tmp_path):
    pytest.importorskip("PySide6")
    env = {**os.environ, "QT_QPA_PLATFORM": "offscreen", "QML_DISABLE_DISK_CACHE": "1"}
    subprocess.run([sys.executable, "-B", __file__, str(tmp_path)], env=env, cwd=ROOT,
                   check=True, timeout=30)


def exercise(settings_dir):
    from PySide6.QtCore import QEvent, QEventLoop, QObject, QPointF, QSettings, Qt, QTimer, QUrl
    from PySide6.QtGui import QGuiApplication, QKeyEvent, QMouseEvent
    from PySide6.QtQml import QQmlComponent, QQmlEngine, QQmlPropertyMap
    from PySide6.QtQuick import QQuickWindow

    app = QGuiApplication(["kino-pointer-test"])
    app.setOrganizationName("SeekinoTests")
    app.setApplicationName("WatchPointer")
    QSettings.setDefaultFormat(QSettings.IniFormat)
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, settings_dir)
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
    signal watchTurnStarted()
    signal watchTurnFinished(bool failed)
}""", QUrl())
    workspace = fixture.create()
    engine.rootContext().setContextProperty("desktopPlatform", platform)
    engine.rootContext().setContextProperty("workspace", workspace)
    component = QQmlComponent(engine, QUrl.fromLocalFile(str(ROOT / "desktop/ui/WatchCompanion.qml")))
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
    header = named("watchPanelMove")
    header_start = header.mapToScene(QPointF(35, 16))
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
    exercise(sys.argv[1])
