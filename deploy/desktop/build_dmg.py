"""Package a signed Seekino.app as a drag-to-Applications disk image."""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory


def build(app, destination, *, identity, notary_profile):
    if app.name != "Seekino.app" or not app.is_dir():
        raise ValueError("expected a built Seekino.app")
    if destination.suffix != ".dmg" or destination.exists():
        raise ValueError("choose a new .dmg output path")
    if bool(notary_profile) != bool(identity):
        raise ValueError("supply both a Developer ID Application identity and a notary profile for distribution")
    subprocess.run(["codesign", "--verify", "--deep", "--strict", app], check=True)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=destination.parent) as temporary:
        root = Path(temporary)
        volume = root / "volume"
        volume.mkdir()
        shutil.copytree(app, volume / app.name, symlinks=True)
        os.symlink("/Applications", volume / "Applications")
        image = root / destination.name
        subprocess.run(["hdiutil", "create", "-volname", "Seekino", "-fs", "HFS+", "-format", "UDZO",
                        "-srcfolder", volume, image], check=True)
        if identity:
            subprocess.run(["codesign", "--sign", identity, "--timestamp", image], check=True)
            subprocess.run(["xcrun", "notarytool", "submit", image, "--keychain-profile", notary_profile,
                            "--wait"], check=True)
            subprocess.run(["xcrun", "stapler", "staple", image], check=True)
            subprocess.run(["xcrun", "stapler", "validate", image], check=True)
        image.rename(destination)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("app", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--identity")
    parser.add_argument("--notary-profile")
    args = parser.parse_args()
    build(args.app.resolve(), args.destination.absolute(),
          identity=args.identity, notary_profile=args.notary_profile)
