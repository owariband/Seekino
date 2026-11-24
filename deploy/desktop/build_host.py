"""Build the ad-hoc signed macOS integration host with relocatable CPython 3.12."""
import argparse
import json
import plistlib
import shutil
import subprocess
import sys
import sysconfig
from zipfile import ZipFile
from pathlib import Path
from tempfile import TemporaryDirectory

import yaml


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from server.config.storage import desktop_storage


def copy_business_files(resources: Path) -> None:
    for package in ("agent", "engine", "gateway", "server"):
        for src in (ROOT / package).rglob("*"):
            rel = src.relative_to(ROOT)
            if not src.is_file() or (src.suffix != ".py" and rel.as_posix() not in {
                "agent/video_parse_v2/edit_reference_pick.txt", "agent/video_parse_v2/edit_reference_refine.txt",
            }):
                continue
            if rel.as_posix() in {"server/main.py", "server/cli.py", "server/bootstrap.py"}:
                continue
            if rel.parts[:2] == ("agent", "chat") and rel.as_posix() not in {
                "agent/chat/__init__.py", "agent/chat/common/__init__.py", "agent/chat/common/errors.py",
                "agent/chat/video_search/__init__.py", "agent/chat/video_search/frame_evidence.py",
            }:
                continue
            if rel.parts[:2] in {
                ("gateway", "bridge"), ("gateway", "codex"), ("gateway", "claude_code"),
                ("server", "caller"), ("server", "api"), ("server", "router"), ("server", "skills"),
            } or (rel.parts[:2] == ("server", "storage") and src.stem.startswith("elasticsearch")):
                continue
            target = resources / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, target)
    config = resources / "config"
    config.mkdir()
    (config / "storage.yaml").write_text(yaml.safe_dump({"desktop": desktop_storage().model_dump()}), encoding="utf-8")
    shutil.copy2(ROOT / "config/model-pricing.yaml", config)
    models = Path("deploy/cloud/models.example.yaml")
    (resources / models.parent).mkdir(parents=True)
    shutil.copy2(ROOT / models, resources / models)


def build(destination: Path, media_wheel: Path | None = None) -> None:
    if sys.platform != "darwin" or sys.version_info[:2] != (3, 12):
        raise RuntimeError("build with the macOS project Python 3.12 environment")
    if media_wheel is not None:
        media_wheel = media_wheel.resolve(strict=True)
        if not media_wheel.name.startswith("av-19.0.2-") or media_wheel.suffix != ".whl":
            raise ValueError("expected a wheel built from the locked PyAV 19.0.2 sources")
        with ZipFile(media_wheel) as package:
            manifest = "av-19.0.2.dist-info/seekino-media.json"
            if manifest not in package.namelist():
                raise ValueError("expected the native media wheel produced by build_media.py")
            details = json.loads(package.read(manifest))
            if (details["architecture"] != "arm64" or details["pyav"]["version"] != "19.0.2"
                    or not {"--enable-videotoolbox", "--disable-gpl", "--disable-nonfree", "--disable-network"}
                    .issubset(details["ffmpeg"]["configure"])):
                raise ValueError("media wheel has incompatible build options")
    destination = destination.absolute()
    if destination.exists():
        raise FileExistsError(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=destination.parent) as stage:
        app = Path(stage) / destination.name
        contents = app / "Contents"
        resources = contents / "Resources"
        binary_dir = contents / "MacOS"
        binary_dir.mkdir(parents=True)
        (contents / "Info.plist").write_bytes(plistlib.dumps({
            "CFBundleExecutable": "SeekinoBackend",
            "CFBundleIdentifier": "app.seekino.backend-verifier",
            "CFBundleName": "SeekinoBackend",
            "CFBundlePackageType": "APPL",
            "CFBundleVersion": "1",
        }))
        frameworks = contents / "Frameworks"
        frameworks.mkdir()
        dylib = frameworks / "libpython3.12.dylib"
        original = Path(sysconfig.get_config_var("LIBDIR")) / dylib.name
        shutil.copy2(original, dylib)
        subprocess.run(["install_name_tool", "-id", "@rpath/" + dylib.name, dylib], check=True)
        subprocess.run(["codesign", "--force", "--sign", "-", dylib], check=True)
        stdlib = Path(sysconfig.get_path("stdlib"))
        shutil.copytree(stdlib, resources / "python/lib/python3.12",
                        ignore=shutil.ignore_patterns("site-packages", "__pycache__", "*.pyc", "test", "tests", "idlelib"))
        copy_business_files(resources)
        requirements = Path(stage) / "requirements.txt"
        exclude_av = ["--no-emit-package", "av"] if media_wheel is not None else []
        subprocess.run(["uv", "export", "--locked", "--only-group", "desktop", "--no-emit-project", *exclude_av,
                        "--format", "requirements.txt", "--output-file", requirements],
                       cwd=ROOT, check=True, stdout=subprocess.DEVNULL)
        subprocess.run(["uv", "pip", "install", "--python", sys.executable, "--no-deps", "--require-hashes",
                        "--only-binary", ":all:", "--target", resources / "site-packages", "-r", requirements], check=True)
        if media_wheel is not None:
            subprocess.run(["uv", "pip", "install", "--python", sys.executable, "--no-deps",
                            "--target", resources / "site-packages", media_wheel], check=True)
        exe = binary_dir / "SeekinoBackend"
        subprocess.run(["clang", str(ROOT / "deploy/desktop/host.c"), "-I" + sysconfig.get_path("include"),
                        "-L" + str(frameworks), "-lpython3.12", "-Wl,-rpath,@executable_path/../Frameworks",
                        "-o", exe], check=True)
        subprocess.run(["codesign", "--force", "--sign", "-", app], check=True)
        subprocess.run(["codesign", "--verify", "--strict", app], check=True)
        app.rename(destination)


if __name__ == "__main__":
    args = argparse.ArgumentParser()
    args.add_argument("destination", type=Path)
    media = args.add_mutually_exclusive_group(required=True)
    media.add_argument("--media-wheel", type=Path, help="wheel built by deploy/desktop/build_media.py")
    media.add_argument("--pypi-media", action="store_true", help="use the locked public wheel for development comparisons")
    options = args.parse_args()
    build(options.destination, options.media_wheel)
