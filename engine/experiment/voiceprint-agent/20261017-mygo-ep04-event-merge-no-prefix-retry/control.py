"""Bounded service control and one isolated EP04 reparse; no schema operations."""
from __future__ import annotations

import argparse
from datetime import UTC, datetime
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener

from sqlalchemy import create_engine, event, text

ROOT = Path(__file__).resolve().parents[4]
OUT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
VIDEO = "0019c16c-a7b7-5f6c-8275-5019869c1207"
SERIES = "bdbf28ae-5404-58e4-8c98-699433499ffc"
ORIGINAL = "836fa9f0-2bf4-50fb-9c64-75526fa1dea9"
SHA = "50e981e49a3d74e8c0aca7571ffae8265b88f6388f1d34849876b61ad8bf5814"
HTTP = build_opener(ProxyHandler({}))


def write(name, value):
    (OUT / name).write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n")


def get(url):
    with HTTP.open(url, timeout=10) as response:
        return json.load(response)


def database():
    engine = create_engine(os.environ["ANIVAG_DATABASE_URL"], pool_pre_ping=True,
                           hide_parameters=True, connect_args={"connect_timeout": 5, "read_timeout": 15})

    @event.listens_for(engine, "before_cursor_execute")
    def readonly(_conn, _cursor, statement, _parameters, _context, _many):
        if not statement.lstrip().upper().startswith("SELECT"):
            raise ValueError("Experiment DB access is SELECT-only")

    return engine


def rows(connection, sql, **parameters):
    values = [dict(row) for row in connection.execute(text(sql), parameters).mappings()]
    for row in values:
        for key in ["detail", "parse_config", "content", "last_error"]:
            if isinstance(row.get(key), str):
                row[key] = json.loads(row[key])
    return values


