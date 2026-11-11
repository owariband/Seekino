"""Run a browser scenario against the built watch page, a live API and a real MP4."""

from pathlib import Path
import socket
import subprocess
from threading import Thread
from time import monotonic, sleep

import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles


def run_watch_browser(app: FastAPI, source: Path, script: str, *arguments: str) -> None:
    web = Path(__file__).resolve().parents[2] / "web"
    dist = web / "dist"
    assert (dist / "index.html").is_file(), "Run npm --prefix web run build first"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=5",
                    "-t", "20", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(source)], check=True)
    app.mount("/assets", StaticFiles(directory=dist / "assets"), name="web-assets")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str):
        return FileResponse(dist / "index.html")

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
        thread = Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            deadline = monotonic() + 10
            while not server.started and thread.is_alive() and monotonic() < deadline:
                sleep(.02)
            assert server.started
            subprocess.run(["node", f"tests/{script}", f"http://127.0.0.1:{port}", *arguments],
                           cwd=web, check=True, timeout=120)
        finally:
            server.should_exit = True
            thread.join(10)
