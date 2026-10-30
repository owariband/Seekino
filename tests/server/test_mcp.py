from contextlib import asynccontextmanager
from types import SimpleNamespace

from fastapi.testclient import TestClient

from server.api.app import build_app
from server.config.auth import AuthConfig


class Catalog:
    def list_series(self, offset=0, limit=20):
        return {"items": [], "offset": offset, "limit": limit, "total": 0}


def services(mode="local"):
    return SimpleNamespace(
        auth=SimpleNamespace(config=AuthConfig(
            mode=mode, secret_key="x" * 32 if mode == "cloud" else "",
        )),
        mcp_retrieval=Catalog(),
    )


def test_mcp_mount_runs_with_application_lifespan():
    events = []

    @asynccontextmanager
    async def lifespan(_app):
        events.append("started")
        yield
        events.append("closed")

    app = build_app(services(), lifespan=lifespan)
    with TestClient(app, base_url="http://127.0.0.1:9621", client=("127.0.0.1", 50000)) as client:
        assert events == ["started"]
        response = client.post("/mcp", headers={"Accept": "application/json, text/event-stream"}, json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                       "clientInfo": {"name": "seekino-test", "version": "1"}},
        })
        assert response.status_code == 200
        assert response.json()["result"]["capabilities"]["tools"] is not None
        tools = client.post("/mcp", headers={"Accept": "application/json, text/event-stream"}, json={
            "jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {},
        })
        assert tools.status_code == 200
        assert {tool["name"] for tool in tools.json()["result"]["tools"]} == {
            "list_series", "retrieve", "read_evidence",
        }
    assert events == ["started", "closed"]


def test_cloud_app_does_not_mount_local_mcp():
    with TestClient(build_app(services("cloud"))) as client:
        assert client.post("/mcp", json={}).status_code == 404


def test_existing_application_without_mcp_still_runs_lifespan():
    original = services()
    del original.mcp_retrieval
    with TestClient(build_app(original)) as client:
        assert client.post("/mcp", json={}).status_code == 404
