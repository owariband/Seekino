"""A hung native acceptance command must not leave its hosted app running."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS probe process groups")
@pytest.mark.parametrize(("outcome", "status"), [
    ("deadline", 124), ("interrupted", 128 + signal.SIGTERM), ("early_exit", 0),
])
def test_probe_reaps_session_with_unresponsive_host(tmp_path, outcome, status):
    cli = Path(__file__).resolve().parents[2] / "deploy/desktop/run_probe.py"
    ready = tmp_path / "ready"
    received = tmp_path / "term"
    # The observer exits on TERM. Its hosted process records TERM but hangs
    # until killed. Readiness means both the test child and its signal handler exist.
    host = (
        "import os, signal, sys, time; from pathlib import Path; "
        "signal.signal(signal.SIGTERM, lambda *_: Path(sys.argv[2]).touch()); "
        "ready=Path(sys.argv[1]); tmp=ready.with_suffix('.new'); "
        "tmp.write_text(str(os.getpgrp())); tmp.replace(ready); time.sleep(10)"
    )
    observer = """
import subprocess, sys, time
from pathlib import Path
child = subprocess.Popen(sys.argv[2:])
if sys.argv[1] == 'early_exit':
    while not Path(sys.argv[-2]).exists():
        time.sleep(0.01)
    sys.exit(0)
sys.exit(child.wait())
"""
    command = [sys.executable, "-B", cli, "--timeout", "2" if outcome == "deadline" else "5", "--",
               sys.executable, "-B", "-c", observer, outcome, sys.executable, "-B", "-c",
               host, ready, received]
    with subprocess.Popen(command) as runner:
        deadline = time.monotonic() + 3
        while not ready.exists():
            assert runner.poll() is None and time.monotonic() < deadline
            time.sleep(0.03)
        pgid = int(ready.read_text())
        assert pgid != os.getpgrp()
        if outcome == "interrupted":
            runner.terminate()
        assert runner.wait(timeout=7) == status
    assert received.is_file()
    deadline = time.monotonic() + 2
    while True:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            break
        assert time.monotonic() < deadline, "test app survived its supervisor"
        time.sleep(0.03)
