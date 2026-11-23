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
    # Use the app's wheel versions with checkout-only test helpers and pytest.
    sys.path[:0] = [str(app / "Contents/Resources/site-packages"), str(ROOT)]
    import pytest
    from server.desktop.app import dispatch

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


def verify(app):
    with TemporaryDirectory(prefix="seekino-desktop-qa-") as name:
        work = Path(name).resolve()
        # The seed process imports the exact wheel files but has its own CPython.
        subprocess.run([sys.executable, "-B", __file__, str(app), "--seed", str(work)],
                       env={**os.environ, "DYLD_LIBRARY_PATH": str(app / "Contents/Frameworks")},
                       check=True, timeout=90)
        moved = work / "Moved Seekino.app"
        shutil.copytree(app, moved, symlinks=True)
        subprocess.run(["codesign", "--verify", "--deep", "--strict", moved], check=True)
        observer = work / "verify_process"
        subprocess.run(["clang", "-Wall", "-Werror", ROOT / "deploy/desktop/verify_process.c", "-o", observer], check=True)
        positive = subprocess.run([observer, "/bin/sh", "-c", "/usr/bin/true & wait"])
        if positive.returncode != 86:
            raise RuntimeError("macOS observer missed its positive control")
        subprocess.run([observer, moved / "Contents/MacOS/Seekino", "--verify-library", work / "library", "红旗"],
                       cwd=work, env={"PATH": "/nonexistent", "HOME": str(work), "LANG": "en_US.UTF-8"},
                       check=True, timeout=30)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("app", type=Path, help="absolute path to Seekino.app")
    parser.add_argument("--seed", type=Path, help="only seed a new isolated QA library")
    args = parser.parse_args()
    if args.seed is None:
        verify(args.app.resolve(strict=True))
    else:
        seed(args.app.resolve(strict=True), args.seed)
