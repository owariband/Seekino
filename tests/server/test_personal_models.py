from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from server.model.dto.personal_model import PersonalModelConnectionSave
from server.model.personal_models import PersonalModelPreferences
from server.service.personal_models import PersonalModelConnections, PersonalModelConflictError, PersonalModelNotFoundError


def test_personal_connections_are_owned_encrypted_and_versioned(tmp_path):
    db = create_engine("sqlite://")
    PersonalModelPreferences.__table__.create(db)
    sessions = sessionmaker(db)
    path = tmp_path / "vault" / "secret"
    store = PersonalModelConnections(sessions, key_file=path, signing_secret="", allow_local=True)
    cid = str(uuid4())
    fields = dict(name="My service", protocol="openai_chat", base_url="http://localhost:4242/v1", model_ids=["org/custom"], revision=0)
    assert not store.list("alice").connections and not path.exists()
    added = store.save("alice", cid, PersonalModelConnectionSave(**fields, api_key="test-only-secret"))
    assert "test-only-secret" not in added.model_dump_json() and added.connections[0].has_api_key
    with sessions() as session:
        assert "test-only-secret" not in str(session.scalar(select(PersonalModelPreferences)).connections)
    assert path.stat().st_mode & 0o777 == 0o600
    ref = added.connections[0].models[0].id
    assert store.set_default("alice", ref).default_model_id == ref
    assert not store.list("bob").connections
    with pytest.raises(PersonalModelNotFoundError):
        store.resolve("bob", ref)
    with pytest.raises(PersonalModelNotFoundError):
        store.set_default("bob", ref)
    with pytest.raises(PersonalModelNotFoundError):
        store.resolve("alice", ref, connection_id=str(uuid4()))
    fields.update(revision=1, model_ids=["other", "org/custom"])
    assert store.save("alice", cid, PersonalModelConnectionSave(**fields)).connections[0].models[1].id == ref
    assert store.resolve("alice", ref).api_key.get_secret_value() == "test-only-secret"
    with pytest.raises(PersonalModelConflictError):
        store.save("alice", cid, PersonalModelConnectionSave(**fields))
    with pytest.raises(PersonalModelConflictError):
        store.delete("alice", cid, 1)
    fields.update(revision=2, base_url="https://proxy.example/v1")
    with pytest.raises(ValueError, match="API Key"):
        store.save("alice", cid, PersonalModelConnectionSave(**fields))
    updated = store.save("alice", cid, PersonalModelConnectionSave(**fields, api_key=""))
    assert not updated.connections[0].has_api_key and updated.default_model_id == ref
    with pytest.raises(PersonalModelNotFoundError):
        store.delete("bob", cid, 3)
    assert store.delete("alice", cid, 3).default_model_id == "default"
    with pytest.raises(PersonalModelNotFoundError):
        store.resolve("alice", ref)
