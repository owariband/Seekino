"""Check a clean release checkout before merging into main."""
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import unquote, urlsplit


def git_paths(*args):
    data = subprocess.check_output(["git", *args, "-z"])
    return {Path(name.decode("utf-8", "surrogateescape")) for name in data.split(b"\0") if name}


def main():
    tracked = git_paths("ls-files", "--cached")
    ignored = git_paths("ls-files", "--cached", "--ignored", "--exclude-per-directory=.gitignore")
    problems = [f"Tracked but ignored: {path}" for path in sorted(ignored)]

    # A dev merge can also replace the release .gitignore. Exercise its actual rules.
    private_examples = (
        "docs/design/example.md", "docs/report/example.json", "data/mysql/example",
        "training/example.py", "evals/example.py", ".agents/skills/example/SKILL.md",
        "assets/library/example.mp4",
    )
    covered = subprocess.run(
        ["git", "check-ignore", "--no-index", "-z", "--stdin"],
        input=b"\0".join(path.encode() for path in private_examples) + b"\0",
        stdout=subprocess.PIPE, check=False,
    )
    if covered.returncode not in (0, 1):
        return covered.returncode
    matched = set(covered.stdout.decode().split("\0"))
    problems.extend(f"Missing release ignore rule for {path}" for path in private_examples if path not in matched)

    guide_dir = Path("docs/guide")
    for lang in ("zh", "en"):
        for name in ("README.md", "start.md"):
            if guide_dir / lang / name not in tracked:
                problems.append(f"Missing public guide: {guide_dir / lang / name}")
    for guide in sorted(path for path in tracked if guide_dir in path.parents and path.suffix == ".md"):
        for href in re.findall(r'\]\(([^\s)]+)\)', guide.read_text()):
            target = urlsplit(href)
            if target.scheme or target.netloc or not target.path or target.path.startswith("/"):
                continue
            linked = (guide.parent / unquote(target.path)).resolve()
            if not linked.is_relative_to(Path.cwd()) or linked.relative_to(Path.cwd()) not in tracked:
                problems.append(f"{guide} links to an unpublished file: {href}")

    # Publish README images, linked drawing sources and assets used by the web build.
    assets = {path for path in tracked if path.parts[0] == "assets"}
    used = set()
    for readme in (Path("README.md"), Path("README.en.md")):
        for raw in re.findall(r'(?:\]\(|(?:src|href)=["\'])(assets/[^)"\'\n]+)', readme.read_text()):
            path = Path(unquote(raw))
            if path not in assets and not any(path in f.parents for f in assets):
                problems.append(f"{readme} links to an unpublished asset: {path}")
            used.add(path)
    for link in tracked:
        if link.parts[:2] == ("web", "public") and link.is_symlink():
            if not link.is_file():
                problems.append(f"Missing web asset: {link}")
            elif link.resolve().is_relative_to(Path.cwd()):
                used.add(link.resolve().relative_to(Path.cwd()))
    for asset in sorted(assets - used):
        if asset.suffix != ".excalidraw" or asset.with_suffix(".png") not in used:
            problems.append(f"Unreferenced release asset: {asset}")
    if problems:
        print("\n".join(problems), file=sys.stderr)
        return 1
    print(f"Verified {len(tracked)} tracked paths and {len(assets)} release assets.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
