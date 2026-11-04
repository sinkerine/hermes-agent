"""Sanitized subprocesses and Linux session ownership for the disposable harness."""

import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from common import ROOT, SB, read_json, write_json

ALLOWED_ENV = {
    "PATH", "LANG", "LC_ALL", "TZ", "HOME", "USER", "SHELL", "SSL_CERT_FILE", "SSL_CERT_DIR",
    "HERMES_HOME", "HERMES_RUNTIME_DIR", "HERMES_PYTHON", "PYTHONPYCACHEPREFIX", "TMPDIR", "TMP", "TEMP",
    "XDG_CACHE_HOME", "RUFF_CACHE_DIR", "PNPM_HOME", "COREPACK_HOME", "npm_config_cache", "PNPM_STORE_DIR", "NX_DAEMON",
}


def child_environment():
    return {key: value for key, value in os.environ.items() if key in ALLOWED_ENV}


def proc_state(pid):
    try:
        fields = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8-sig").rsplit(")", 1)[1].split()
        return {"state": fields[0], "group": int(fields[2]), "session": int(fields[3]), "starttime": fields[19]}
    except (FileNotFoundError, ProcessLookupError):
        return None


def starttime(pid):
    state = proc_state(pid)
    return state["starttime"] if state else None


def group_members(pid):
    members = []
    for path in Path("/proc").iterdir():
        if path.name.isdecimal():
            state = proc_state(int(path.name))
            if state and state["group"] == pid and state["state"] != "Z":
                assert state["session"] == pid, "Owned group changed session"
                members.append(int(path.name))
    return members


def launch(args, receipt, owner, **kwargs):
    assert not receipt.exists(), "Stop the previous sandbox run first"
    previous = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT, signal.SIGTERM})
    try:
        child = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), *args], cwd=kwargs.pop("cwd", ROOT),
            env=child_environment(), start_new_session=True, **kwargs,
        )
        stamp = starttime(child.pid)
        assert stamp is not None, "Guardian exited before ownership receipt"
        write_json(receipt, {"pid": child.pid, "starttime": stamp, "owner": owner})
        return child
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous)


def stop_receipt(receipt, owner, grace=5):
    if not receipt.exists():
        return
    record = read_json(receipt)
    assert record["owner"] == owner
    pid = record["pid"]
    members = group_members(pid)
    if members:
        # The guardian remains session leader until every non-zombie descendant exits.
        # Never adopt a reused PID or an orphaned group with unverifiable ownership.
        assert starttime(pid) == record["starttime"], "PID identity changed; retain receipt and refuse purge"
        state = proc_state(pid)
        assert state["group"] == state["session"] == pid
        signal_group(pid, signal.SIGTERM)
        deadline = time.monotonic() + grace
        while group_members(pid) and time.monotonic() < deadline:
            time.sleep(0.02)
        if group_members(pid):
            assert starttime(pid) == record["starttime"], "PID identity changed during cleanup"
            signal_group(pid, signal.SIGKILL)  # windows-footgun: ok — Linux-only /proc harness
        deadline = time.monotonic() + 5
        while group_members(pid) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert not group_members(pid), "Owned group survived; retain receipt and refuse purge"
    receipt.unlink()


def signal_group(pid, signum):
    try:
        os.killpg(pid, signum)  # windows-footgun: ok — caller verifies Linux session identity
    except ProcessLookupError:
        assert not group_members(pid), "Group vanished during signal; verify cleanup"


def guardian(args):
    # Keep the group identity alive when a command leader exits before its children.
    signal.signal(signal.SIGINT, lambda *_: None)
    signal.signal(signal.SIGTERM, lambda *_: None)
    signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGINT, signal.SIGTERM})
    child = subprocess.Popen(args, env=child_environment())
    result = child.wait()  # health: allow HX006 -- parent owns the deadline and signals the entire session on timeout
    while any(pid != os.getpid() for pid in group_members(os.getpid())):
        time.sleep(0.02)
    return result if result >= 0 else 128 - result


if __name__ == "__main__":
    raise SystemExit(guardian(sys.argv[1:]))
