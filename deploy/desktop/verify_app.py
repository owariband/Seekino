"""Replay a fixture import and check the unchanged native app from a new location."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
from tempfile import TemporaryDirectory
from time import monotonic, sleep

ROOT = Path(__file__).resolve().parents[2]


def seed(app, work):
    # Load the packaged business code and wheels. Only the fixture and pytest need the checkout.
    resources = app / "Contents/Resources"
    sys.path[:0] = [str(resources), str(resources / "site-packages"), str(ROOT)]
    import pytest
    from server.desktop import app as desktop_app
    assert Path(desktop_app.__file__).is_relative_to(resources)
    dispatch = desktop_app.dispatch

    spec = importlib.util.spec_from_file_location("pipeline", ROOT / "tests/server/test_desktop_pipeline.py")
    pipeline = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pipeline)
    source = work / "episode.mp4"
    pipeline._fixture_movie(source)

    def request(action, **args):
        reply = json.loads(dispatch(json.dumps({"action": action, "args": args})))
        if "error" in reply:
            raise RuntimeError(f"{action}: {reply['error']}")
        return reply["value"]

    def offline(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            raise RuntimeError("The fixed model replay must stay offline")
        return connect(sock, address)

    connect = socket.socket.connect
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(socket.socket, "connect", offline)
        calls = pipeline._fixed_model_responses(patch)
        request("open", root=str(work / "library"))
        try:
            series = request("createSeries", name="桌面验收 · 原片")
            imported = request("import", path=str(source), seriesID=series["id"])
            deadline = monotonic() + 45
            while True:
                job, = request("tasks")
                if job["status"] == "COMPLETE":
                    break
                if job["error"] or monotonic() >= deadline:
                    raise RuntimeError(f"Unexpected job state: {job['status']} ({job['error']}); {calls}")
                sleep(0.2)
            for query, lexical in (("红旗", True), ("挥动旗帜", False)):
                result = request("search", query=query, seriesIDs=[series["id"]], keywords=lexical)
                hit, = result["hits"]
                assert (hit["video_id"], hit["start_ms"], hit["end_ms"]) == (imported["videoID"], 300, 1200)
                assert Path(request("openResult", sourceRef=hit["source_ref"])["path"]).read_bytes() == source.read_bytes()
            assert {"video_parse_v2_chunk", "IntervalRefinementResult", "text", "video_url"} <= set(calls)
        finally:
            request("close")
        print("fixed-model import, V2, keyword and semantic search: passed", flush=True)


def verify_watch(app, library):
    # -I -S keeps unshipped development SDKs out of this process. Imports below
    # must work with the installed product's packages and no application server.
    resources = app / "Contents/Resources"
    sys.path[:0] = [str(resources), str(resources / "site-packages")]
    import httpx
    from server.desktop.app import dispatch

    def request(action, **args):
        reply = json.loads(dispatch(json.dumps({"action": action, "args": args})))
        if "error" in reply:
            raise RuntimeError(f"{action}: {reply['error']}")
        return reply["value"]

    def fixed_reply(transport, req):
        assert req.url.host == "localhost" and req.url.path == "/v1/chat/completions"
        payload = json.loads(req.content)
        assert payload["model"] == "qa-vision"
        assert any(part.get("type") == "image_url" and part["image_url"]["url"].startswith("data:image/")
                   for msg in payload["messages"] for part in msg["content"] if isinstance(part, dict))
        return httpx.Response(200, json={"id": "local", "created": 0, "model": "qa-vision",
                              "object": "chat.completion", "choices": [{"index": 0, "finish_reason": "stop",
                              "message": {"role": "assistant", "content": '{"answer":"frame received"}'}}]})

    httpx.HTTPTransport.handle_request = fixed_reply
    request("open", root=str(library))
    try:
        revision = request("modelSettings", root=str(library))["watchRevision"]
        request("saveWatchSettings", root=str(library), revision=revision,
                form={"protocol": "openai_chat", "baseUrl": "http://localhost:7890/v1",
                      "auth": "none", "model": "qa-vision"})
        series, = request("seriesList")
        answer = request("askWatch", videoID=series["firstVideoID"], seriesID=series["id"],
                         positionMs=500, question="What is in the frame?", history=[])
        assert answer == {"text": "frame received", "positionMs": 500}, answer
    finally:
        request("close")
    print("packaged watch model, source frame and HTTP contract: passed", flush=True)


def verify(app):
    # Only the driver imports the watchdog. The relocated --watch subprocess
    # runs with -I -S and must continue to use only the packaged runtime.
    if __package__:
        from .run_probe import run_probe
    else:
        from run_probe import run_probe

    def check_probe(command, **settings):
        status = run_probe(command, **settings)
        if status:
            raise subprocess.CalledProcessError(status, command)

    with TemporaryDirectory(prefix="seekino-desktop-qa-") as name:
        work = Path(name).resolve()
        # The seed process imports the exact wheel files but has its own CPython.
        check_probe([sys.executable, "-B", __file__, str(app), "--seed", str(work)],
                    env={**os.environ, "DYLD_LIBRARY_PATH": str(app / "Contents/Frameworks")}, timeout=90)
        moved = work / "Moved Seekino.app"
        shutil.copytree(app, moved, symlinks=True)
        subprocess.run(["codesign", "--verify", "--deep", "--strict", moved], check=True)
        check_probe([sys.executable, "-B", "-I", "-S", __file__, str(moved), "--watch", str(work / "library")],
                    env={"DYLD_LIBRARY_PATH": str(moved / "Contents/Frameworks"), "HOME": str(work)},
                    cwd=work, timeout=45)
        observer = work / "verify_process"
        subprocess.run(["clang", "-Wall", "-Werror", ROOT / "deploy/desktop/verify_process.c", "-o", observer], check=True)
        if run_probe([observer, "/bin/sh", "-c", "/usr/bin/true & wait"], timeout=10) != 86:
            raise RuntimeError("macOS observer missed its positive control")
        check_probe([observer, moved / "Contents/MacOS/Seekino", "--verify-library", work / "library", "红旗"],
                    cwd=work, env={"PATH": "/nonexistent", "HOME": str(work), "LANG": "en_US.UTF-8"}, timeout=30)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("app", type=Path, help="absolute path to Seekino.app")
    parser.add_argument("--seed", type=Path, help="only seed a new isolated QA library")
    parser.add_argument("--watch", type=Path, help="verify a seeded library with only the bundled modules")
    args = parser.parse_args()
    if args.watch is not None:
        verify_watch(args.app.resolve(strict=True), args.watch)
    elif args.seed is None:
        verify(args.app.resolve(strict=True))
    else:
        seed(args.app.resolve(strict=True), args.seed)
