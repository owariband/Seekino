"""Check the desktop's source-string translation catalog and native switch."""
from pathlib import Path

import pytest

pytest.importorskip("PySide6")
from PySide6.QtCore import QCoreApplication, QSettings

from desktop.locale import DesktopLocale, MESSAGES, missing_translations

UI = Path(__file__).resolve().parents[2] / "desktop/ui"


def test_every_qs_tr_source_has_an_english_message():
    assert missing_translations(UI) == []
    assert all(value for value in MESSAGES.values())


def test_translator_and_selection_are_persisted(tmp_path):
    app = QCoreApplication.instance() or QCoreApplication([])
    assert app is not None
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    first = DesktopLocale(settings=settings)
    second = None
    try:
        assert first.language == "zh"
        assert QCoreApplication.translate("desktop", "资料库") == "资料库"
        first.setLanguage("en")
        assert first.language == "en"
        assert QCoreApplication.translate("desktop", "资料库") == "Library"
        assert QCoreApplication.translate("desktop", "第 %1 集") == "Episode %1"
        assert first.translate("描述一个画面，找回那一刻…") == "Describe a scene. Find that moment…"
        assert settings.value("interface/language") == "en"
        second = DesktopLocale(settings=settings)
        assert second.language == "en"
    finally:
        first.setLanguage("zh")
        if second is not None:
            second.setLanguage("zh")
    assert QCoreApplication.translate("desktop", "资料库") == "资料库"
