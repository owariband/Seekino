"""Start the same desktop workspace in the embedded macOS or Windows process."""
import argparse
import ctypes
import os
from pathlib import Path
import sys

from server.desktop.host import enforce_single_process

if __name__ == "__main__":
    # Install before loading the GUI or model SDKs, including their OS capability probes.
    enforce_single_process()

if sys.platform == "win32":
    # Shiboken loads before PySide registers its own directory. Its C++ runtime
    # lives next to the Qt DLLs in the standalone bundle, including on clean PCs.
    from importlib.util import find_spec

    bindings = find_spec("PySide6")
    if bindings and bindings.origin:
        _qt_dll_directory = os.add_dll_directory(str(Path(bindings.origin).parent))


from PySide6 import QtCore
from PySide6.QtCore import QObject, Property, Signal, Qt, QUrl
from PySide6.QtGui import QFontDatabase, QFontInfo, QGuiApplication, QIcon
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuickControls2 import QQuickStyle


class PlatformPreferences(QObject):
    changed = Signal()
    inputIdleChanged = Signal()

    _activity_events = frozenset((
        QtCore.QEvent.Type.MouseButtonPress, QtCore.QEvent.Type.MouseMove,
        QtCore.QEvent.Type.KeyPress, QtCore.QEvent.Type.Wheel,
        QtCore.QEvent.Type.TouchBegin, QtCore.QEvent.Type.TouchUpdate,
        QtCore.QEvent.Type.TabletPress, QtCore.QEvent.Type.TabletMove,
        QtCore.QEvent.Type.WindowActivate,
    ))

    def __init__(self, parent):
        super().__init__(parent)
        self._read = _motion_preference_reader()
        self._reduced = self._read()
        parent.applicationStateChanged.connect(self.refresh)
        self._input_idle = False
        self._idle_timer = QtCore.QTimer(self)
        self._idle_timer.setSingleShot(True)
        self._idle_timer.setTimerType(Qt.PreciseTimer)
        self._idle_timer.setInterval(45_000)
        self._idle_timer.timeout.connect(self._doze)

    @Property(bool, notify=inputIdleChanged)
    def inputIdle(self):
        return self._input_idle

    def observe_window(self, window):
        window.installEventFilter(self)
        self._idle_timer.start()

    def eventFilter(self, watched, event):
        if event.type() in self._activity_events:
            self._idle_timer.start()
            if self._input_idle:
                self._input_idle = False
                self.inputIdleChanged.emit()
        return False

    def _doze(self):
        self._input_idle = True
        self.inputIdleChanged.emit()

    @Property(bool, notify=changed)
    def reducedMotion(self):
        return self._reduced

    def refresh(self, *_):
        value = self._read()
        if value != self._reduced:
            self._reduced = value
            self.changed.emit()


def configure_interface_font(app):
    font = app.font()
    system_family = QFontInfo(font).family()
    if sys.platform == "darwin":
        families = [system_family, "PingFang SC"]
    elif sys.platform == "win32":
        latin = next((family for family in ("Segoe UI Variable Text", "Segoe UI Variable", "Segoe UI")
                      if QFontDatabase.hasFamily(family)), system_family)
        families = [latin, "Microsoft YaHei UI"]
    else:
        return
    # Match both scripts at the requested weight in mixed Chinese/Latin labels.
    font.setFamilies(families)
    app.setFont(font)


def _cocoa_messages():
    objc = ctypes.CDLL("/usr/lib/libobjc.A.dylib")
    objc.objc_getClass.argtypes = [ctypes.c_char_p]
    objc.objc_getClass.restype = ctypes.c_void_p
    objc.sel_registerName.argtypes = [ctypes.c_char_p]
    objc.sel_registerName.restype = ctypes.c_void_p
    send = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)(("objc_msgSend", objc))
    return objc, send


