from hashlib import sha256
from io import BytesIO
from threading import Event

import pytest

from gateway.local import qwen_assets as assets


@pytest.fixture
def weights(tmp_path, monkeypatch):
    body = b"model contents"
    target = tmp_path / "models" / "weights.gguf"
    monkeypatch.setattr(assets, "model_path", lambda: target)
    monkeypatch.setattr(assets, "MODEL_BYTES", len(body))
    monkeypatch.setattr(assets, "MODEL_SHA256", sha256(body).hexdigest())
    monkeypatch.setattr(assets, "_cached", None)
    return body, target


def test_download_verifies_before_publishing(weights):
    body, path = weights
    assets.fetch_weights(BytesIO(body), Event(), lambda _: None)
    assert assets.verified_model_path() == path
    assert assets.local_model_ready()
    with pytest.raises(ValueError):
        assets.fetch_weights(BytesIO(b"X" * len(body)), Event(), lambda _: None)
    assert path.read_bytes() == body
    assert not list(path.parent.glob("*.part"))
    path.write_bytes(b"Y" * len(body))
    assert not assets.local_model_ready()
    with pytest.raises(ValueError):
        assets.verified_model_path()


def test_cancel_then_retry(weights):
    body, path = weights
    cancel = Event()
    with pytest.raises(assets.DownloadCancelled):
        assets.fetch_weights(BytesIO(body), cancel, lambda _: cancel.set())
    assert not path.exists()
    assert not list(path.parent.glob("*.part"))
    assets.fetch_weights(BytesIO(body), Event(), lambda _: None)
    assert assets.local_model_ready()
