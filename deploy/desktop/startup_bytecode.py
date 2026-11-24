"""Precompile the large Python modules on the desktop library's startup path."""
import importlib.util
from pathlib import Path
import py_compile
import re
import sys


# These packages account for most source-compilation time when opening a local library.
# Small modules dominate the module count, but contribute little to launch time.
_STARTUP_PACKAGES = (
    "sqlalchemy", "pydantic", "langchain_core", "langchain", "langchain_openai",
    "langgraph", "langgraph_sdk", "langsmith", "arkruntime", "openai",
)


def compile_startup_bytecode(resources: Path, *, windows: bool) -> None:
    """Compile staged target sources using platform-independent CPython 3.12 bytecode."""
    stdlib = resources / ("Lib" if windows else "python/lib/python3.12")
    # Cross-builds cannot run the Windows DLL. Compare the magic declared by its
    # matching stdlib to the compiler, so incompatible caches cannot ship silently.
    target = (stdlib / "importlib/_bootstrap_external.py").read_bytes()
    magic = re.search(rb"^MAGIC_NUMBER = \((\d+)\)\.to_bytes\(2, 'little'\)", target, re.MULTILINE)
    if (sys.implementation.cache_tag != "cpython-312" or magic is None
            or int(magic[1]).to_bytes(2, "little") + b"\r\n" != importlib.util.MAGIC_NUMBER):
        raise ValueError("desktop bytecode requires a matching CPython 3.12 compiler and bundled runtime")

    directories = [stdlib, *(resources / name for name in ("agent", "desktop", "engine", "gateway", "server")),
                   *(resources / "site-packages" / name for name in _STARTUP_PACKAGES)]
    for directory in directories:
        for source in directory.rglob("*.py"):
            if source.stat().st_size < 8192:
                continue
            cache = source.parent / "__pycache__" / f"{source.stem}.{sys.implementation.cache_tag}.pyc"
            py_compile.compile(
                str(source), cfile=str(cache), dfile=source.relative_to(resources).as_posix(), doraise=True,
                optimize=0, invalidation_mode=py_compile.PycInvalidationMode.CHECKED_HASH,
            )