def _unify_titlebar(window):
    window.setFlags(window.flags() | Qt.ExpandedClientAreaHint | Qt.NoTitleBarBackgroundHint)
    objc, send = _cocoa_messages()
    nswindow = send(int(window.winId()), objc.sel_registerName(b"window"))
    set_integer = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_long)(("objc_msgSend", objc))
    # Retain the real window title for Mission Control while QML draws the centered caption.
    set_integer(nswindow, objc.sel_registerName(b"setTitleVisibility:"), 1)


def _motion_preference_reader():
    if sys.platform == "darwin":
        # AppKit has already been loaded by Qt's Cocoa plugin.
        objc, send = _cocoa_messages()
        read = ctypes.CFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)(("objc_msgSend", objc))
        workspace = send(objc.objc_getClass(b"NSWorkspace"), objc.sel_registerName(b"sharedWorkspace"))
        selector = objc.sel_registerName(b"accessibilityDisplayShouldReduceMotion")
        return lambda: bool(workspace and read(workspace, selector))
    if sys.platform == "win32":
        def read():
            enabled = ctypes.c_int(1)
            ctypes.windll.user32.SystemParametersInfoW(0x1042, 0, ctypes.byref(enabled), 0)
            return not enabled.value
        return read
    return lambda: False


def main():
    enforce_single_process()
    parser = argparse.ArgumentParser()
    parser.add_argument("--verify-library", nargs=2, metavar=("LIBRARY", "QUERY"))
    parser.add_argument("--library", type=Path, help="use a specific local library directory")
    args = parser.parse_args()
    basedir = Path(__file__).resolve().parent
    # Explicit paths also support a moved/copied .app and a Windows user directory with spaces.
    import PySide6
    qt = Path(PySide6.__file__).parent
    if (qt / "Qt").is_dir():
        qt = qt / "Qt"
    QtCore.QCoreApplication.setLibraryPaths([str(qt / "plugins")])
    os.environ["QT_MEDIA_BACKEND"] = "windows" if sys.platform == "win32" else "darwin"
    if args.verify_library:
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
        os.environ["QT_QUICK_BACKEND"] = "software"
    QQuickStyle.setStyle("Basic")
    app = QGuiApplication([sys.argv[0]])
    configure_interface_font(app)
    app.setApplicationName("Seekino")
    app.setOrganizationName("Seekino")
    app.setApplicationVersion("0.2.19")
    app.setWindowIcon(QIcon(str(basedir.parent / "SeekinoLogo.png")))
    from .locale import DesktopLocale
    from .qt_player import register_player
    locale = DesktopLocale(app)
    register_player()
    if args.verify_library:
        from .qt_verification import run
        root, query = args.verify_library
        return run(app, Path(root), query)
    from .qt_controller import Workspace
    workspace = Workspace(args.library)
    preferences = PlatformPreferences(app)
    from .posters import PosterProvider
    engine = QQmlApplicationEngine()
    engine.addImageProvider("posters", PosterProvider())
    engine.addImportPath(str(qt / "qml"))
    engine.rootContext().setContextProperty("workspace", workspace)
    engine.rootContext().setContextProperty("desktopPlatform", preferences)
    engine.rootContext().setContextProperty("desktopLocale", locale)
    locale.retranslate.connect(engine.retranslate)
    engine.load(QUrl.fromLocalFile(str(basedir / "ui/Main.qml")))
    if not engine.rootObjects():
        return 1
    window = engine.rootObjects()[0]
    preferences.observe_window(window)
    if sys.platform == "darwin" and app.platformName() == "cocoa":
        _unify_titlebar(window)
    window.show()
    app.aboutToQuit.connect(window.rememberPlayback)
    app.aboutToQuit.connect(workspace.shutdown)
    try:
        return app.exec()
    finally:
        workspace.shutdown()
        # Destroy the window while its context objects are still alive.
        del window, engine


if __name__ == "__main__":
    raise SystemExit(main())
