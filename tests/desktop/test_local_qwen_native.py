"""Use the packaged CPU bridge and pinned GGUF for offline inference regression tests."""
from concurrent.futures import ThreadPoolExecutor
import math
import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from gateway.local import inprocess_embedding as native


def test_windows_loader_preloads_the_bundled_openmp(monkeypatch, tmp_path):
    bridge_path = tmp_path / "中文目录" / "qwen-runtime" / "seekino-qwen.dll"
    omp, bridge = MagicMock(), MagicMock()
    bridge.qwen_init.return_value = 0
    loader = MagicMock(side_effect=[omp, bridge])
    monkeypatch.setattr(native, "_library_path", lambda: bridge_path)
    monkeypatch.setattr(native.sys, "platform", "win32")
    monkeypatch.setattr(native.ct, "CDLL", loader)

    assert native._load_bridge()._openmp is omp
    assert [call.args[0] for call in loader.call_args_list] == [
        str(bridge_path.with_name("libomp.dll")), str(bridge_path),
    ]
    bridge.qwen_init.assert_called_once_with(str(bridge_path.parent).encode("utf-8"))


def test_packaged_qwen_shares_weights_and_recovers_after_invalid_input(monkeypatch):
    weights = os.environ.get("SEEKINO_QWEN_TEST_WEIGHTS")
    library = os.environ.get("SEEKINO_QWEN_TEST_LIBRARY")
    if not weights or not library:
        pytest.skip("requires the signed desktop CPU bridge and Qwen3 GGUF")
    monkeypatch.setattr(native, "_library_path", lambda: Path(library))
    monkeypatch.setattr(native, "_bridge", None)
    model = native.LocalQwenEmbedder(Path(weights))
    second = native.LocalQwenEmbedder(Path(weights))
    try:
        documents = (
            "The lecture demonstrates backpropagation and computing gradients in micrograd.",
            "A sunset above the ocean lights up the fishing boats in the harbor.",
        )
        with ThreadPoolExecutor(2) as workers:
            vectors = list(workers.map(lambda pair: pair[0].embed_text(pair[1]).vector,
                                       zip((model, second), documents)))
        query = model.embed_query("How do we compute gradients using backpropagation?").vector
        assert model._session is second._session
        for vector in [query, *vectors]:
            assert len(vector) == 1024 and math.hypot(*vector) == pytest.approx(1)
        scores = [sum(a*b for a, b in zip(query, doc)) for doc in vectors]
        assert scores[0] > scores[1] + 0.1
        model.close()
        with pytest.raises(ValueError, match="8192"):
            second.embed_text(" graph" * 9000)
        # A request spanning multiple physical microbatches must still pool the last token.
        assert math.hypot(*second.embed_text(" graph" * 1000).vector) == pytest.approx(1)
        assert second.embed_text(documents[1]).vector == vectors[1]
    finally:
        model.close()
        second.close()
    assert Path(weights).resolve() not in native._sessions
