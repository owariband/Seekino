"""Executed by the embedded interpreter in verify_host.py's disposable bundle."""
import ctypes
import importlib.util
import json
import os
from pathlib import Path
import sys


process_events = []


def reject_process(event, args):
    if event in {"subprocess.Popen", "os.system", "os.posix_spawn", "os.fork", "os.forkpty", "os.exec"}:
        process_events.append(event)
        raise RuntimeError(f"desktop backend attempted a child process: {event}")


sys.addaudithook(reject_process)
for driver in ("pymysql", "elasticsearch", "elastic_transport", "redis"):
    assert importlib.util.find_spec(driver) is None, driver

import pytest
from _pipeline_test import test_import_parse_search_and_open_original, test_resume_two_chunks_and_refine_their_shared_interval

for run in (test_import_parse_search_and_open_original, test_resume_two_chunks_and_refine_their_shared_interval):
    sample = Path(sys.argv[1]) / run.__name__
    sample.mkdir()
    with pytest.MonkeyPatch.context() as patch:
        run(sample, patch)
assert not process_events, process_events

dyld = ctypes.CDLL(None)
dyld._dyld_image_count.restype = ctypes.c_uint32
dyld._dyld_get_image_name.argtypes = [ctypes.c_uint32]
dyld._dyld_get_image_name.restype = ctypes.c_char_p
names = ("libpython3.12", "libavcodec", "libavformat", "libavutil", "libswscale", "libswresample", "vec0")
contents = Path(__file__).resolve().parents[3]
loaded = {}
for i in range(dyld._dyld_image_count()):
    path = Path(os.fsdecode(dyld._dyld_get_image_name(i))).resolve()
    for name in names:
        if path.name.startswith(name):
            assert path.is_relative_to(contents), path
            loaded[name] = str(path.relative_to(contents))
# SQLite unloads its extension on connection close. Inspect a live connection too.
import sqlite3
import sqlite_vec
with sqlite3.connect(":memory:") as connection:
    connection.enable_load_extension(True)
    sqlite_vec.load(connection)
    connection.enable_load_extension(False)
    assert connection.execute("SELECT vec_version()").fetchone()
    paths = [Path(os.fsdecode(dyld._dyld_get_image_name(i))).resolve() for i in range(dyld._dyld_image_count())]
    vec, = [p for p in paths if p.name == "vec0.dylib"]
    assert vec.is_relative_to(contents)
    loaded["vec0"] = str(vec.relative_to(contents))
assert set(loaded) == set(names), loaded
print(json.dumps({"pid": os.getpid(), "process_events": process_events, "loaded": loaded}, indent=2))