def facts():
    engine = database()
    try:
        with engine.connect() as connection:
            latest = rows(connection, "SELECT p.*,v.series_id,v.episode_order,v.hash AS source_sha256 "
                          "FROM video_parse_task p JOIN video v ON v.id=p.video_id "
                          "WHERE p.video_id=:video AND p.is_deleted=0 ORDER BY p.version DESC LIMIT 1", video=VIDEO)[0]
            assert (latest["series_id"], latest["episode_order"], latest["source_sha256"]) == (SERIES, 4, SHA)
            upload = rows(connection, "SELECT id,tenant_id,status,video_id FROM upload_session "
                          "WHERE video_id=:video ORDER BY create_time DESC LIMIT 1", video=VIDEO)[0]
            active = rows(connection, "SELECT id,status,lease_owner FROM video_parse_task "
                          "WHERE is_deleted=0 AND lease_owner IS NOT NULL")
            chat = rows(connection, "SELECT id,status FROM agent_message WHERE status='PROCESSING'")
            original = {
                "videos": rows(connection, "SELECT id,parse_status,parse_progress,update_time FROM video WHERE series_id=:series ORDER BY id", series=ORIGINAL),
                "tasks": rows(connection, "SELECT p.id,p.version,p.status,p.update_time FROM video_parse_task p JOIN video v ON v.id=p.video_id WHERE v.series_id=:series ORDER BY p.id", series=ORIGINAL),
                "contents": rows(connection, "SELECT c.id,c.version,c.content,c.update_time FROM chunk_parse_content c JOIN chunk_parse_task t ON t.id=c.chunk_task_id JOIN video_parse_task p ON p.id=t.video_task_id JOIN video v ON v.id=p.video_id WHERE v.series_id=:series ORDER BY c.id", series=ORIGINAL),
            }
            for row in original["contents"]:
                row["content_sha256"] = hashlib.sha256(json.dumps(row.pop("content"), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        return {"captured_at": datetime.now(UTC).isoformat(), "latest": latest, "upload": upload,
                "active_parse_leases": active, "processing_chat": chat, "original_series": original}
    finally:
        engine.dispose()


def services():
    spec = importlib.util.spec_from_file_location("anivag_local_services", ROOT / "scripts/local_services.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.configure()
    return module


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["preflight", "restart", "submit", "verify-original"])
    action = parser.parse_args().action
    os.chdir(ROOT)
    module = services()
    base = f"http://127.0.0.1:{module.port('ANIVAG_HTTP_PORT')}"
    if action == "preflight":
        state = facts()
        state["processes"] = {name: {"pid": module.owned_pid(name), "record": json.loads((module.STATE / f"{name}.pid.json").read_text())} for name in ["api", "web"]}
        state["health"] = get(base + "/healthz")
        write("baseline.json", state)
        files = ["agent/video_parse_v2/prompts.py", "agent/video_parse_v2/voiceprint.py", "server/constants/parse.py", "server/repository/video_parse.py", "server/service/video_parse.py", "gateway/local/campplus.py", "engine/parsev2/validation.py", "agent/video_parse_v2/refine/agent.py", "config.yaml"]
        write("source-sha256.json", {"head": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(), "files": {file: hashlib.sha256((ROOT / file).read_bytes()).hexdigest() for file in files}})
        print(json.dumps({"latest_id": state["latest"]["id"], "version": state["latest"]["version"], "status": state["latest"]["status"], "active_parse_leases": state["active_parse_leases"], "processing_chat": state["processing_chat"], "process_ids": {name: value["pid"] for name, value in state["processes"].items()}, "health": state["health"]}, ensure_ascii=False), flush=True)
    elif action == "restart":
        state = facts()
        if state["active_parse_leases"] or state["processing_chat"]:
            raise RuntimeError("Wait for existing parse/chat work before restarting")
        before = {name: json.loads((module.STATE / f"{name}.pid.json").read_text()) for name in ["api", "web"]}
        write("restart-before.json", {"at": datetime.now(UTC).isoformat(), "processes": before})
        for name in ["web", "api"]:
            pid = module.owned_pid(name)
            if pid is None:
                raise RuntimeError(f"Managed {name} process identity could not be verified")
            for signum, seconds in [(signal.SIGTERM, 18), (signal.SIGINT, 8), (signal.SIGKILL, 5)]:
                if module.owned_pid(name) != pid:
                    break
                os.killpg(pid, signum)
                print(f"{name} pid={pid} signal={signum.name}", flush=True)
                deadline = time.monotonic() + seconds
                while module.owned_pid(name) == pid and time.monotonic() < deadline:
                    time.sleep(0.2)
            if module.owned_pid(name) == pid:
                raise RuntimeError(f"Managed {name} did not stop")
            (module.STATE / f"{name}.pid.json").unlink(missing_ok=True)
        module.start_applications()
        after = {name: json.loads((module.STATE / f"{name}.pid.json").read_text()) for name in ["api", "web"]}
        write("restart-after.json", {"at": datetime.now(UTC).isoformat(), "processes": after, "health": get(base + "/healthz"), "database_and_es_restarted": False})
        print("API/Web restart complete; MySQL/Elasticsearch unchanged.", flush=True)
    elif action == "submit":
        if (OUT / "reparse-response.json").exists():
            raise RuntimeError("A response already exists; inspect it instead of submitting twice")
        state = facts()
        if state["latest"]["status"] not in ["COMPLETE", "PARSING_FAILED", "INDEXING_FAILED"]:
            raise RuntimeError("The latest EP04 parse has not reached a terminal state")
        request = json.loads((ROOT / "engine/experiment/voiceprint-agent/20261017-mygo-ep04-multiref-no-prefix/reparse-request.json").read_text())
        config = request["parseConfig"]
        config.update(compact_enabled=False, prefix_context=None)
        config["models"]["prefix_context"] = None
        from server.model.dto.uploadDTO import UploadReparseRequest
        UploadReparseRequest.model_validate(request)
        write("reparse-request.json", request)
        query = {"uploadSessionId": state["upload"]["id"], "tenantId": state["upload"]["tenant_id"], "expectedJobVersion": state["latest"]["version"]}
        write("reparse-query.json", query)
        call = Request(base + "/api/uploads/reparse?" + urlencode(query), data=json.dumps(request).encode(), headers={"Content-Type": "application/json"}, method="POST")
        with HTTP.open(call, timeout=30) as response:
            result = {"http_status": response.status, "body": json.load(response)}
        write("reparse-response.json", result)
        body = result["body"]
        assert body["videoId"] == VIDEO and body["parseConfig"]["compact_enabled"] is False
        assert body["version"] == state["latest"]["version"] + 1
        print(json.dumps({key: body[key] for key in ["videoParseTaskId", "videoId", "status", "version", "chunkCount"]}, ensure_ascii=False), flush=True)
    else:
        before = json.loads((OUT / "baseline.json").read_text())["original_series"]
        current = json.loads(json.dumps(facts()["original_series"], default=str))
        write("original-preservation-check.json", {"captured_at": datetime.now(UTC).isoformat(), "unchanged": before == current, "counts": {key: len(value) for key, value in current.items()}})
        assert before == current, "Original series changed during isolated reparse"
        print("Original series unchanged.", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"control_error={type(error).__name__}: {error}" if isinstance(error, (RuntimeError, AssertionError)) else f"control_error={type(error).__name__}", flush=True)
        raise SystemExit(1)
