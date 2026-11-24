"""Exercise connection registration, role binding and native credential isolation."""
from time import monotonic, sleep

import pytest

pytest.importorskip("PySide6")
from PySide6.QtCore import QCoreApplication

from desktop.qt_controller import Workspace
from gateway.constants import ARK_OPENAI_BASE_URL
from server.desktop.model_settings import ModelProfile
from server.desktop.watch_settings import watch_connection
from tests.desktop.test_workspace import MemoryCredentials


def test_connections_roles_keys_and_library_restart(tmp_path, monkeypatch):
    import sqlite3
    app = QCoreApplication.instance() or QCoreApplication([])
    monkeypatch.setattr("server.desktop.app.enforce_single_process", lambda: None)
    for name in ("ARK_API_KEY", "DEEPSEEK_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    vault = MemoryCredentials()
    errors, saved = [], []
    root = tmp_path / "library"

    def wait(predicate):
        until = monotonic() + 20
        while not predicate():
            app.processEvents()
            assert monotonic() < until
            sleep(.003)

    def open_workspace():
        ws = Workspace(root, credentials=vault)
        ws.serviceProfileSaved.connect(saved.append)
        ws.serviceProfileFailed.connect(errors.append)
        ws.start()
        wait(lambda: ws.ready or ws.error)
        assert not ws.error
        return ws

    ws = open_workspace()

    def save(fields, secret=""):
        ws.saveServiceProfile(dict(fields, servicesRevision=ws.modelConfig["servicesRevision"]), secret)
        wait(lambda: not ws.savingKeys)

    def apply(selected, purpose, **roles):
        cfg = ws.modelConfig
        revision = cfg["watchRevision" if purpose == "watch" else "revision"]
        ws.applyServiceProfile(selected, purpose, dict(roles, servicesRevision=cfg["servicesRevision"], revision=revision))
        wait(lambda: not ws.savingKeys)

    try:
        original = ws.modelConfig
        assert original["serviceProfiles"] == [] and not (root / "model-services.json").exists()
        save(dict(name="Vision", protocol="ark", baseUrl=ARK_OPENAI_BASE_URL, auth="key"), "secret-ark")
        ark = saved[-1]
        text = dict(name="Embeddings", protocol="openai_chat", baseUrl="https://first.example/v1", auth="key")
        save(text, "secret-one")
        embed = saved[-1]
        save(dict(text, name="Chat", baseUrl="https://second.example/v1"), "secret-two")
        chat = saved[-1]
        assert len(set(saved)) == 3 and not ws.importModelReady
        roles = dict(vision="vision-1", transcription="asr-1", embedding="embed-1", dimensions=64, rerank="",
                     roleServices=dict(vision=ark, transcription=ark, embedding=embed, rerank=""))
        apply(ark, "model", **roles)
        assert not errors and ws.importModelReady and ws.searchModelReady
        assert ws.modelConfig["parseRoleServices"] == dict(vision=ark, transcription=ark, embedding=embed, rerank=ark)
        profile = ModelProfile.read(root)
        config, keys = profile.for_gateway(vault)
        for role, token in (("vision", "secret-ark"), ("transcription", "secret-ark"), ("embedding", "secret-one")):
            binding = config.resolve_model_binding(role, environ={})
            assert keys[config.source(binding.source).api_key_env] == token
        apply(chat, "watch", watchModel="qa-one")
        config, keys = watch_connection(root, profile, vault)
        binding = config.resolve_model_binding("vision", environ={})
        assert binding.model == "qa-one" and keys[config.source(binding.source).api_key_env] == "secret-two"

        # A published projection freezes the encoder. Changing the vision provider is allowed.
        with sqlite3.connect(root / "library.sqlite3") as db:
            db.execute("INSERT INTO local_chunks(task,chunk,version,generation,event_count,document) "
                       "VALUES ('published','one',1,1,0,'{}')")
        changed = {**roles, "roleServices": {**roles["roleServices"], "vision": chat}, "vision": "vision-2"}
        apply(chat, "model", **changed)
        assert not errors and ModelProfile.read(root).encoder_contract() == profile.encoder_contract()
        apply(chat, "model", **dict(changed, dimensions=128))
        assert errors.pop() and ModelProfile.read(root).form["dimensions"] == 64

        # Editing a saved key's destination requires a fresh key; typed secrets never enter QML or JSON.
        save(dict(text, id=embed, baseUrl="https://third.example/v1"))
        assert errors.pop()
        assert next(row for row in ws.modelConfig["serviceProfiles"] if row["id"] == embed)["baseUrl"] == text["baseUrl"]
        ws.removeServiceProfile(embed, ws.modelConfig["servicesRevision"])
        wait(lambda: not ws.savingKeys)
        assert errors.pop() and len(ws.modelConfig["serviceProfiles"]) == 3
        from server.desktop import app as backend

        def running_keys():
            live = backend._application
            parse_config, parse_keys = live.profile.for_gateway(live.keys)
            watch_config, watch_keys = watch_connection(root, live.profile, live.keys)
            def vision_endpoint(config, keys):
                binding = config.resolve_model_binding("vision", environ={})
                target = config.source(binding.source)
                return target.base_url, keys[target.api_key_env]
            return vision_endpoint(parse_config, parse_keys), vision_endpoint(watch_config, watch_keys)

        save({"id": chat}, "rotated-key")
        assert not errors and running_keys() == (("https://second.example/v1", "rotated-key"),) * 2
        before_files = tuple((root / name).read_bytes() for name in ("models.yaml", "watch-model.json"))
        save({"id": chat, "baseUrl": "https://new-chat.example/v1"}, "new-site-secret")
        assert not errors and running_keys() == (("https://second.example/v1", "rotated-key"),) * 2
        row = next(item for item in ws.modelConfig["serviceProfiles"] if item["id"] == chat)
        assert not row["activeRoles"] and set(row["pendingRoles"]) == {"vision", "rerank", "watch"}
        assert ws.modelConfig["effectiveRoleServices"]["watch"]["baseUrl"] == "https://second.example/v1"
        assert before_files == tuple((root / name).read_bytes() for name in ("models.yaml", "watch-model.json"))
        apply(chat, "watch", watchModel="qa-two")
        assert not errors and running_keys() == (("https://second.example/v1", "rotated-key"),
                                                 ("https://new-chat.example/v1", "new-site-secret"))
        row = next(item for item in ws.modelConfig["serviceProfiles"] if item["id"] == chat)
        assert row["activeRoles"] == ["watch"] and set(row["pendingRoles"]) == {"vision", "rerank"}
        for secret in ("secret-ark", "secret-one", "secret-two", "rotated-key", "new-site-secret"):
            assert secret not in str(ws.modelConfig)
            assert secret not in (root / "model-services.json").read_text()
            assert secret not in (root / "models.yaml").read_text()
            assert secret not in (root / "watch-model.json").read_text()
    finally:
        ws.shutdown()

    ws = open_workspace()
    try:
        assert ws.watchModelReady and ws.importModelReady
        assert ws.modelConfig["parseRoleServices"]["vision"] == chat
        assert sorted(row["id"] for row in ws.modelConfig["serviceProfiles"]) == sorted([ark, embed, chat])
    finally:
        ws.shutdown()


def test_legacy_projection_and_explicit_key_discovery(tmp_path, monkeypatch):
    from desktop.service_profiles import ServiceProfiles
    from tests.server.test_desktop_model_settings import form
    from server.desktop.watch_settings import save_watch, watch_credentials, watch_view

    profile = ModelProfile.read(tmp_path).candidate(form(baseUrl="https://old.example/v1"))
    profile.persist()
    model_file = profile.path.read_bytes()
    watch = {"protocol": "openai_chat", "baseUrl": "https://old-watch.example/v1", "model": "watch", "auth": "key"}
    old_watch = watch_credentials(watch)["credentialName"]
    old_model = profile.slots()[profile.source]
    vault = MemoryCredentials({old_watch: "old-watch-token", old_model: "old-parse-token"})
    save_watch(tmp_path, watch, vault)
    snapshot = profile.view(vault) | watch_view(tmp_path, profile, vault)
    services = ServiceProfiles(tmp_path, snapshot)
    assert len(services.rows) == 2
    assert not services.path.exists() and model_file == profile.path.read_bytes()
    for purpose, expected in (("vision", "old-parse-token"), ("watch", "old-watch-token")):
        key = services.active[purpose]
        row = services.rows[key]
        assert services.get_key(row, vault) == expected
        edited = services.edit({**row, "baseUrl": "https://changed.example/v1"})
        assert not services.get_key(edited, vault)

    app = QCoreApplication.instance() or QCoreApplication([])
    ws = Workspace(tmp_path, credentials=vault)
    monkeypatch.setattr("desktop.qt_controller._request", lambda action, **kwargs: snapshot)
    received = []
    def listing(protocol, url, api_key, **kwargs):
        received.append((url, api_key))
        return ["a"]
    monkeypatch.setattr("server.desktop.model_catalogue.discover_models", listing)
    try:
        responses = []
        ws.modelsDiscovered.connect(lambda *args: responses.append(args))
        row = services.rows[services.active["watch"]]
        for i, (url, expected) in enumerate(((row["baseUrl"], "old-watch-token"),
                                             ("https://changed.example/v1", None))):
            ws.discoverModels(str(i), "service:" + row["id"], "openai_chat", url, "", services.revision)
            deadline = monotonic() + 5
            while len(responses) <= i:
                app.processEvents()
                assert monotonic() < deadline
            assert received[-1] == (url, expected)
    finally:
        ws.shutdown()
