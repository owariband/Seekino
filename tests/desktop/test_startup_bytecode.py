"""The cross-builder must cache target source and detect changes after relocation."""
from importlib import util
from importlib.machinery import SourceFileLoader
from pathlib import Path
import sysconfig

import pytest

from deploy.desktop.startup_bytecode import compile_startup_bytecode


@pytest.mark.parametrize("windows", [False, True])
def test_checked_target_bytecode_survives_relocation(tmp_path, windows, monkeypatch):
    resources = tmp_path / "stage"
    stdlib = resources / ("Lib" if windows else "python/lib/python3.12")
    bootstrap = stdlib / "importlib/_bootstrap_external.py"
    bootstrap.parent.mkdir(parents=True)
    bootstrap.write_bytes((Path(sysconfig.get_path("stdlib")) / "importlib/_bootstrap_external.py").read_bytes())
    package = resources / "desktop"
    package.mkdir()
    source = package / "sample.py"
    source.write_bytes(b"#" + b"." * 8192 + b"\nvalue = 1\n")
    compile_startup_bytecode(resources, windows=windows)

    relocated = resources.rename(tmp_path / "Moved Seekino")
    source = relocated / source.relative_to(resources)
    loader = SourceFileLoader("sample", str(source))
    cache = Path(util.cache_from_source(str(source))).read_bytes()
    assert cache[:4] == util.MAGIC_NUMBER
    assert cache[4:8] == (3).to_bytes(4, "little")  # hash-based, checked against source
    assert cache[8:16] == util.source_hash(source.read_bytes())
    with monkeypatch.context() as patch:
        patch.setattr(loader, "source_to_code", lambda *_: pytest.fail("packaged cache was skipped"))
        assert loader.get_code("sample").co_filename == str(source)
    source.write_bytes(source.read_bytes().replace(b"value = 1", b"value = 2"))
    namespace = {}
    exec(loader.get_code("sample"), namespace)
    assert namespace["value"] == 2


def test_cross_builder_rejects_incompatible_python(tmp_path):
    bootstrap = tmp_path / "Lib/importlib/_bootstrap_external.py"
    bootstrap.parent.mkdir(parents=True)
    bootstrap.write_bytes(b"MAGIC_NUMBER = (0).to_bytes(2, 'little') + b'\\r\\n'\n")
    with pytest.raises(ValueError, match="matching CPython"):
        compile_startup_bytecode(tmp_path, windows=True)
