"""Whole-window activity wakes an idle Kino without intercepting the player's input."""
import pytest


def test_idle_wakes_once_and_delivers_the_same_key(monkeypatch):
    pytest.importorskip("PySide6")
    from PySide6.QtCore import QCoreApplication, QEvent, QEventLoop, QObject, QTimer, Qt, Signal
    from PySide6.QtGui import QKeyEvent
    from desktop import main

    app = QCoreApplication.instance() or QCoreApplication([])
    monkeypatch.setattr(main, "_motion_preference_reader", lambda: lambda: False)

    class Target(QObject):
        keys = 0

        def event(self, event):
            if event.type() == QEvent.KeyPress:
                self.keys += 1
                return True
            return super().event(event)

    # In-process test suites may already own a QCoreApplication. Preferences only
    # needs a GUI application's activation signal, so use a local signal owner.
    class AppSignals(QObject):
        applicationStateChanged = Signal()

    signal_owner = AppSignals()
    prefs = main.PlatformPreferences(signal_owner)
    target = Target()
    changes = []
    prefs.inputIdleChanged.connect(lambda: changes.append(prefs.inputIdle))
    assert prefs._idle_timer.interval() == 45_000
    prefs._idle_timer.setInterval(12)
    prefs.observe_window(target)
    loop = QEventLoop()
    QTimer.singleShot(60, loop.quit)
    loop.exec()
    assert changes == [True]
    for _ in range(2):
        app.sendEvent(target, QKeyEvent(QEvent.KeyPress, Qt.Key_A, Qt.NoModifier, "a"))
    assert changes == [True, False]
    assert target.keys == 2
