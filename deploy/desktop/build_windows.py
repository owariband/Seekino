"""Build a self-contained x64 Windows desktop preview with the same QML and Python entry."""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
from zipfile import ZIP_DEFLATED, ZipFile

from pefile import PE, RESOURCE_TYPE
from PIL import Image
from build_host import ROOT, copy_business_files
from qwen_runtime import install_qwen_runtime
from startup_bytecode import compile_startup_bytecode
from qt_bundle import collect, copy_frontend, install_qt


def build(destination, python, zig, rc=None):
    destination = destination.absolute()
    if destination.suffix.lower() != ".zip" or destination.exists():
        raise ValueError("choose a new output .zip")
    if not (python / "python312.dll").is_file():
        raise ValueError("supply the x86_64 Windows CPython 3.12 install directory from uv")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=destination.parent) as tmp:
        stage = Path(tmp)
        app = stage / "Seekino"
        app.mkdir()
        for file in [*python.glob("*.dll"), python / "LICENSE.txt"]:
            shutil.copy2(file, app)
        for name in ("Lib", "DLLs"):
            shutil.copytree(python / name, app / name, ignore=shutil.ignore_patterns(
                "site-packages", "__pycache__", "test", "tests", "_test*", "*tkinter*", "tcl*", "tk86*", "idlelib",
                "venv", "ensurepip",
            ))
        packages = app / "site-packages"
        (app / "python312._pth").write_text(".\nLib\nDLLs\nsite-packages\n", encoding="ascii")
        copy_business_files(app)
        copy_frontend(app)
        requirements = stage / "requirements.txt"
        subprocess.run(["uv", "export", "--locked", "--only-group", "desktop", "--no-emit-project",
                        "--output-file", requirements, "--quiet"], cwd=ROOT, check=True)
        subprocess.run(["uv", "pip", "install", "--python-platform", "x86_64-pc-windows-msvc", "--python-version",
                        "3.12.16", "--target", packages, "--no-deps", "--only-binary", ":all:", "--require-hashes",
                        "-r", requirements], check=True)
        for name in ("bin", "Scripts"):
            shutil.rmtree(packages / name, ignore_errors=True)
        qt = stage / "qt"
        install_qt(qt, windows=True)
        collect(qt, packages)
        install_qwen_runtime(app, app / "qwen-runtime", windows=True, zig=zig)
        compile_startup_bytecode(app, windows=True)
        icon = stage / "Seekino.ico"
        with Image.open(app / "SeekinoLogo.png") as image:
            image.convert("RGBA").save(icon)
        (stage / "icon.rc").write_text('1 ICON "Seekino.ico"\n', encoding="ascii")
        env = os.environ.copy()
        env.setdefault("ZIG_GLOBAL_CACHE_DIR", str(stage / "zig-cache"))
        resource_compiler = [rc] if rc is not None else [zig, "rc"]
        subprocess.run([*resource_compiler, "/fo", "icon.res", "icon.rc"], cwd=stage, env=env, check=True)
        subprocess.run([zig, "cc", "-target", "x86_64-windows-gnu", "-O2", "-municode", "-Wl,--subsystem,windows", "-s",
                        ROOT / "deploy/desktop/windows_host.c", stage / "icon.res", "-lshell32", "-luser32",
                        "-o", app / "Seekino.exe"], cwd=stage, env=env, check=True)
        with PE(str(app / "Seekino.exe")) as host:
            icons = {item.id for item in host.DIRECTORY_ENTRY_RESOURCE.entries}
            if (host.FILE_HEADER.Machine != 0x8664 or host.OPTIONAL_HEADER.Subsystem != 2
                    or not host.OPTIONAL_HEADER.AddressOfEntryPoint or RESOURCE_TYPE["RT_GROUP_ICON"] not in icons):
                raise ValueError("expected an x64 Windows GUI executable with the Seekino app icon")
        staged_zip = stage / destination.name
        with ZipFile(staged_zip, "w", ZIP_DEFLATED, compresslevel=6) as package:
            for path in sorted(app.rglob("*")):
                if path.is_file():
                    package.write(path, path.relative_to(stage))
        staged_zip.rename(destination)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--python-runtime", required=True, type=Path)
    parser.add_argument("--zig", default="zig")
    parser.add_argument("--rc", help="optional MS-compatible resource compiler, e.g. llvm-rc")
    args = parser.parse_args()
    build(args.destination, args.python_runtime.resolve(), args.zig, args.rc)
