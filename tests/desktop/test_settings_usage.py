"""The native settings bridge forwards discovery and reads the usage ledger off the UI thread."""
import pytest

pytest.importorskip("PySide6")
from PySide6.QtCore import QCoreApplication

from desktop.qt_controller import Workspace


def test_discovery_and_usage_states_stay_separate_from_general_errors(tmp_path, monkeypatch):
    app = QCoreApplication.instance() or QCoreApplication([])
    workspace = Workspace(tmp_path)
    workspace._set("ready", True)
    requests = []

    def request(action, **args):
        requests.append((action, args))
        if action == "discoverModels":
            return {"modelIds": ["model-a", "model-b"], "listAvailable": True}
        assert action == "usage"
        return {"status": "available", "totals": {"callCount": 1}}

    def submit(run, done, **_):
        try:
            value = run()
        except Exception as exc:
            done(None, str(exc))
        else:
            done(value, "")

    monkeypatch.setattr("desktop.qt_controller._request", request)
    monkeypatch.setattr(workspace, "_submit", submit)
    discovered, failed = [], []
    workspace.modelsDiscovered.connect(lambda *args: discovered.append(args))
    workspace.modelDiscoveryFailed.connect(lambda *args: failed.append(args))
    try:
        workspace.discoverModels("request-1", "model", "openai_chat", "https://models.example/v1",
                                 "typed-key", "revision-1")
        assert requests[0] == ("discoverModels", {
            "root": str(tmp_path), "purpose": "model", "protocol": "openai_chat",
            "baseUrl": "https://models.example/v1", "apiKey": "typed-key", "revision": "revision-1",
        })
        assert len(discovered) == 1 and discovered[0][0] == "request-1" and not failed
        assert list(discovered[0][1]) == ["model-a", "model-b"] and discovered[0][2] is True
        assert not workspace.error

        workspace.loadUsage()
        assert requests[-1] == ("usage", {})
        assert workspace.usage == {"status": "available", "totals": {"callCount": 1}}
        assert not workspace.usageLoading and not workspace.usageError

        def reject(action, **args):
            raise RuntimeError("模型列表接口返回 HTTP 401，可手动填写模型 ID")

        monkeypatch.setattr("desktop.qt_controller._request", reject)
        workspace.discoverModels("request-2", "watch", "openai_chat", "https://models.example/v1",
                                 "", "revision-2")
        assert failed == [("request-2", "模型列表接口返回 HTTP 401，可手动填写模型 ID")]
        assert not workspace.error

        workspace.loadUsage()
        assert not workspace.usageLoading
        assert workspace.usageError == "模型列表接口返回 HTTP 401，可手动填写模型 ID"
        assert workspace.usage["status"] == "available"
        assert not workspace.error
    finally:
        workspace.shutdown()
        app.processEvents()
