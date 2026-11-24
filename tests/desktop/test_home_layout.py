"""Protect the Web homepage proportions in the native Qt layout."""
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]


def test_home_hero_scales_at_web_viewport_widths(tmp_path):
    pytest.importorskip("PySide6")
    # Other desktop tests may have created a QCoreApplication without a GUI.
    env = {**os.environ, "QT_QPA_PLATFORM": "offscreen", "QML_DISABLE_DISK_CACHE": "1"}
    subprocess.run([sys.executable, "-B", __file__, str(tmp_path)], env=env, cwd=ROOT,
                   check=True, timeout=30)


def check_native_layout(settings_dir):
    from PySide6.QtCore import QObject, QSettings, QUrl
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtQml import QQmlApplicationEngine, QQmlComponent
    from desktop.main import PlatformPreferences, configure_interface_font

    app = QGuiApplication(["seekino-layout-test"])
    app.setOrganizationName("SeekinoTests")
    app.setApplicationName("HomeLayout")
    QSettings.setDefaultFormat(QSettings.IniFormat)
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, settings_dir)
    configure_interface_font(app)
    engine = QQmlApplicationEngine()
    prefs = PlatformPreferences(app)
    engine.rootContext().setContextProperty("desktopPlatform", prefs)
    ui = ROOT / "desktop/ui/HomeHero.qml"
    component = QQmlComponent(engine, QUrl.fromLocalFile(str(ui)))
    assert not component.isError(), [err.toString() for err in component.errors()]
    for width, height, font, gutter in [(1080, 583.2, 54, 54), (1322, 713.88, 66, 66.1)]:
        hero = component.createWithInitialProperties({
            "series": {"title": "Neural Networks: Zero to Hero", "count": 2, "firstVideoID": "ep"},
            "width": width, "gutter": gutter,
        })
        assert hero is not None
        app.processEvents()
        title = hero.findChild(QObject, "heroTitle")
        art = hero.findChild(QObject, "heroArtwork")
        button = hero.findChild(QObject, "heroPlay")
        assert abs(hero.implicitHeight() - height) < 1
        assert abs(title.property("font").pixelSize() - font) <= 1
        assert abs(art.width() - width) < 1
        assert art.height() >= height
        assert button.implicitHeight() == 48


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    check_native_layout(sys.argv[1])
