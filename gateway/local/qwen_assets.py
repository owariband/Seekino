"""Pinned desktop Qwen3 embedding weights shared by the installer and encoder."""
import hashlib
import os
from pathlib import Path
import sys
from tempfile import NamedTemporaryFile
import threading

MODEL_ID = "seekino-qwen3-embedding-0.6b"
MODEL_FILENAME = "Qwen3-Embedding-0.6B-Q8_0.gguf"
MODEL_BYTES = 639_150_592
MODEL_SHA256 = "06507c7b42688469c4e7298b0a1e16deff06caf291cf0a5b278c308249c3e439"
MODEL_URL = "https://registry.ollama.ai/v2/library/qwen3-embedding/blobs/sha256:" + MODEL_SHA256
DIMENSIONS = 1024
QUERY_INSTRUCTION = "Given a question about a machine learning course, retrieve relevant lecture passages that answer the question."
_CHUNK = 1024 * 1024
_cached = None
_guard = threading.Lock()


def model_path() -> Path:
    if sys.platform == "darwin":
        root = Path.home() / "Library/Application Support"
    elif os.name == "nt":
        root = Path(os.environ["LOCALAPPDATA"])
    else:
        root = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    return root / "Seekino/models" / MODEL_SHA256 / MODEL_FILENAME


def _identity(path):
    s = path.stat()
    return path, s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns


class DownloadCancelled(Exception):
    pass


def _check_cancel(cancel):
    if cancel and cancel.is_set():
        raise DownloadCancelled()


def local_model_ready() -> bool:
    """Fast readiness for views. A background scan or installer verifies the digest."""
    try:
        return _cached == _identity(model_path())
    except OSError:
        return False


def verified_model_path(*, cancel=None) -> Path:
    global _cached
    path = model_path()
    with _guard:
        key = _identity(path)
        if key == _cached:
            return path
        if key[3] != MODEL_BYTES:
            raise ValueError("本机模型大小不符，请重新下载。")
        digest = hashlib.sha256()
        with path.open("rb") as src:
            while chunk := src.read(_CHUNK):
                _check_cancel(cancel)
                digest.update(chunk)
        if digest.hexdigest() != MODEL_SHA256 or _identity(path) != key:
            raise ValueError("本机模型校验失败，请重新下载。")
        _cached = key
        return path


def fetch_weights(source, cancel, progress):
    """Publish only a complete, verified file; competing installs have identical bytes."""
    global _cached
    path = model_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = None
    try:
        digest = hashlib.sha256()
        received = 0
        with NamedTemporaryFile(dir=path.parent, prefix="qwen-download-", suffix=".part", delete=False) as dst:
            tmp = Path(dst.name)
            while True:
                _check_cancel(cancel)
                chunk = source.read(_CHUNK)
                if not chunk:
                    break
                received += len(chunk)
                if received > MODEL_BYTES:
                    raise ValueError("下载超出模型大小。")
                dst.write(chunk)
                digest.update(chunk)
                progress(received)
            dst.flush()
            os.fsync(dst.fileno())
        _check_cancel(cancel)
        if received != MODEL_BYTES or digest.hexdigest() != MODEL_SHA256:
            raise ValueError("文件大小或 SHA-256 校验失败，请重新下载。")
        with _guard:
            _check_cancel(cancel)
            tmp.replace(path)
            _cached = _identity(path)
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)
