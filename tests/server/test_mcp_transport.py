from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from server.api.mcp import create_mcp_app


def _service(**overrides):
    return SimpleNamespace(**{
        "list_series": lambda **kwargs: {"items": [], "next_offset": None},
        "retrieve": lambda **kwargs: {"items": [], "top_k_unit": "distinct_chunks"},
        "browse_events": lambda **kwargs: {"items": [], "next_cursor": None},
        "read_evidence": lambda **kwargs: {
            "matched_event": {"start_ms": 1000, "end_ms": 3000},
            "media": {"original_url": "/api/videos/content?videoId=video&variant=original", "playback_url": None},
        },
        **overrides,
    })


def _client(service=None, *, auth_mode="local", address="127.0.0.1", base_url="http://127.0.0.1:9621"):
    return TestClient(
        create_mcp_app(service or _service(), auth_mode=auth_mode),
        client=(address, 50000), base_url=base_url,
        headers={"Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-06-18"},
    )


def _rpc(client, method, params=None, **kwargs):
    return client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}, **kwargs)


def _call(client, name, arguments=None):
    response = _rpc(client, "tools/call", {"name": name, "arguments": arguments or {}})
    assert response.status_code == 200
    return response.json()["result"]


def test_2025_client_handshake_discovers_generated_schemas_and_structured_results():
    with _client() as client:
        response = _rpc(client, "initialize", {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "codex-contract-test", "version": "1"},
        })
        assert response.status_code == 200
        assert response.json()["result"]["protocolVersion"] == "2025-06-18"
        response = client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"})
        assert response.status_code == 202
        tools = {tool["name"]: tool for tool in _rpc(client, "tools/list").json()["result"]["tools"]}
        assert set(tools) == {"list_series", "retrieve", "read_evidence", "browse_events", "find_edit_templates", "get_style_card",
                              "get_edit_template", "list_edit_template_versions", "save_edit_template_version",
                              "append_edit_template_note", "set_default_edit_template_version"}
        assert all(tool["description"] and tool["outputSchema"] for tool in tools.values())
        schema = tools["retrieve"]["inputSchema"]
        assert schema["required"] == ["query"]
        assert "ctx" not in schema["properties"]
        assert "tenant_id" not in schema["properties"]
        assert schema["properties"]["top_k"]["maximum"] == 20
        assert tools["retrieve"]["annotations"]["readOnlyHint"] is True
        assert tools["browse_events"]["annotations"]["readOnlyHint"] is True
        assert tools["browse_events"]["inputSchema"]["required"] == ["series_id"]
        assert "tenant_id" not in tools["browse_events"]["inputSchema"]["properties"]
        assert _call(client, "browse_events", {"series_id": "series"})["structuredContent"]["next_cursor"] is None
        assert _call(client, "retrieve", {"query": "someone runs"})["structuredContent"]["top_k_unit"] == "distinct_chunks"
        evidence = _call(client, "read_evidence", {"evidence_ref": "ref"})["structuredContent"]
        assert evidence["media"]["original_url"] == "http://127.0.0.1:9621/api/videos/content?videoId=video&variant=original"
        assert evidence["media"]["playback_url"] is None


@pytest.mark.parametrize("headers", [
    {"Host": "evil.example"},
    {"Host": "localhost.evil.example:9621"},
    {"Host": "user@localhost:9621"},
    {"Host": "localhost:99999"},
    {"Origin": "https://evil.example"},
    {"Origin": "http://localhost:5173"},
    {"Origin": "null"},
    {"Forwarded": "for=127.0.0.1"},
    {"X-Forwarded-For": "127.0.0.1"},
    {"X-Forwarded-Host": "localhost:9621"},
    {"X-Forwarded-Proto": "http"},
])
def test_local_endpoint_rejects_foreign_origins_hosts_and_forwarded_connections(headers):
    with _client() as client:
        response = _rpc(client, "tools/list", headers=headers)
        assert response.status_code == 403
        assert response.json() == {"error": "MCP_LOCAL_CONNECTION_REQUIRED"}


@pytest.mark.parametrize("address", ["192.168.1.3", "203.0.113.4", "testclient"])
def test_local_endpoint_checks_the_actual_peer_even_with_local_host(address):
    with _client(address=address) as client:
        assert _rpc(client, "tools/list").status_code == 403


def test_loopback_cannot_enable_an_unconfigured_cloud_transport():
    with _client(auth_mode="cloud") as client:
        assert _rpc(client, "tools/list").status_code == 503


def test_ipv6_loopback_and_same_origin_are_supported():
    with _client(address="::1") as client:
        assert _rpc(client, "tools/list", headers={
            "Host": "[::1]:9621", "Origin": "http://[::1]:9621",
        }).status_code == 200


def test_sync_service_runs_outside_the_async_event_loop():
    def retrieve(**kwargs):
        with pytest.raises(RuntimeError, match="no running event loop"):
            asyncio.get_running_loop()
        return {"query": kwargs["query"]}

    with _client(_service(retrieve=retrieve)) as client:
        assert _call(client, "retrieve", {"query": "run"})["structuredContent"] == {"query": "run"}


