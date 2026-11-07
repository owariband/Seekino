"""Edit the active YAML file under one lock shared by all settings endpoints."""
from __future__ import annotations

import fcntl
import hashlib
import os
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path

import yaml
from yaml.nodes import MappingNode, ScalarNode, SequenceNode


class GlobalAgentSettingsConflictError(Exception):
    """The YAML file changed since the caller last read it."""


def revision(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


@contextmanager
def document_lock(path: Path):
    # A separate inode keeps the lock valid across atomic replacement of the YAML.
    with path.with_name("." + path.name + ".seekino.lock").open("ab") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def replace_document(path: Path, content: bytes, expected_revision: str) -> None:
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", prefix=f".{path.name}.", suffix=".tmp",
                                         dir=path.parent, delete=False) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.chmod(temporary_path, stat.S_IMODE(path.stat().st_mode))
        if revision(path.read_bytes()) != expected_revision:
            raise GlobalAgentSettingsConflictError("配置已更改，请刷新后再保存")
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def edit_mapping(raw: bytes, before: dict, after: dict) -> bytes:
    """Splice changed mapping entries; other entries and their comments retain their original bytes."""
    document = raw.decode("utf-8")
    root = yaml.compose(document)
    if not isinstance(root, MappingNode):
        raise ValueError("配置文件必须是 YAML 映射")
    newline = "\r\n" if "\r\n" in document else "\n"
    edits: list[tuple[int, int, str]] = []

    def end_of_entry(node):
        if isinstance(node, MappingNode) and not node.flow_style and node.value:
            return end_of_entry(node.value[-1][1])
        if isinstance(node, SequenceNode) and not node.flow_style and node.value:
            return end_of_entry(node.value[-1])
        end = node.end_mark.index
        if end and document[end - 1] == "\n":
            return end
        next_line = document.find("\n", end)
        return len(document) if next_line < 0 else next_line + 1

    def block(values, indent):
        dumped = yaml.safe_dump(values, sort_keys=False, allow_unicode=True, width=200)
        return "".join(" " * indent + line + newline for line in dumped.splitlines())

    def walk(node, old, new):
        keys = {key.value: (key, val) for key, val in node.value if isinstance(key, ScalarNode)}
        if len(keys) != len(node.value) or keys.keys() != old.keys():
            raise ValueError("此 YAML 映射使用了别名或重复键，无法安全保存")
        for name, (key, val) in keys.items():
            if name in new and old[name] == new[name]:
                continue
            if name in new and isinstance(val, MappingNode) and val.value and not val.flow_style and isinstance(new[name], dict) and new[name]:
                walk(val, old[name], new[name])
                continue
            begin = key.start_mark.index - key.start_mark.column
            end = end_of_entry(val)
            if end <= begin:
                raise ValueError("无法安全编辑 YAML 别名")
            edits.append((begin, end, block({name: new[name]}, key.start_mark.column) if name in new else ""))
        added = {name: value for name, value in new.items() if name not in old}
        if added:
            pos = end_of_entry(node)
            indent = node.value[0][0].start_mark.column
            prefix = "" if not pos or document[pos-1] == "\n" else newline
            edits.append((pos, pos, prefix + block(added, indent)))

    if root.flow_style or not root.value:
        result = yaml.safe_dump(after, sort_keys=False, allow_unicode=True)
    else:
        walk(root, before, after)
        result = document
        for begin, end, replacement in sorted(edits, key=lambda edit: (edit[0], edit[1]), reverse=True):
            result = result[:begin] + replacement + result[end:]
    if yaml.safe_load(result) != after:
        raise ValueError("无法保留 YAML 文件结构，未写入变更")
    return result.encode("utf-8")
