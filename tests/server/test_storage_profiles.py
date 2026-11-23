from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from server.config import settings
from server.config.storage import desktop_storage, server_storage


def write_profile(path: Path, key: str, value):
    path.write_text(yaml.safe_dump({key: value}), encoding="utf-8")
    return path


def test_desktop_loads_its_packaged_section_only(tmp_path, monkeypatch):
    actual = desktop_storage()
    package = write_profile(tmp_path / "storage.yaml", "desktop", actual.model_dump())
    monkeypatch.setenv("SEEKINO_DATABASE_URL", "mysql://invalid")
    monkeypatch.setenv("SEEKINO_ELASTICSEARCH_URL", "https://invalid")
    assert desktop_storage(package) == actual
    with pytest.raises(ValueError, match="missing server"):
        server_storage(package)


@pytest.mark.parametrize(("field", "value"), [
    ("type", "mysql"), ("url_env", "SEEKINO_DATABASE_URL"), ("file", "../elsewhere"),
    ("file", "/tmp/elsewhere"), ("file", ":memory:"),
])
def test_desktop_rejects_remote_or_out_of_library_database(tmp_path, field, value):
    profile = desktop_storage().model_dump()
    profile["database"][field] = value
    with pytest.raises(ValidationError):
        desktop_storage(write_profile(tmp_path / "storage.yaml", "desktop", profile))


def test_server_uses_the_configured_credential_variable(tmp_path, monkeypatch):
    profile = server_storage().model_dump()
    profile["database"]["url_env"] = "TEST_SEEKINO_DB"
    path = write_profile(tmp_path / "storage.yaml", "server", profile)
    monkeypatch.setattr(settings, "server_storage", lambda: server_storage(path))
    monkeypatch.setenv("SEEKINO_DATABASE_URL", "ignored")
    monkeypatch.setenv("TEST_SEEKINO_DB", "mysql+pymysql://localhost/isolated")
    assert settings.resolve_database_url() == "mysql+pymysql://localhost/isolated"
    monkeypatch.delenv("TEST_SEEKINO_DB")
    with pytest.raises(RuntimeError, match="TEST_SEEKINO_DB"):
        settings.resolve_database_url()


def test_active_library_read_allows_progress_and_drains_on_close():
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from contextlib import ExitStack
    from types import SimpleNamespace
    pytest.importorskip("av", reason="install the desktop dependency group")
    from server.desktop.runtime import DesktopLibrary

    entered, release, disposed = threading.Event(), threading.Event(), threading.Event()

    def wait_for_read(**kwargs):
        entered.set()
        assert release.wait(5)
        return []

    services = SimpleNamespace(library=SimpleNamespace(list_series=wait_for_read),
                               jobs=SimpleNamespace(get_video_parse_task=lambda *a, **kw: "progress"))
    cleanup = ExitStack()
    cleanup.callback(disposed.set)
    library = DesktopLibrary(services, cleanup)
    with ThreadPoolExecutor(max_workers=3) as threads:
        reader = threads.submit(library.list_series)
        try:
            assert entered.wait(5)
            assert threads.submit(library.task, "id").result(timeout=2) == "progress"
            closing = threads.submit(library.close)
            assert not disposed.wait(0.1)
        finally:
            release.set()
        assert reader.result(timeout=5) == []
        closing.result(timeout=5)
    assert disposed.is_set()
    with pytest.raises(RuntimeError, match="closed"):
        library.task("id")
