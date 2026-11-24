"""The latest reply owns one Kino across streamed row replacements and thread switches."""
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]


def test_native_chat_perch(tmp_path):
    pytest.importorskip("PySide6")
    env = {**os.environ, "QT_QPA_PLATFORM": "offscreen", "QT_QUICK_BACKEND": "software",
           "QML_DISABLE_DISK_CACHE": "1", "XDG_CONFIG_HOME": str(tmp_path)}
    subprocess.run([sys.executable, "-B", __file__, str(tmp_path)], env=env, cwd=ROOT, check=True, timeout=25)


def check_perches(root):
    from time import monotonic, sleep

    from PySide6.QtCore import QObject, Property, QSettings, QUrl
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtQuick import QQuickView
    from PySide6.QtQuickControls2 import QQuickStyle

    from desktop.qt_controller import Workspace

    class MotionPreferences(QObject):
        inputIdle = Property(bool, lambda _: False, constant=True)
        reducedMotion = Property(bool, lambda _: False, constant=True)

    app = QGuiApplication([])
    app.setOrganizationName("SeekinoTests")
    app.setApplicationName("ChatPerch")
    QSettings.setDefaultFormat(QSettings.IniFormat)
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, str(root))
    QQuickStyle.setStyle("Basic")
    view = QQuickView()
    prefs = MotionPreferences()
    workspace = Workspace(root)
    view.rootContext().setContextProperty("desktopPlatform", prefs)
    view.rootContext().setContextProperty("workspace", workspace)
    view.setResizeMode(QQuickView.SizeRootObjectToView)
    view.setSource(QUrl.fromLocalFile(str(ROOT / "desktop/ui/WatchCompanion.qml")))
    assert view.rootObject(), [e.toString() for e in view.errors()]
    panel = view.rootObject()
    panel.setProperty("libraryMode", True)
    panel.setProperty("opened", True)
    view.resize(660, 520)
    view.show()
    view.requestActivate()

    def until(condition):
        deadline = monotonic() + 4
        while not condition():
            app.processEvents()
            assert monotonic() < deadline
            sleep(.008)

    try:
        welcome = panel.findChild(QObject, "watchWelcomeKino")
        kino = panel.findChild(QObject, "watchReplyKino")
        turns = panel.findChild(QObject, "watchChatMessages")
        until(lambda: view.isActive() and welcome.isVisible())
        status = panel.findChild(QObject, "watchStatusKino")
        workspace._set("watchBusy", True)
        until(lambda: status.property("item") is not None)
        status_kino = status.property("item")
        assert status_kino.property("pose") == "loading" and status_kino.opacity() == 0
        until(lambda: status_kino.opacity() >= 0.99)
        workspace._set("watchBusy", False)
        workspace._set("watchError", "Unavailable")
        until(lambda: status.property("item") is not None)
        assert status.property("item").property("pose") == "oops"
        workspace._set("watchError", "")
        until(lambda: welcome.isVisible() and not status.isVisible())
        rows = []
        workspace._set("watchThreadID", "one")
        for failed in (False, True):
            workspace.watchTurnStarted.emit()
            workspace._set("watchPhase", "QUEUED")
            rows += [{"role": "user", "text": "?", "positionMs": 5},
                     {"role": "assistant", "text": "", "positionMs": 5}]
            workspace._set("watchMessages", list(rows))
            until(lambda: kino.isVisible() and kino.property("flightTime") > 0)
            assert not welcome.isVisible() and turns.property("currentIndex") == len(rows) - 1
            workspace._set("watchPhase", "RESPONDING")
            rows[-1] = {**rows[-1], "text": "partial", "streaming": True}
            workspace._set("watchMessages", list(rows))
            until(lambda: kino.property("pose") == "loading")
            rows[-1] = {"role": "assistant", "text": "final", "positionMs": 5, "failed": failed}
            workspace._set("watchMessages", list(rows))
            workspace._set("watchPhase", "")
            workspace.watchTurnFinished.emit(failed)
            assert kino.property("pose") == ("oops" if failed else "happy")
            if not failed:
                until(lambda: kino.property("pose") == "sleep")
        # Switch while the failed turn is still reacting. A restored thread has no live farewell.
        workspace._set("watchThreadID", "two")
        workspace._set("watchMessages", rows[:2])
        until(lambda: turns.property("currentIndex") == 1)
        assert kino.property("pose") == "sleep"
    finally:
        view.close()
        workspace.shutdown()


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    check_perches(Path(sys.argv[1]))
