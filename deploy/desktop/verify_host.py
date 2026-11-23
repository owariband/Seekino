"""Replay the integration test inside a temporary copy of an already built macOS host."""
import argparse
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[2]


def verify(app: Path) -> None:
    with TemporaryDirectory() as tmp:
        directory = Path(tmp).resolve()
        copy = directory / "Moved Backend.app"
        shutil.copytree(app, copy, symlinks=True)
        resources = copy / "Contents/Resources"
        # Only this disposable copy gets the pytest runner. Application libraries still
        # load from the built product and the interpreter ignores the developer venv.
        requirements = directory / "test-requirements.txt"
        subprocess.run(["uv", "export", "--locked", "--only-group", "dev", "--no-emit-project",
                        "--format", "requirements.txt", "--output-file", requirements],
                       cwd=ROOT, check=True, stdout=subprocess.DEVNULL)
        subprocess.run(["uv", "pip", "install", "--python", sys.executable, "--no-deps", "--require-hashes",
                        "--target", resources / "site-packages", "-r", requirements], check=True)
        shutil.copy2(ROOT / "tests/server/test_desktop_pipeline.py", resources / "_pipeline_test.py")
        # Replace the smoke command in the copy. The native executable, data, backend,
        # provider SDKs, media libraries and SQLite extension stay byte-for-byte identical.
        shutil.copy2(Path(__file__).with_name("verify_runtime.py"), resources / "server/desktop/host.py")
        executable = copy / "Contents/MacOS/SeekinoBackend"
        observer = directory / "verify_process"
        subprocess.run(["clang", "-Wall", "-Werror", Path(__file__).with_name("verify_process.c"), "-o", observer], check=True)
        positive = subprocess.run([observer, "/bin/sh", "-c", "/usr/bin/true & wait"])
        if positive.returncode != 86:
            raise RuntimeError("macOS process observer missed its positive control")
        subprocess.run([observer, executable, directory], cwd=directory, check=True,
                       env={"PATH": "/nonexistent", "SEEKINO_DATABASE_URL": "invalid",
                            "SEEKINO_ELASTICSEARCH_URL": "invalid", "SEEKINO_AUTH_MODE": "cloud",
                            "HOME": str(directory), "LANG": "en_US.UTF-8"})


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("app", type=Path)
    verify(parser.parse_args().app)
