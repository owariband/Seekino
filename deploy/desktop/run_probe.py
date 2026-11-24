"""Run a macOS acceptance command with a deadline, including its teardown."""
import argparse
from contextlib import closing
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import time


def _signal_group(pgid, signum):
    try:
        os.killpg(pgid, signum)
    except (ProcessLookupError, PermissionError):
        # Darwin returns EPERM for a group containing only the unreaped
        # leader. The probe and its children run as our unprivileged user.
        return False
    return True


def _wait_for_group_exit(pgid, timeout):
    deadline = time.monotonic() + timeout
    while _signal_group(pgid, 0):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.03)
    return True


def run_probe(command, *, timeout, cwd=None, env=None):
    """Run a macOS foreground probe in a private session, from the main thread."""
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    interrupted = 0
    status = None

    def cancel(signum, _frame):
        nonlocal interrupted
        interrupted = signum

    child = None
    handlers = {}
    try:
        for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
            handlers[sig] = signal.signal(sig, cancel)
        child = subprocess.Popen(command, cwd=cwd, env=env, start_new_session=True)
        deadline = time.monotonic() + timeout
        # Unlike Popen.wait(), NOTE_EXIT observes exit without reaping the
        # leader. Even after an early exit its group ID is ours until wait().
        with closing(select.kqueue()) as queue:
            event = select.kevent(child.pid, filter=select.KQ_FILTER_PROC,
                                  flags=select.KQ_EV_ADD | select.KQ_EV_ONESHOT,
                                  fflags=select.KQ_NOTE_EXIT)
            try:
                queue.control([event], 0, 0)
            except ProcessLookupError:
                pass  # The child exited before registration.
            else:
                while not interrupted:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        status = 124
                        break
                    if queue.control(None, 1, min(remaining, 0.25)):
                        break
        if interrupted:
            status = 128 + interrupted
        if status is not None:
            reason = f"signal {interrupted}" if interrupted else f"{timeout}s deadline"
            print(f"Probe {Path(command[0]).name} (pid {child.pid}): {reason}; stopping its group",
                  file=sys.stderr, flush=True)
    finally:
        try:
            if child is not None:
                try:
                    if _signal_group(child.pid, signal.SIGTERM) and not _wait_for_group_exit(child.pid, 1):
                        _signal_group(child.pid, signal.SIGKILL)
                        if not _wait_for_group_exit(child.pid, 2):
                            raise RuntimeError(f"Probe group {child.pid} survived SIGKILL")
                finally:
                    child.wait(timeout=2)
        finally:
            for sig, previous in handlers.items():
                signal.signal(sig, previous)
    return child.returncode if status is None else status


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=int, default=60, help="maximum duration in seconds")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    cmd = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not cmd or args.timeout <= 0:
        parser.error("supply a positive timeout and a foreground command")
    status = run_probe(cmd, timeout=args.timeout)
    sys.exit(status if status >= 0 else 128 - status)
