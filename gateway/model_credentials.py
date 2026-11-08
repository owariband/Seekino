"""Authenticated encryption for credentials written through the model settings API."""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def _key(config_path: Path, *, create: bool) -> bytes:
    directory = config_path.parent / ".seekino-secrets"
    path = directory / "model-config.key"
    if create and not path.exists():
        directory.mkdir(mode=0o700, exist_ok=True)
        with NamedTemporaryFile(dir=directory) as tmp:
            tmp.write(os.urandom(32))
            tmp.flush()
            os.fsync(tmp.fileno())
            try:
                os.link(tmp.name, path)
            except FileExistsError:
                pass
    try:
        key = path.read_bytes()
    except OSError:
        raise RuntimeError("模型配置的凭证保护密钥不可用") from None
    if len(key) != 32:
        raise RuntimeError("模型配置的凭证保护密钥无效")
    return key


def seal(config_path: Path, destination: tuple[str, ...], secret: str) -> str:
    nonce = os.urandom(12)
    ciphertext = AESGCM(_key(config_path, create=True)).encrypt(nonce, secret.encode(), json.dumps(destination).encode())
    return base64.b64encode(nonce + ciphertext).decode("ascii")


def unseal(config_path: Path, destination: tuple[str, ...], value: str) -> str:
    try:
        data = base64.b64decode(value, validate=True)
        return AESGCM(_key(config_path, create=False)).decrypt(data[:12], data[12:], json.dumps(destination).encode()).decode()
    except (InvalidTag, ValueError):
        raise RuntimeError("无法读取此模型服务的加密凭证") from None
