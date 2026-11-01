from __future__ import annotations

import sys

import pytest

from scripts.local_whisper import run_with_metal_required


@pytest.mark.parametrize("backend", ["no GPU found", "whisper_backend_init_gpu: using CUDA0 backend"])
def test_metal_required_stops_non_metal_before_inference(tmp_path, backend):
    marker = tmp_path / "cpu-work-started"
    code = ("import sys,time,pathlib; "
            f"print({backend!r},file=sys.stderr,flush=True); "
            "print('whisper server listening',file=sys.stderr,flush=True); "
            "time.sleep(1); "
            f"pathlib.Path({str(marker)!r}).write_text('unexpected inference')")
    with pytest.raises(RuntimeError, match="refusing CPU fallback"):
        run_with_metal_required([sys.executable, "-u", "-c", code])
    assert not marker.exists()


def test_metal_required_accepts_confirmed_backend():
    code = ("import sys; "
            "print('whisper_backend_init_gpu: using MTL0 backend',file=sys.stderr,flush=True); "
            "print('whisper server listening',file=sys.stderr,flush=True)")
    run_with_metal_required([sys.executable, "-u", "-c", code])
