"""Collect the desktop's Qt bindings, native platform plugins and QML modules."""
from pathlib import Path
import os
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[2]
MODULES = ("QtCore", "QtGui", "QtNetwork", "QtQml", "QtQuick", "QtQuickControls2", "QtMultimedia", "QtOpenGL")
# QML plugins resolve at runtime and do not appear in a Python import graph.
QML_DIRS = ("QtCore", "QtQml", "QtQml/Models", "QtQml/WorkerScript", "QtQuick", "QtQuick/Window",
            "QtQuick/Templates", "QtQuick/Controls", "QtQuick/Controls/impl", "QtQuick/Controls/Basic",
            "QtQuick/Controls/Basic/impl", "QtQuick/Layouts", "QtQuick/Dialogs", "QtQuick/Dialogs/quickimpl",
            "QtQuick/Dialogs/quickimpl/qml", "QtQuick/Effects", "QtQuick/Shapes", "Qt/labs/folderlistmodel", "Qt/labs/platform")


def install_qt(stage, windows=False):
    command = ["uv", "pip", "install", "--python-version", "3.12", "--target", stage, "--only-binary", ":all:",
               "--no-deps", "--require-hashes", "-r", ROOT / "deploy/desktop/qt-requirements.txt"]
    if windows:
        command += ["--python-platform", "x86_64-pc-windows-msvc"]
    subprocess.run(command, check=True)


def copy_frontend(resources):
    shutil.copytree(ROOT / "deploy/desktop/licenses", resources / "licenses/Qt")
    shutil.copytree(ROOT / "desktop", resources / "desktop", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy2(ROOT / "deploy/desktop/kino-app-icon.png", resources / "SeekinoLogo.png")


def collect(wheels, packages, frameworks=None):
    """Keep Qt's logical wheel layout. Place Mach-O code in the app's signed Frameworks directory."""
    windows = frameworks is None
    side = wheels / "PySide6"
    qt = side if windows else side / "Qt"
    extension = ".pyd" if windows else ".abi3.so"
    seeds = [side / (name + extension) for name in MODULES]
    seeds += [wheels / "shiboken6" / ("Shiboken" + extension)]
    plugins = (("platforms/qwindows", "platforms/qoffscreen", "multimedia/windowsmediaplugin",
                "imageformats/qjpeg", "imageformats/qsvg") if windows else
               ("platforms/libqcocoa", "platforms/libqoffscreen", "multimedia/libdarwinmediaplugin",
                "imageformats/libqjpeg", "imageformats/libqsvg"))
    seeds += [qt / "plugins" / (name + (".dll" if windows else ".dylib")) for name in plugins]
    for directory in QML_DIRS:
        seeds += [file for file in (qt / "qml" / directory).iterdir()
                  if file.is_file() and file.suffix != ".qmltypes"]
    for package in (side, wheels / "shiboken6"):
        seeds += list(package.glob("*.py"))
        # LGPL / third-party notices are included when present in the official wheels.
    for info in wheels.glob("*.dist-info"):
        if info.name.lower().startswith(("shiboken6-", "pyside6")):
            shutil.copytree(info, packages / info.name, dirs_exist_ok=True)
    if windows:
        # Windows may dynamically select WARP/software rendering without importing it statically.
        seeds += list(side.glob("opengl32sw.dll"))
        seeds += list(side.glob("d3dcompiler*.dll"))
        from pefile import PE, DIRECTORY_ENTRY
        known = {p.name.lower(): p for d in (wheels / "shiboken6", side) for p in d.glob("*.dll")}
    seen = set()
    signatures = []
    while seeds:
        src = seeds.pop()
        if src in seen:
            continue
        seen.add(src)
        relative = src.relative_to(wheels)
        target = packages / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if windows:
            shutil.copy2(src, target)
            if src.suffix.lower() in (".pyd", ".dll"):
                with PE(str(src), fast_load=True) as pe:
                    pe.parse_data_directories(directories=[DIRECTORY_ENTRY['IMAGE_DIRECTORY_ENTRY_IMPORT'],
                                                           DIRECTORY_ENTRY['IMAGE_DIRECTORY_ENTRY_DELAY_IMPORT']])
                    for group in ("DIRECTORY_ENTRY_IMPORT", "DIRECTORY_ENTRY_DELAY_IMPORT"):
                        for imported in getattr(pe, group, ()):
                            dependency = known.get(imported.dll.decode("ascii").lower())
                            if dependency:
                                seeds.append(dependency)
            continue
        framework = next((parent for parent in src.parents if parent.suffix == ".framework"), None)
        if framework:
            target = frameworks / framework.name / src.relative_to(framework)
            ver = target.parent
            ver.mkdir(parents=True)
            shutil.copytree(src.parent / "Resources", ver / "Resources")
            os.symlink("A", ver.parent / "Current")
            os.symlink("Versions/Current/Resources", ver.parent.parent / "Resources")
            os.symlink("Versions/Current/" + src.name, ver.parent.parent / src.name)
            signatures.append(ver.parent.parent)
        elif src.suffix in (".so", ".dylib"):
            native = frameworks / ("-".join(relative.parts[1:]) if target.name.startswith("libqt") else src.name)
            if native.exists():
                raise RuntimeError(f"duplicate native module {native}")
            target.symlink_to(os.path.relpath(native, target.parent))
            target = native
            signatures.append(target)
        else:
            shutil.copy2(src, target)
            continue
        subprocess.run(["lipo", src, "-thin", "arm64", "-output", target], check=True)
        links = subprocess.check_output(["otool", "-L", target], text=True).splitlines()[1:]
        for entry in links:
            dep = entry.strip().split(" (compatibility")[0]
            if dep.startswith(("/usr/lib/", "/System/Library/")):
                continue
            if not dep.startswith("@rpath/"):
                raise ValueError(f"unexpected library path in {src.name}: {dep}")
            dep = dep.removeprefix("@rpath/")
            if ".framework/" in dep:
                seeds.append(qt / "lib" / dep)
            else:
                candidates = [folder / dep for folder in (side, wheels / "shiboken6", qt / "lib")]
                seeds.append(next(p for p in candidates if p.is_file()))
    return signatures
