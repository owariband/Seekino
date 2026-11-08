"""Read older, owner-scoped conversation targets without publishing them in deployment settings."""
import base64
import hashlib
import hmac
import json
from pathlib import Path
from uuid import UUID, uuid5

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import SecretStr
from sqlalchemy.orm import Session, sessionmaker

from gateway.personal_endpoint import PersonalModelTarget
from ..repository.personal_models import PersonalModelRepository
from .model_settings import PersonalModelNotFoundError


class LegacyPersonalModels:
    def __init__(self, sessions: sessionmaker[Session], *, key_file: Path, signing_secret: str):
        self._repo = PersonalModelRepository(sessions)
        self._key_file = key_file
        self._master = hmac.digest(signing_secret.encode(), b"seekino/personal-model-connections/v1", hashlib.sha256) if signing_secret else None

    def resolve(self, owner: str, model_id: str) -> PersonalModelTarget:
        for record in self._repo.read(owner)[0]:
            for model in record["models"]:
                if model_id != f"personal:{uuid5(UUID(record['id']), model)}":
                    continue
                secret = ""
                if record["key"]:
                    blob = base64.b64decode(record["key"])
                    aad = json.dumps([owner, record["id"], record["protocol"], record["url"]]).encode()
                    cipher = AESGCM(self._master or self._key_file.read_bytes())
                    secret = cipher.decrypt(blob[:12], blob[12:], aad).decode()
                return PersonalModelTarget(record["protocol"], record["url"], model, SecretStr(secret), record["vision"])
        raise PersonalModelNotFoundError()
