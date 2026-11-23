"""Process-lifetime exclusive locks for settings, database ownership and media leases."""
from contextlib import contextmanager
from pathlib import Path
import os

if os.name == "nt":
    import ctypes
    from ctypes import wintypes
    import hashlib
    import time

    _kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel.CreateEventW.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR)
    _kernel.CreateEventW.restype = wintypes.HANDLE
    _kernel.CloseHandle.argtypes = (wintypes.HANDLE,)

    @contextmanager
    def exclusive_lock(path: Path, *, blocking: bool = True):
        # Windows cannot rename or reap a media directory containing an open lock
        # file. Own a named kernel object instead; process exit releases its handle.
        digest = hashlib.sha256(os.fsencode(os.path.normcase(path.resolve()))).hexdigest()
        name = "Global\\Seekino-" + digest
        while True:
            ctypes.set_last_error(0)
            handle = _kernel.CreateEventW(None, True, False, name)
            code = ctypes.get_last_error()
            if not handle:
                raise ctypes.WinError(code)
            if code != 183:  # ERROR_ALREADY_EXISTS
                break
            _kernel.CloseHandle(handle)
            if not blocking:
                raise BlockingIOError(f"already locked: {path}")
            time.sleep(0.05)
        try:
            yield
        finally:
            _kernel.CloseHandle(handle)
else:
    import fcntl

    @contextmanager
    def exclusive_lock(path: Path, *, blocking: bool = True):
        with path.open("ab") as handle:
            os.set_inheritable(handle.fileno(), False)
            fcntl.flock(handle, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            yield
