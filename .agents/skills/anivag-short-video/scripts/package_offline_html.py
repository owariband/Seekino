#!/usr/bin/env python3
"""Package one HyperFrames project as a self-contained offline HTML film."""

from __future__ import annotations

import argparse
import base64
import html
import json
import mimetypes
import re
import sys
from collections import OrderedDict
from pathlib import Path
from typing import Any, Match, Optional, Union
from urllib.parse import unquote, urljoin, urlparse
from urllib.request import Request, urlopen


PathOrUrl = Union[Path, str]

SCRIPT_RE = re.compile(r"<script\b(?P<attrs>[^>]*)>(?P<body>.*?)</script\s*>", re.I | re.S)
STYLE_RE = re.compile(r"<style\b(?P<attrs>[^>]*)>(?P<body>.*?)</style\s*>", re.I | re.S)
LINK_RE = re.compile(r"<link\b(?P<attrs>[^>]*)>", re.I | re.S)
ATTR_RE = re.compile(
    r"(?P<space>\s+)(?P<name>[\w:-]+)\s*=\s*(?P<quote>['\"])(?P<value>.*?)(?P=quote)",
    re.I | re.S,
)
CSS_URL_RE = re.compile(
    r"url\(\s*(?P<quote>['\"]?)(?P<value>.*?)(?P=quote)\s*\)", re.I | re.S
)
CSS_IMPORT_RE = re.compile(
    r"@import\s+(?:url\(\s*)?(?P<quote>['\"])(?P<value>.*?)(?P=quote)\s*\)?\s*;",
    re.I,
)
FETCH_RE = re.compile(
    r"\b(?:fetch|import)\s*\(\s*(?P<quote>['\"])(?P<value>.*?)(?P=quote)", re.I | re.S
)
VERSION_RE = re.compile(r"\bhyperframes@(?P<version>\d+\.\d+\.\d+(?:[-+][\w.-]+)?)")
PLAYER_RUNTIME_URL_RE = re.compile(
    r"https://cdn\.jsdelivr\.net/npm/@hyperframes/core@[^/]+/dist/hyperframe\.runtime\.iife\.js"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project", type=Path, help="HyperFrames project directory")
    parser.add_argument("--entry", default="index.html", help="Project-relative entry composition")
    parser.add_argument("--output", type=Path, required=True, help="Destination .html file")
    parser.add_argument("--title", help="Browser title; defaults to the project name")
    parser.add_argument(
        "--hyperframes-dist",
        type=Path,
        help="Directory containing hyperframes-player.global.js and hyperframe.runtime.iife.js",
    )
    parser.add_argument(
        "--metadata",
        type=Path,
        action="append",
        default=[],
        help="JSON file to embed for provenance; may be repeated",
    )
    parser.add_argument("--fetch-timeout", type=float, default=45.0)
    return parser.parse_args()


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def script_safe(source: str) -> str:
    return re.sub(r"</script", r"<\\/script", source, flags=re.I)


def mime_for(name: str, header: Optional[str] = None) -> str:
    if header:
        value = header.split(";", 1)[0].strip()
        if value and value != "application/octet-stream":
            return value
    return mimetypes.guess_type(name)[0] or "application/octet-stream"


def package_version(dist: Path) -> Optional[str]:
    package_json = dist.parent / "package.json"
    try:
        value = json.loads(package_json.read_text())
    except (OSError, ValueError):
        return None
    version = value.get("version")
    return str(version) if version else None


def requested_hyperframes_version(project: Path) -> Optional[str]:
    package_json = project / "package.json"
    try:
        source = package_json.read_text()
    except OSError:
        return None
    match = VERSION_RE.search(source)
    return match.group("version") if match else None


def locate_hyperframes_dist(project: Path, explicit: Optional[Path]) -> Path:
    expected = requested_hyperframes_version(project)
    candidates = []
    if explicit:
        candidates.append(explicit.expanduser())
    candidates.extend(
        [
            project / "node_modules" / "hyperframes" / "dist",
            Path.cwd() / "node_modules" / "hyperframes" / "dist",
        ]
    )
    candidates.extend(
        Path.home().glob(".npm/_npx/*/node_modules/hyperframes/dist")
    )
    valid = []
    for candidate in candidates:
        candidate = candidate.resolve()
        if candidate in valid:
            continue
        if (candidate / "hyperframes-player.global.js").is_file() and (
            candidate / "hyperframe.runtime.iife.js"
        ).is_file():
            valid.append(candidate)
    if not valid:
        raise RuntimeError(
            "HyperFrames runtime not found. Run the pinned HyperFrames CLI once or pass "
            "--hyperframes-dist."
        )
    if expected:
        matches = [candidate for candidate in valid if package_version(candidate) == expected]
        if matches:
            return max(matches, key=lambda item: item.stat().st_mtime)
        found = sorted({package_version(candidate) or "unknown" for candidate in valid})
        raise RuntimeError(
            f"HyperFrames {expected} is pinned by the project, but only {found} were found"
        )
    return max(valid, key=lambda item: item.stat().st_mtime)


class OfflinePacker:
    def __init__(self, project: Path, fetch_timeout: float) -> None:
        self.project = project.resolve()
        self.fetch_timeout = fetch_timeout
        self.assets: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
        self.asset_keys: dict[str, str] = {}
        self.compositions: "OrderedDict[str, str]" = OrderedDict()
        self.composition_tokens: dict[Path, str] = {}
        self.composition_order: list[str] = []
        self.active_compositions: set[Path] = set()
        self.remote_cache: dict[str, tuple[bytes, Optional[str]]] = {}

    @staticmethod
    def passthrough(value: str) -> bool:
        lowered = value.strip().lower()
        return lowered.startswith(("data:", "blob:", "#", "mailto:", "tel:", "javascript:"))

    def resolve_local(self, value: str, base: Path) -> Path:
        parsed = urlparse(value)
        if parsed.scheme == "file":
            path = Path(unquote(parsed.path))
            if path.is_file():
                return path.resolve()
            raise FileNotFoundError(path)
        clean = unquote(parsed.path)
        if clean.startswith("/"):
            candidates = [self.project / clean.lstrip("/")]
        else:
            candidates = [self.project / clean, base.parent / clean]
        for candidate in candidates:
            if candidate.is_file():
                return candidate.resolve()
        raise FileNotFoundError(
            f"Local asset {value!r} was not found from project root or {base.parent}"
        )

    def resolve_reference(self, value: str, base: PathOrUrl) -> PathOrUrl:
        value = html.unescape(value.strip())
        if isinstance(base, str):
            return urljoin(base, value)
        parsed = urlparse(value)
        if parsed.scheme in {"http", "https"}:
            return value
        if value.startswith("//"):
            return "https:" + value
        return self.resolve_local(value, base)

    def read_remote(self, url: str) -> tuple[bytes, Optional[str]]:
        cached = self.remote_cache.get(url)
        if cached is not None:
            return cached
        request = Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 AniVAG-Offline-Packager/1.0",
                "Accept": "*/*",
            },
        )
        with urlopen(request, timeout=self.fetch_timeout) as response:
            result = (response.read(), response.headers.get("Content-Type"))
        self.remote_cache[url] = result
        return result

    def read_bytes(self, reference: PathOrUrl) -> tuple[bytes, Optional[str], str]:
        if isinstance(reference, Path):
            return reference.read_bytes(), None, str(reference)
        data, content_type = self.read_remote(reference)
        return data, content_type, reference

    def read_text(self, reference: PathOrUrl) -> str:
        data, content_type, label = self.read_bytes(reference)
        charset_match = re.search(r"charset=([\w.-]+)", content_type or "", re.I)
        charset = charset_match.group(1) if charset_match else "utf-8"
        try:
            return data.decode(charset)
        except (LookupError, UnicodeDecodeError) as exc:
            raise RuntimeError(f"Unable to decode text resource {label}: {exc}") from exc

    def asset_token(self, value: str, base: PathOrUrl) -> str:
        if self.passthrough(value):
            return value
        reference = self.resolve_reference(value, base)
        key = str(reference)
        existing = self.asset_keys.get(key)
        if existing:
            return existing
        data, content_type, label = self.read_bytes(reference)
        token = f"__ANIVAG_OFFLINE_ASSET_{len(self.assets) + 1:04d}__"
        name = reference.name if isinstance(reference, Path) else urlparse(reference).path
        self.assets[token] = {
            "mime": mime_for(name, content_type),
            "base64": b64(data),
            "bytes": len(data),
            "label": label,
        }
        self.asset_keys[key] = token
        return token

    def process_css(self, source: str, base: PathOrUrl) -> str:
        def replace_import(match: Match[str]) -> str:
            value = match.group("value")
            if self.passthrough(value):
                return match.group(0)
            reference = self.resolve_reference(value, base)
            imported = self.read_text(reference)
            return self.process_css(imported, reference)

        source = CSS_IMPORT_RE.sub(replace_import, source)

        def replace_url(match: Match[str]) -> str:
            value = match.group("value").strip()
            if self.passthrough(value):
                return match.group(0)
            return f'url("{self.asset_token(value, base)}")'

        return CSS_URL_RE.sub(replace_url, source)

    @staticmethod
    def attrs(attrs_source: str) -> list[tuple[str, str]]:
        return [
            (match.group("name").lower(), html.unescape(match.group("value")))
            for match in ATTR_RE.finditer(attrs_source)
        ]

    @staticmethod
    def remove_attr(attrs_source: str, name: str) -> str:
        return re.sub(
            rf"\s+{re.escape(name)}\s*=\s*(['\"]).*?\1",
            "",
            attrs_source,
            flags=re.I | re.S,
        )

    def process_scripts(self, source: str, base: Path) -> str:
        def replace(match: Match[str]) -> str:
            attrs_source = match.group("attrs")
            attrs = dict(self.attrs(attrs_source))
            value = attrs.get("src")
            if not value:
                return match.group(0)
            if self.passthrough(value):
                raise RuntimeError(f"Script src cannot remain dynamic in offline output: {value}")
            reference = self.resolve_reference(value, base)
            script = script_safe(self.read_text(reference))
            clean_attrs = self.remove_attr(attrs_source, "src")
            return f"<script{clean_attrs}>{script}</script>"

        return SCRIPT_RE.sub(replace, source)

    def process_styles(self, source: str, base: Path) -> str:
        def replace_link(match: Match[str]) -> str:
            attrs = dict(self.attrs(match.group("attrs")))
            rel = {part.lower() for part in attrs.get("rel", "").split()}
            if "stylesheet" not in rel:
                return match.group(0)
            value = attrs.get("href")
            if not value or self.passthrough(value):
                raise RuntimeError(f"Stylesheet href cannot be packaged: {value!r}")
            reference = self.resolve_reference(value, base)
            css = self.process_css(self.read_text(reference), reference)
            return f"<style>{css}</style>"

        source = LINK_RE.sub(replace_link, source)

        def replace_style(match: Match[str]) -> str:
            css = self.process_css(match.group("body"), base)
            return f'<style{match.group("attrs")}>{css}</style>'

        return STYLE_RE.sub(replace_style, source)

    def process_composition_refs(self, source: str, base: Path) -> str:
        def replace(match: Match[str]) -> str:
            name = match.group("name")
            if name.lower() != "data-composition-src":
                return match.group(0)
            value = html.unescape(match.group("value"))
            if self.passthrough(value):
                return match.group(0)
            reference = self.resolve_reference(value, base)
            if not isinstance(reference, Path):
                raise RuntimeError(
                    "Remote nested compositions are unsupported; copy them into the project first"
                )
            token = self.pack_nested_composition(reference)
            quote = match.group("quote")
            return f'{match.group("space")}{name}={quote}{token}{quote}'

        return ATTR_RE.sub(replace, source)

    def process_asset_attrs(self, source: str, base: Path) -> str:
        def replace(match: Match[str]) -> str:
            name = match.group("name")
            lowered = name.lower()
            value = html.unescape(match.group("value"))
            if lowered in {"src", "poster"}:
                value = self.asset_token(value, base)
            elif lowered == "srcset":
                entries = []
                for entry in value.split(","):
                    pieces = entry.strip().split()
                    if pieces:
                        pieces[0] = self.asset_token(pieces[0], base)
                    entries.append(" ".join(pieces))
                value = ", ".join(entries)
            elif lowered == "style":
                value = self.process_css(value, base)
            else:
                return match.group(0)
            quote = match.group("quote")
            return f'{match.group("space")}{name}={quote}{value}{quote}'

        return ATTR_RE.sub(replace, source)

    def validate_source(self, source: str, label: str) -> None:
        for match in ATTR_RE.finditer(source):
            name = match.group("name").lower()
            value = html.unescape(match.group("value")).strip()
            if name in {"src", "poster", "srcset", "data-composition-src"} and re.match(
                r"^(?:https?:|//|/|\.\.?/)", value, re.I
            ):
                raise RuntimeError(f"Unpackaged {name} in {label}: {value}")
        for match in FETCH_RE.finditer(source):
            value = match.group("value")
            if not self.passthrough(value) and not value.startswith("__ANIVAG_OFFLINE_"):
                raise RuntimeError(
                    f"Authored runtime fetch/import is not self-contained in {label}: {value}"
                )

    def process_html(self, source: str, base: Path) -> str:
        source = self.process_scripts(source, base)
        source = self.process_styles(source, base)
        source = self.process_composition_refs(source, base)
        source = self.process_asset_attrs(source, base)
        self.validate_source(source, str(base))
        return source

    def pack_nested_composition(self, path: Path) -> str:
        path = path.resolve()
        existing = self.composition_tokens.get(path)
        if existing:
            if path in self.active_compositions:
                raise RuntimeError(f"Nested composition cycle detected at {path}")
            return existing
        token = f"__ANIVAG_OFFLINE_COMPOSITION_{len(self.composition_tokens) + 1:04d}__"
        self.composition_tokens[path] = token
        self.active_compositions.add(path)
        try:
            source = self.process_html(path.read_text(), path)
        finally:
            self.active_compositions.remove(path)
        self.compositions[token] = b64(source.encode())
        self.composition_order.append(token)
        return token

    def pack_entry(self, entry: Path, runtime: str) -> str:
        source = self.process_html(entry.read_text(), entry)
        runtime_tag = f"<script>{script_safe(runtime)}</script>"
        if re.search(r"</head\s*>", source, re.I):
            source = re.sub(
                r"</head\s*>",
                lambda _: runtime_tag + "</head>",
                source,
                count=1,
                flags=re.I,
            )
        else:
            source = runtime_tag + source
        return source


