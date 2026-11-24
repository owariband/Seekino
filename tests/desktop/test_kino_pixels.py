"""Verify the desktop actually paints the canonical Kino frames in its native window."""
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]


def test_native_kino_engine_tracks_web_source():
    engine = (ROOT / "web/src/shared/kino/engine.ts").read_text(encoding="utf-8")
    generated = (ROOT / "desktop/ui/KinoEngine.mjs").read_text(encoding="utf-8")
    assert sha256(engine.encode()).hexdigest() in generated.splitlines()[0], (
        "Regenerate the Qt ES module with node web/scripts/build-kino-desktop.mjs"
    )


def test_rendered_kino_frames(tmp_path):
    pytest.importorskip("PySide6")
    env = {**os.environ, "QT_QPA_PLATFORM": "offscreen", "QT_QUICK_BACKEND": "software", "QML_DISABLE_DISK_CACHE": "1"}
    subprocess.run([sys.executable, "-B", __file__, str(tmp_path)], check=True, env=env, timeout=30)


def check_rendering(settings):
    from PySide6.QtCore import QEventLoop, QSettings, Qt, QTimer, QUrl
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtQml import QQmlComponent, QQmlEngine, QQmlExpression, QQmlPropertyMap, qmlContext
    from PySide6.QtQuick import QQuickWindow

    app = QGuiApplication(["kino-raster-test"])
    app.setOrganizationName("SeekinoTests")
    app.setApplicationName("KinoRaster")
    QSettings.setDefaultFormat(QSettings.IniFormat)
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, settings)
    engine = QQmlEngine()
    platform = QQmlPropertyMap()
    platform.insert("reducedMotion", True)
    platform.insert("inputIdle", False)
    engine.rootContext().setContextProperty("desktopPlatform", platform)
    comp = QQmlComponent(engine, QUrl.fromLocalFile(str(ROOT / "desktop/ui/Kino.qml")))
    assert comp.isReady(), [err.toString() for err in comp.errors()]
    win = QQuickWindow()
    win.resize(114, 84)
    win.setColor(Qt.white)
    kino = comp.createWithInitialProperties({"width": 114, "height": 84, "sceneWidth": 38, "ground": False, "interactive": False})
    kino.setParentItem(win.contentItem())
    win.show()

    def settle():
        loop = QEventLoop()
        QTimer.singleShot(75, loop.quit)
        loop.exec()

    def js(expression):
        expression = QQmlExpression(qmlContext(kino), kino, expression)
        value, _ = expression.evaluate()
        assert not expression.hasError(), expression.error().toString()
        return value

    def assert_pixels():
        settle()
        image = win.grabWindow()
        assert not image.isNull()
        ratio = image.devicePixelRatio()
        palette = json.loads(js('JSON.stringify(Engine.PALETTE)'))
        palette[None] = "#ffffff"
        if not js('Studio.dark'):
            palette["spark"] = palette["shade"]
        cells = json.loads(js('JSON.stringify(Engine.rasterize(frame, columns).cells)'))
        assert any(cells)
        for i, colour in enumerate(cells):
            # Sample the centre of each displayed 3 × 3 sprite pixel.
            x, y = i % 38 * 3 + 1, i // 38 * 3 + 1
            assert image.pixelColor(int(x * ratio), int(y * ratio)).name() == palette[colour], (i, colour)
        return cells

    for state in ("idle", "loading", "think", "sleep", "oops"):
        kino.setProperty("mood", state)
        assert_pixels()

    # Hold each selected sprint frame so the software window and the readback have presented
    # the very same frame. Compare both directions, gait phases and the feet under the belly.
    poses = []
    for direction in ("dashLeft", "dashRight"):
        for step in (0, 2):
            js(f'frame = Engine.buildTimeline("{direction}", Engine.startCursor("{direction}", 38), 38).loop[{step}]')
            poses.append(tuple(assert_pixels()))
    assert len(set(poses)) == 4
    win.close()


if __name__ == "__main__":
    check_rendering(sys.argv[1])
