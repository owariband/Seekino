"""Exercise the macOS File menu with an isolated AppKit Command-W event."""
import os
from pathlib import Path
import subprocess
import sys
from time import monotonic


def test_native_close_window(tmp_path):
    import pytest
    if sys.platform != "darwin" or os.environ.get("QT_QPA_PLATFORM", "cocoa") != "cocoa":
        pytest.skip("requires the macOS Cocoa window system")
    pytest.importorskip("PySide6")
    helper = Path(__file__).with_name("cocoa_close_key.m")
    subprocess.run(["clang", "-dynamiclib", "-fobjc-arc", "-framework", "AppKit", helper,
                    "-o", tmp_path / "keys.dylib"], check=True)
    subprocess.run([sys.executable, "-B", __file__, str(tmp_path)], check=True, timeout=45,
                   env={**os.environ, "QT_QUICK_BACKEND": "software", "QML_DISABLE_DISK_CACHE": "1"})


def exercise(tmp):
    import ctypes
    from PySide6.QtCore import QEventLoop, QObject, QSettings, QTimer
    from PySide6.QtGui import QGuiApplication
    from desktop import main as entry, qt_controller
    from tests.desktop.test_workspace import MemoryCredentials

    keys = ctypes.CDLL(str(tmp / "keys.dylib"))
    keys.focusTestWindow.argtypes = keys.sendCommandW.argtypes = [ctypes.c_void_p]
    keys.focusTestWindow.restype = keys.sendCommandW.restype = None
    QSettings.setDefaultFormat(QSettings.IniFormat)
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, str(tmp))

    def wait_for(condition):
        deadline = monotonic() + 5
        while not condition():
            assert monotonic() < deadline, "Cocoa close action did not reach the expected window"
            loop = QEventLoop()
            QTimer.singleShot(25, loop.quit)
            loop.exec()

    def check():
        app = QGuiApplication.instance()
        app.setQuitOnLastWindowClosed(False)
        window = next(w for w in app.topLevelWindows() if w.objectName() == "workspaceWindow")
        window.openSettings()
        prefs = window.findChild(QObject, "preferences")
        wait_for(lambda: prefs.property("opened"))
        keys.focusTestWindow(window.winId())
        wait_for(window.isActive)
        keys.sendCommandW(window.winId())
        wait_for(lambda: not prefs.property("visible"))
        assert window.isVisible()

        help_window = window.findChild(QObject, "helpWindow")
        help_window.setOpacity(0)
        window.openHelp()
        keys.focusTestWindow(help_window.winId())
        wait_for(help_window.isActive)
        keys.sendCommandW(help_window.winId())
        wait_for(lambda: not help_window.isVisible())
        assert window.isVisible()

        keys.focusTestWindow(window.winId())
        wait_for(window.isActive)
        keys.sendCommandW(window.winId())
        wait_for(lambda: not window.isVisible())
        return 0

    workspace = qt_controller.Workspace
    qt_controller.Workspace = lambda root: workspace(root, credentials=MemoryCredentials())
    class TestApplication(QGuiApplication):
        def setOrganizationName(self, _):
            super().setOrganizationName("SeekinoTests")

        def exec(self):
            errors = []

            def run():
                try:
                    check()
                except Exception as error:
                    errors.append(error)
                finally:
                    self.quit()

            QTimer.singleShot(250, run)
            result = super().exec()
            if errors:
                raise errors[0]
            return result

    unify_titlebar = entry._unify_titlebar

    def prepare_window(window):
        window.setOpacity(0)
        window.setPosition(-10000, -10000)
        unify_titlebar(window)

    entry._unify_titlebar = prepare_window
    entry.QGuiApplication = TestApplication
    sys.argv[:] = ["seekino-close-test", "--library", str(tmp / "library")]
    return entry.main()


if __name__ == "__main__":
    raise SystemExit(exercise(Path(sys.argv[1])))
