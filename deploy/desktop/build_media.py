"""Build a relocatable arm64 PyAV wheel against a source-built FFmpeg."""

import argparse
import ctypes
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tomllib
from importlib.metadata import version
from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[2]
FFMPEG_VERSION = "9.0.2"
FFMPEG_SHA256 = "8c3850283eb25fa026482078a04051e0be17347b09ef81a0849bec15a96e002e"
LIBRARIES = {"libavcodec", "libavdevice", "libavfilter", "libavformat", "libavutil", "libswresample", "libswscale"}
TARGET = "11.0"


def extract(archive: Path, expected_sha256: str, destination: Path) -> None:
    with archive.open("rb") as source:
        if hashlib.file_digest(source, "sha256").hexdigest() != expected_sha256:
            raise ValueError(f"unexpected source digest for {archive.name}")
    with tarfile.open(archive) as source:
        source.extractall(destination, filter="data")


def audit_wheel(unpacked: Path) -> list[str]:
    from delocate.tools import get_install_names

    libs = sorted((unpacked / "av/.dylibs").glob("*.dylib"))
    if {lib.name.split(".")[0] for lib in libs} != LIBRARIES or len(libs) != len(LIBRARIES):
        raise ValueError(f"unexpected bundled media libraries: {[lib.name for lib in libs]}")
    for binary in [*libs, *unpacked.rglob("*.so")]:
        subprocess.run(["lipo", binary, "-verify_arch", "arm64"], check=True)
        for dep in get_install_names(str(binary)):
            if dep.startswith(("/usr/lib/", "/System/Library/Frameworks/")):
                continue
            if dep.startswith("@loader_path/"):
                resolved = (binary.parent / dep.removeprefix("@loader_path/")).resolve()
                if resolved in libs:
                    continue
            raise ValueError(f"{binary.name} links to a library outside the wheel: {dep}")
    # Loading the relocated codec verifies that dyld can resolve its dependencies.
    codec = ctypes.CDLL(str(next(lib for lib in libs if lib.name.startswith("libavcodec."))))
    codec.avcodec_configuration.restype = ctypes.c_char_p
    configured = set(codec.avcodec_configuration().decode().split())
    required = {"--enable-shared", "--enable-videotoolbox", "--disable-gpl", "--disable-version3",
                "--disable-nonfree", "--disable-autodetect", "--disable-programs"}
    if required - configured:
        raise ValueError(f"FFmpeg configuration is missing {sorted(required - configured)}")
    codec.avcodec_find_encoder_by_name.argtypes = [ctypes.c_char_p]
    codec.avcodec_find_encoder_by_name.restype = ctypes.c_void_p
    if not codec.avcodec_find_encoder_by_name(b"h264_videotoolbox"):
        raise ValueError("the bundled FFmpeg lacks the desktop VideoToolbox encoder")
    return [lib.name for lib in libs]


def build(sources: Path, wheel_dir: Path, jobs: int) -> Path:
    if (sys.platform, platform.machine(), sys.version_info[:2]) != ("darwin", "arm64", (3, 12)):
        raise RuntimeError("build on Apple Silicon using the project CPython 3.12")
    if jobs < 1:
        raise ValueError("jobs must be positive")
    wheel_dir = wheel_dir.resolve()
    wheel_dir.mkdir(parents=True, exist_ok=True)
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    av = next(package for package in lock["package"] if package["name"] == "av")
    source_url, av_digest = av["sdist"]["url"], av["sdist"]["hash"].removeprefix("sha256:")
    env = {**os.environ, "MACOSX_DEPLOYMENT_TARGET": TARGET, "ARCHFLAGS": "-arch arm64"}
    ffmpeg_archive = f"ffmpeg-{FFMPEG_VERSION}.tar.xz"
    with TemporaryDirectory(prefix="seekino-media-") as directory:
        work = Path(directory).resolve()
        extract(sources / ffmpeg_archive, FFMPEG_SHA256, work)
        extract(sources / source_url.rsplit("/", 1)[-1], av_digest, work)
        ffmpeg = work / f"ffmpeg-{FFMPEG_VERSION}"
        pyav = work / f"av-{av['version']}"
        prefix = work / "ffmpeg-install"
        flags = [
            "--cc=clang", "--arch=arm64", "--enable-shared", "--disable-static",
            "--disable-programs", "--disable-doc", "--disable-debug", "--disable-autodetect",
            "--disable-network", "--disable-gpl", "--disable-version3", "--disable-nonfree",
            "--enable-videotoolbox", "--enable-zlib", "--enable-bzlib",
        ]
        subprocess.run([ffmpeg / "configure", f"--prefix={prefix}", *flags], cwd=ffmpeg, env=env, check=True)
        subprocess.run(["make", f"-j{jobs}"], cwd=ffmpeg, env=env, check=True)
        subprocess.run(["make", "install"], cwd=ffmpeg, env=env, check=True)
        raw = work / "raw"
        subprocess.run([sys.executable, "setup.py", f"--ffmpeg-dir={prefix}", "build_ext", "-j", str(jobs),
                        "bdist_wheel", "--dist-dir", raw], cwd=pyav, env=env, check=True)
        repaired = work / "repaired"
        delocate = Path(sys.executable).parent / "delocate-wheel"
        subprocess.run([delocate, "--require-archs", "arm64", "--sanitize-rpaths", "-w", repaired,
                        next(raw.glob("*.whl"))], env=env, check=True)
        wheel = next(repaired.glob("*.whl"))
        unpacked = work / "unpacked"
        with ZipFile(wheel) as src:
            src.extractall(unpacked)
        libraries = audit_wheel(unpacked)
        info = next(unpacked.glob("*.dist-info"))
        license_dir = info / "licenses/FFmpeg"
        license_dir.mkdir(parents=True)
        for name in ("COPYING.LGPLv2.1", "LICENSE.md"):
            shutil.copy2(ffmpeg / name, license_dir / name)
        details = {
            "ffmpeg": {"version": FFMPEG_VERSION, "source": f"https://ffmpeg.org/releases/{ffmpeg_archive}",
                       "sha256": FFMPEG_SHA256, "configure": flags, "libraries": libraries},
            "pyav": {"version": av["version"], "source": source_url, "sha256": av_digest},
            "tools": {package: version(package) for package in ("cython", "setuptools", "wheel", "delocate")},
            "architecture": "arm64", "macos_deployment_target": TARGET,
        }
        (info / "seekino-media.json").write_text(json.dumps(details, indent=2) + "\n", encoding="utf-8")
        if (wheel_dir / wheel.name).exists():
            raise FileExistsError(wheel_dir / wheel.name)
        # Repacking recalculates the wheel RECORD including the bundled notices.
        subprocess.run([sys.executable, "-m", "wheel", "pack", unpacked, "-d", wheel_dir], env=env, check=True)
        return wheel_dir / wheel.name


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sources", type=Path, required=True, help="directory containing both pinned source archives")
    parser.add_argument("--wheel-dir", type=Path, required=True)
    parser.add_argument("--jobs", type=int, default=8)
    args = parser.parse_args()
    print(build(args.sources, args.wheel_dir, args.jobs))