@pytest.mark.parametrize("exception,expected", [
    (ValueError("EVIDENCE_STALE: /private/secret/source.mp4"), "EVIDENCE_STALE"),
    (ValueError("unknown /private/secret/source.mp4"), "RETRIEVAL_FAILED"),
    (RuntimeError("database password /private/secret/source.mp4"), "RETRIEVAL_FAILED"),
])
def test_service_errors_are_actionable_without_internal_details(exception, expected):
    def fail(**kwargs):
        raise exception

    with _client(_service(read_evidence=fail)) as client:
        result = _call(client, "read_evidence", {"evidence_ref": "ref"})
        assert result["isError"] is True
        text = result["content"][0]["text"]
        assert expected in text
        assert "/private" not in text
        assert "password" not in text


@pytest.mark.parametrize("arguments", [
    {"query": "a", "top_k": 21}, {"query": "a", "top_k": True},
    {"query": "a", "series_ids": []}, {"query": "a", "event_types": ["invented"]},
    {"query": "a" * 4001},
])
def test_tool_schema_rejects_invalid_arguments_before_service(arguments):
    def unexpected(**kwargs):
        pytest.fail("Service should not be called for invalid arguments")

    with _client(_service(retrieve=unexpected)) as client:
        result = _call(client, "retrieve", arguments)
        assert result["isError"] is True


def test_transport_bounds_request_and_result_payloads():
    with _client(_service(retrieve=lambda **kwargs: {"oversized": "a" * 131073})) as client:
        assert "RESULT_TOO_LARGE" in _call(client, "retrieve", {"query": "a"})["content"][0]["text"]
        response = _rpc(client, "tools/call", {"name": "retrieve", "arguments": {"query": "a" * 70000}})
        assert response.status_code == 413


def test_modern_skills_manifest_and_legacy_resources_serve_identical_packaged_bytes():
    import hashlib
    from pathlib import Path

    from mcp.shared.inbound import CLIENT_CAPABILITIES_META_KEY, NAME_BEARING_METHODS, PROTOCOL_VERSION_META_KEY

    version = "2026-07-28"
    capability = "io.modelcontextprotocol/skills"
    uri = "skill://seekino-editing-materials/SKILL.md"
    root = Path(__file__).resolve().parents[2] / "server/skills/seekino-editing-materials"
    with _client() as client:
        def modern(method, **params):
            headers = {"Mcp-Method": method, "MCP-Protocol-Version": version}
            if key := NAME_BEARING_METHODS.get(method):
                headers["Mcp-Name"] = params[key]
            meta = {PROTOCOL_VERSION_META_KEY: version, CLIENT_CAPABILITIES_META_KEY: {"extensions": {capability: {}}}}
            return _rpc(client, method, {**params, "_meta": meta}, headers=headers).json()

        assert capability in modern("server/discover")["result"]["capabilities"]["extensions"]
        entry, = modern("skills/list")["result"]["skills"]
        assert modern("skills/get", uri=uri)["result"]["skill"] == entry
        assert modern("skills/get", uri=uri + "/absent")["error"]["code"] == -32602
        assert entry["uri"] == uri and entry["frontmatter"]["name"] == root.name
        resources = {item["uri"] for item in _rpc(client, "resources/list").json()["result"]["resources"]}
        assert {r["uri"] for r in entry["resources"]} == resources
        assert len(resources) == 2
        for item in entry["resources"]:
            contents, = _rpc(client, "resources/read", {"uri": item["uri"]}).json()["result"]["contents"]
            raw = contents["text"].encode()
            assert raw == (root / item["uri"].removeprefix(f"skill://{root.name}/")).read_bytes()
            assert item["size"] == len(raw) and item["digest"] == "sha256:" + hashlib.sha256(raw).hexdigest()


def test_media_absolute_url_cannot_be_supplied_by_service_result():
    with _client(_service(read_evidence=lambda **kwargs: {"media": {"original_url": "https://evil.example/private"}})) as client:
        result = _call(client, "read_evidence", {"evidence_ref": "ref"})
        assert result["isError"] is True
        assert "evil.example" not in result["content"][0]["text"]


def test_read_evidence_advertises_optional_source_clip_and_returns_only_same_origin_clip_urls():
    def read(evidence_ref, *, start_ms, end_ms, cloud):
        assert (evidence_ref, start_ms, end_ms, cloud) == ("ref", 9010, 9350, False)
        return {"media": {"clip_url": "/api/videos/clip?evidenceRef=ref&startMs=9010&endMs=9350"}}

    args = {"evidence_ref": "ref", "start_ms": 9010, "end_ms": 9350}
    with _client(_service(read_evidence=read)) as client:
        tool = next(t for t in _rpc(client, "tools/list").json()["result"]["tools"] if t["name"] == "read_evidence")
        assert tool["inputSchema"]["required"] == ["evidence_ref"]
        assert set(tool["inputSchema"]["properties"]) == set(args)
        clip = _call(client, "read_evidence", args)["structuredContent"]["media"]
        assert clip["clip_url"] == "http://127.0.0.1:9621/api/videos/clip?evidenceRef=ref&startMs=9010&endMs=9350"
        assert _call(client, "read_evidence", {**args, "start_ms": 9.01})["isError"]
    with _client(_service(read_evidence=lambda **_: {"media": {"clip_url": "/api/videos/content?variant=original"}})) as client:
        assert _call(client, "read_evidence", args)["isError"]