def read_metadata(project: Path, paths: list[Path]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for raw_path in paths:
        path = raw_path if raw_path.is_absolute() else project / raw_path
        path = path.resolve()
        value = json.loads(path.read_text())
        try:
            label = path.relative_to(project).as_posix()
        except ValueError:
            label = path.name
        result[label] = value
    return result


def make_document(
    *,
    title: str,
    width: int,
    height: int,
    player_source: str,
    runtime_source: str,
    entry_source: str,
    packer: OfflinePacker,
    version: Optional[str],
    metadata: dict[str, Any],
) -> str:
    runtime_data_url = "data:text/javascript;base64," + b64(runtime_source.encode())
    player_source, runtime_url_count = PLAYER_RUNTIME_URL_RE.subn(
        runtime_data_url, player_source
    )
    if runtime_url_count != 1:
        raise RuntimeError(
            "Unable to replace the HyperFrames Player runtime fallback; "
            "the installed Player format is unsupported"
        )
    player_source = script_safe(player_source)
    assets = {
        token: {"mime": item["mime"], "base64": item["base64"]}
        for token, item in packer.assets.items()
    }
    manifest = {
        "format": "anivag.hyperframes.offline-html",
        "formatVersion": 1,
        "hyperframesVersion": version,
        "width": width,
        "height": height,
        "duration": None,
        "assetCount": len(assets),
        "sourceAssetBytes": sum(int(item["bytes"]) for item in packer.assets.values()),
        "embeddedMetadata": metadata,
    }
    payload = {
        "assets": assets,
        "compositions": packer.compositions,
        "compositionOrder": packer.composition_order,
        "entry": b64(entry_source.encode()),
    }
    manifest_json = json.dumps(manifest, ensure_ascii=False).replace("<", "\\u003c")
    payload_json = json.dumps(payload, separators=(",", ":"))
    ratio = f"{width} / {height}"
    safe_title = html.escape(title)
    return f'''<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <meta name="generator" content="AniVAG HyperFrames offline packager">
  <title>{safe_title}</title>
  <style>
    :root {{ color-scheme: dark; background: #08070a; }}
    * {{ box-sizing: border-box; }}
    html, body {{ margin: 0; width: 100%; min-height: 100%; background: #08070a; }}
    body {{ min-height: 100vh; display: grid; place-items: center; padding: 16px; overflow: hidden; }}
    main {{ width: min(calc(100vw - 32px), calc((100vh - 32px) * {width} / {height})); aspect-ratio: {ratio}; position: relative; background: #000; box-shadow: 0 24px 80px rgba(0,0,0,.58); }}
    hyperframes-player {{ display: block; width: 100%; height: 100%; }}
    #status {{ position: fixed; left: 12px; bottom: 8px; z-index: 2; color: #aaa; font: 12px/1.4 ui-sans-serif, system-ui, sans-serif; opacity: .78; }}
    #status[data-error] {{ color: #ff8b92; opacity: 1; }}
  </style>
</head>
<body>
  <main><hyperframes-player id="film" width="{width}" height="{height}" controls></hyperframes-player></main>
  <div id="status" role="status" aria-live="polite">正在载入离线短片…</div>
  <noscript>这个离线短片需要浏览器启用 JavaScript。</noscript>
  <script type="application/json" id="anivag-offline-manifest">{manifest_json}</script>
  <script>{player_source}</script>
  <script>
    (() => {{
      const payload = {payload_json};
      const film = document.getElementById('film');
      const status = document.getElementById('status');
      const objectUrls = [];

      const bytesFromBase64 = base64 => {{
        const chunks = [];
        const chunkSize = 32768;
        for (let offset = 0; offset < base64.length; offset += chunkSize) {{
          const binary = atob(base64.slice(offset, offset + chunkSize));
          const bytes = new Uint8Array(binary.length);
          for (let index = 0; index < binary.length; index += 1) bytes[index] = binary.charCodeAt(index);
          chunks.push(bytes);
        }}
        return chunks;
      }};
      const decodeText = async base64 => new TextDecoder().decode(
        await new Blob(bytesFromBase64(base64)).arrayBuffer()
      );
      const replaceAllTokens = (source, replacements) => {{
        for (const [token, value] of replacements) source = source.split(token).join(value);
        return source;
      }};

      const boot = async () => {{
        const replacements = [];
        for (const [token, asset] of Object.entries(payload.assets)) {{
          const url = URL.createObjectURL(new Blob(bytesFromBase64(asset.base64), {{ type: asset.mime }}));
          objectUrls.push(url);
          replacements.push([token, url]);
        }}
        for (const token of payload.compositionOrder) {{
          let source = await decodeText(payload.compositions[token]);
          source = replaceAllTokens(source, replacements);
          const url = URL.createObjectURL(new Blob([source], {{ type: 'text/html;charset=utf-8' }}));
          objectUrls.push(url);
          replacements.push([token, url]);
        }}
        let entry = await decodeText(payload.entry);
        entry = replaceAllTokens(entry, replacements);
        film.setAttribute('srcdoc', entry);
      }};

      film.addEventListener('ready', event => {{
        status.textContent = '正在载入离线素材…';
        document.documentElement.dataset.timelineReady = 'true';
        const manifestNode = document.getElementById('anivag-offline-manifest');
        const value = JSON.parse(manifestNode.textContent);
        value.duration = event.detail?.duration || film.duration || null;
        manifestNode.textContent = JSON.stringify(value);
      }});
      film.addEventListener('assetsready', () => {{
        if (!film.ready && !film._ready) return;
        status.textContent = '';
        status.hidden = true;
        document.documentElement.dataset.ready = 'true';
      }});
      film.addEventListener('error', event => {{
        const message = event.detail?.message || '未知错误';
        status.hidden = false;
        status.textContent = `载入失败：${{message}}`;
        status.dataset.error = '';
        document.documentElement.dataset.error = message;
      }});
      addEventListener('pagehide', () => objectUrls.forEach(url => URL.revokeObjectURL(url)), {{ once: true }});
      boot().catch(error => {{
        status.hidden = false;
        status.textContent = `载入失败：${{error.message}}`;
        status.dataset.error = '';
        document.documentElement.dataset.error = error.message;
        console.error(error);
      }});
    }})();
  </script>
</body>
</html>
'''


def composition_size(source: str) -> tuple[int, int]:
    match = re.search(
        r"data-width\s*=\s*['\"](?P<width>\d+)['\"][^>]*data-height\s*=\s*['\"](?P<height>\d+)['\"]",
        source,
        re.I | re.S,
    )
    if not match:
        match = re.search(
            r"data-height\s*=\s*['\"](?P<height>\d+)['\"][^>]*data-width\s*=\s*['\"](?P<width>\d+)['\"]",
            source,
            re.I | re.S,
        )
    if not match:
        raise RuntimeError("Entry composition must declare data-width and data-height")
    return int(match.group("width")), int(match.group("height"))


def main() -> int:
    args = parse_args()
    try:
        project = args.project.expanduser().resolve()
        entry = (project / args.entry).resolve()
        if not project.is_dir():
            raise RuntimeError(f"Project directory does not exist: {project}")
        if not entry.is_file() or project not in entry.parents:
            raise RuntimeError(f"Entry must be a file inside the project: {entry}")
        if args.output.suffix.lower() not in {".html", ".htm"}:
            raise RuntimeError("--output must end in .html or .htm")
        dist = locate_hyperframes_dist(project, args.hyperframes_dist)
        runtime = (dist / "hyperframe.runtime.iife.js").read_text()
        player = (dist / "hyperframes-player.global.js").read_text()
        packer = OfflinePacker(project, args.fetch_timeout)
        entry_source = packer.pack_entry(entry, runtime)
        width, height = composition_size(entry_source)
        metadata = read_metadata(project, args.metadata)
        document = make_document(
            title=args.title or project.name,
            width=width,
            height=height,
            player_source=player,
            runtime_source=runtime,
            entry_source=entry_source,
            packer=packer,
            version=package_version(dist),
            metadata=metadata,
        )
        output = args.output.expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_suffix(output.suffix + ".tmp")
        temporary.write_text(document)
        temporary.replace(output)
        print(
            json.dumps(
                {
                    "output": str(output),
                    "bytes": output.stat().st_size,
                    "assets": len(packer.assets),
                    "nestedCompositions": len(packer.compositions),
                    "hyperframesVersion": package_version(dist),
                },
                ensure_ascii=False,
            )
        )
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
