"""User-visible contracts for downloaded and bring-your-own desktop embeddings."""
from contextlib import closing
import hashlib
import sqlite3

import pytest
import yaml

from desktop.service_profiles import ServiceProfiles
from gateway.local import qwen_assets
from gateway.model_factory import GatewayClient
from server.desktop import app
from server.desktop.model_settings import ModelProfile
from tests.server.test_desktop_model_settings import form, request


@pytest.fixture
def weights(tmp_path, monkeypatch):
    data = b"isolated test model bytes"
    path = tmp_path / "mock.gguf"
    monkeypatch.setattr(qwen_assets, "model_path", lambda: path)
    monkeypatch.setattr(qwen_assets, "MODEL_BYTES", len(data))
    monkeypatch.setattr(qwen_assets, "MODEL_SHA256", hashlib.sha256(data).hexdigest())
    monkeypatch.setattr(qwen_assets, "_cached", None)
    return path, data


def test_local_role_has_no_http_connection_or_key_and_keeps_api_catalogue(tmp_path, weights):
    path, data = weights
    path.write_bytes(data)
    api = ModelProfile.read(tmp_path).candidate(form(auth="none"))
    api.persist()
    settings = api.view({})
    assert settings["form"]["embeddingMode"] == "api"
    catalogue = ServiceProfiles(tmp_path, settings)
    [key] = catalogue.rows
    rows, selection, assigned = catalogue.apply_form(
        key, "model", dict(vision="v", transcription="a", embeddingMode="local", rerank=""))
    assert set(assigned) == {"vision", "transcription", "rerank"}
    assert rows[key]["embedding"] == "test-embed" and rows[key]["dimensions"] == 64
    catalogue.persist(rows)
    local = api.prepare({"revision": api.revision, "form": selection})
    view = local.view({})
    assert view["form"]["embeddingMode"] == "local" and view["ready"]
    assert view["form"]["embedding"] == qwen_assets.MODEL_ID
    assert view["form"]["dimensions"] == qwen_assets.DIMENSIONS
    assert not view["credentialNames"]
    assert all(not any(b["role"] == "embedding" for b in row["roles"]) for row in view["connections"])
    local.persist()
    restored = ServiceProfiles(tmp_path, ModelProfile.read(tmp_path).view({}))
    assert "embedding" not in restored.active and restored.rows[key]["embedding"] == "test-embed"
    _, api_again, _ = restored.apply_form(key, "model", form(auth="none"))
    assert not local.prepare({"revision": local.revision, "form": api_again}).local_embedding()
    # Imported operator configuration keeps the download entry available without editable bindings.
    custom = ModelProfile.parse(local.path, local.raw.split(b"\n", 1)[1])
    assert custom.view({})["mode"] == "custom" and custom.view({})["form"]["embeddingMode"] == "local"


def test_local_contract_is_frozen_for_jobs_and_never_accepts_other_weights(tmp_path, weights):
    path, data = weights
    initial = ModelProfile.read(tmp_path)
    args = dict(revision=initial.revision, form=form(embeddingMode="local", embedding="", dimensions=None))
    with pytest.raises(ValueError, match="下载"):
        initial.prepare(args)
    path.write_bytes(b"invalid")
    with pytest.raises(ValueError, match="大小"):
        initial.prepare(args)
    path.write_bytes(data)
    local = initial.prepare(args)
    binding = local.config.resolve_embedding_binding()
    assert binding.model == qwen_assets.MODEL_ID and binding.query_instruction == qwen_assets.QUERY_INSTRUCTION
    assert binding.input_mode == "text" and binding.dimensions == 1024
    assert qwen_assets.MODEL_SHA256 in local.encoder_contract()[2]
    with closing(GatewayClient(local.config, environ={}, enable_supervisor=False)) as gateway:
        with closing(gateway.create_embedding_model(binding)) as encoder:
            assert encoder.provider_name == "local_qwen" and encoder.dimensions == 1024
            with pytest.raises(ValueError, match="empty"):
                encoder.embed_query("  ")
    for change in (dict(dimensions=512), dict(model="someone-else"), dict(query_instruction=None),
                   dict(input_mode="multimodal"), dict(encoder_model="unverified")):
        with pytest.raises(ValueError, match="fixed encoding contract"):
            local.config.resolve_embedding_binding(binding.model_copy(update=change))
    body = yaml.safe_load(local.raw)
    body["model_sources"][binding.source]["base_url"] = "https://unexpected.invalid/v1"
    with pytest.raises(ValueError, match="配置文件无效"):
        ModelProfile.parse(local.path, yaml.safe_dump(body).encode())


def test_restart_with_missing_weights_still_opens_and_download_restores_processing(tmp_path, monkeypatch, weights):
    pytest.importorskip("sqlite_vec")
    monkeypatch.setattr(app, "enforce_single_process", lambda: None)
    path, data = weights
    path.write_bytes(data)
    root = str(tmp_path / "library")
    request("open", root=root)
    try:
        current = request("modelSettings", root=root)
        args = dict(root=root, revision=current["revision"], form=form(embeddingMode="local", auth="none"))
        settings = request("saveModelSettings", **args)
        assert settings["ready"]
        series = request("createSeries", name="Local test")
        request("close")
        path.unlink()
        request("open", root=root)
        assert request("seriesList")[0]["id"] == series["id"]
        assert not app._application.library._consumer.running
        current = request("modelSettings", root=root)
        assert not current["importReady"] and not current["searchReady"]
        with pytest.raises(ValueError, match="下载"):
            request("search", query="test", seriesIDs=[series["id"]], keywords=False)
        path.write_bytes(data)
        qwen_assets.verified_model_path()
        assert request("refreshLocalEmbedding", root=root)["ready"]
        assert app._application.library._consumer.running
        with sqlite3.connect(tmp_path / "library/library.sqlite3") as db:
            db.execute("INSERT INTO local_chunks(task,chunk,version,generation,event_count,document) "
                       "VALUES ('published','one',1,1,0,'{}')")
        for change in (form(auth="none"), form(auth="none", dimensions=1024)):
            with pytest.raises(ValueError, match="资料库已有"):
                request("saveModelSettings", root=root, revision=current["revision"], form=change)
        assert request("modelSettings", root=root)["form"]["embeddingMode"] == "local"
        request("close")
        monkeypatch.setattr(qwen_assets, "_cached", None)
        request("open", root=root)
        assert request("modelSettings", root=root)["ready"] and app._application.library._consumer.running
    finally:
        if app._application is not None:
            request("close")
