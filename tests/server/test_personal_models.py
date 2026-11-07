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


def test_yaml_settings_are_scoped_masked_and_never_seed_the_database(local_yaml_chat_catalog, tmp_path):
    from server.constants.identity import LOCAL_USER_ID

    engine = create_engine("sqlite://")
    PersonalModelPreferences.__table__.create(engine)
    sessions = sessionmaker(engine)
    def make_store(local):
        return PersonalModelConnections(sessions, key_file=tmp_path / "vault" / "key", signing_secret="", allow_local=local,
                                        yaml_models=local_yaml_chat_catalog)

    store = make_store(True)
    settings = store.list(LOCAL_USER_ID)
    connection = next(c for c in settings.connections if c.name == "my-endpoint")
    assert connection.origin == "yaml" and connection.has_api_key
    assert connection.protocol == "openai_chat" and connection.base_url == "https://chat.example.test/v1"
    assert [m.model_id for m in connection.models] == ["team/home", "team/alternative", "team/discovered"]
    assert connection.models[1].label == "My alternative"
    assert settings.connections[0] == connection
    assert "synthetic-private-key" not in settings.model_dump_json() and "TEST_LOCAL_PROVIDER_KEY" not in settings.model_dump_json()
    assert any(not c.has_api_key and not any(m.available for m in c.models) for c in settings.connections if c.name != connection.name)
    home, alternative, _ = connection.models
    assert store.default_for_chat(LOCAL_USER_ID, settings) == home.id and home.is_yaml_default
    assert store.resolve(LOCAL_USER_ID, alternative.id, connection_id=connection.connection_id).api_key.get_secret_value() == "synthetic-private-key"
    with pytest.raises(PersonalModelNotFoundError):
        store.resolve(LOCAL_USER_ID, alternative.id, connection_id=str(uuid4()))
    for other, owner in [(store, "other"), (make_store(False), LOCAL_USER_ID)]:
        assert not other.list(owner).connections and other.yaml_binding(owner, alternative.id) is None
        with pytest.raises(PersonalModelNotFoundError):
            other.resolve(owner, alternative.id)
        with pytest.raises(PersonalModelNotFoundError):
            other.set_default(owner, alternative.id)
    with sessions() as session:
        assert session.scalar(select(PersonalModelPreferences)) is None
    assert not (tmp_path / "vault" / "key").exists()
    body = PersonalModelConnectionSave(name="personal", protocol="openai_chat", base_url="https://other.example",
                                       model_ids=["same/name"], revision=0)
    with pytest.raises(ValueError, match="YAML"):
        store.save(LOCAL_USER_ID, connection.connection_id, body)
    with pytest.raises(ValueError, match="YAML"):
        store.delete(LOCAL_USER_ID, connection.connection_id, 1)
    store.set_default(LOCAL_USER_ID, alternative.id)
    personal_id = str(uuid4())
    assert store.save(LOCAL_USER_ID, personal_id, body).default_model_id == alternative.id
    assert store.delete(LOCAL_USER_ID, personal_id, 1).default_model_id == alternative.id
    engine.dispose()
