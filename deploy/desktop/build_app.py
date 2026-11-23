"""Build the Apple Silicon application from the desktop runtime and native sources."""
import argparse
from collections import Counter
from pathlib import Path
import plistlib
import shutil
import subprocess
import sysconfig
from tempfile import TemporaryDirectory

from PIL import Image

from build_host import ROOT, build as build_host
from qt_bundle import collect, copy_frontend, install_qt

MINIMUM_MACOS = "15.0"


def _run(*command):
    subprocess.run(command, check=True)


def _output(*command):
    return subprocess.check_output(command, text=True).strip()


def _place_code(contents):
    """Keep wheel import paths via relative symlinks and sign Mach-O files in Frameworks."""
    frameworks = contents / "Frameworks"
    resources = contents / "Resources"
    stdlib = resources / "python" / "lib" / "python3.12"
    shutil.rmtree(stdlib / "tkinter", ignore_errors=True)
    for unused in stdlib.glob("lib-dynload/_tkinter*.so"):
        unused.unlink()
    nested = sorted(path for path in resources.rglob("*")
                    if path.suffix in {".so", ".dylib"} and path.is_file())
    same_names = Counter(path.name for path in nested)
    for path in nested:
        filename = path.name if same_names[path.name] == 1 else (
            "-".join(path.relative_to(resources).parts[1:])
        )
        target = frameworks / filename
        if target.exists():
            raise RuntimeError(f"duplicate native library name: {filename}")
        if _output("lipo", "-archs", path) == "arm64":
            path.rename(target)
        else:
            _run("lipo", "-thin", "arm64", path, "-output", target)
            path.unlink()
        path.symlink_to(Path(*([".."] * len(path.parent.relative_to(contents).parts))) / "Frameworks" / filename)

    libraries = {path.name: path for path in frameworks.iterdir()}
    for path in libraries.values():
        dependencies = _output("otool", "-L", path).splitlines()[1:]
        identities = _output("otool", "-D", path).splitlines()[1:]
        changes = ["-id", f"@rpath/{path.name}"] if identities else []
        for line in dependencies:
            name = line.strip().split(" (compatibility", 1)[0]
            if name in identities or name.startswith(("/usr/lib/", "/System/Library/")):
                continue
            target = libraries.get(Path(name).name)
            if target is None:
                raise RuntimeError(f"{path.name} has an unpackaged dependency: {name}")
            if name != f"@rpath/{target.name}":
                changes += ["-change", name, f"@rpath/{target.name}"]
        if changes:
            _run("install_name_tool", *changes, path)
    return sorted(libraries.values())


def _icon(resources):
    runtime_icon = resources / "SeekinoLogo.png"
    with Image.open(runtime_icon) as source:
        # Match the optical inset of macOS system icons inside the ICNS canvas.
        artwork = source.convert("RGBA").resize((896, 896), Image.Resampling.LANCZOS)
    icon = Image.new("RGBA", (1024, 1024))
    icon.alpha_composite(artwork, (64, 64))
    icon.save(runtime_icon)
    icon.save(resources / "Seekino.icns")


def build(destination, wheel, version, identity):
    destination = destination.absolute()
    if destination.suffix != ".app":
        raise ValueError("the destination must end in .app")
    if destination.exists():
        raise FileExistsError(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=destination.parent) as tmp:
        stage = Path(tmp)
        app = stage / destination.name
        build_host(app, wheel)
        contents = app / "Contents"
        resources = contents / "Resources"
        frameworks = contents / "Frameworks"
        executable = contents / "MacOS" / "Seekino"
        (executable.parent / "SeekinoBackend").unlink()
        shutil.rmtree(contents / "_CodeSignature")
        (contents / "Info.plist").write_bytes(plistlib.dumps({
            "CFBundleDevelopmentRegion": "zh-Hans",
            "CFBundleExecutable": "Seekino",
            "CFBundleIconFile": "Seekino.icns",
            "CFBundleIdentifier": "app.seekino.desktop",
            "CFBundleName": "Seekino",
            "CFBundleDisplayName": "Seekino",
            "CFBundlePackageType": "APPL",
            "CFBundleShortVersionString": version,
            "CFBundleVersion": version,
            "LSMinimumSystemVersion": MINIMUM_MACOS,
            "NSHighResolutionCapable": True,
        }))
        libraries = _place_code(contents)
        copy_frontend(resources)
        _icon(resources)
        qt_wheels = stage / "qt"
        install_qt(qt_wheels)
        libraries += collect(qt_wheels, resources / "site-packages", frameworks)
        _run("xcrun", "clang", "-arch", "arm64", f"-mmacosx-version-min={MINIMUM_MACOS}",
             "-O2", "-Wall", "-Werror", "-I", sysconfig.get_path("include"),
             '-DSEEKINO_ENTRY=L"desktop.main"', ROOT / "deploy/desktop/host.c",
             "-L", frameworks, "-lpython3.12", "-Wl,-rpath,@executable_path/../Frameworks", "-o", executable)
        signature = ["--force", "--sign", identity]
        if identity != "-":
            signature += ["--timestamp"]
        for library in libraries:
            _run("codesign", *signature, library)
        _run("codesign", *signature, *(["--options", "runtime"] if identity != "-" else []), app)
        _run("codesign", "--verify", "--deep", "--strict", app)
        app.rename(destination)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--media-wheel", required=True, type=Path)
    parser.add_argument("--version", default="0.2.3")
    parser.add_argument("--identity", default="-", help="Developer ID Application identity for release signing")
    options = parser.parse_args()
    build(options.destination, options.media_wheel, options.version, options.identity)
