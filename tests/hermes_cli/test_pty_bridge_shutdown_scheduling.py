"""Real PTY shutdown with a delayed leader and job-control process groups."""
import asyncio
import os
import re
import shlex
import signal
import sys
import threading

import psutil
import pytest

from tests.waiting import wait_until


@pytest.mark.platforms("posix")
@pytest.mark.live_system_guard_bypass
def test_close_preserves_helper_grace_when_leader_cannot_exit(tmp_path):
    """A descheduled leader must not shorten another group member's save grace."""
    from hermes_cli.pty_bridge import PtyBridge
    from tests.hermes_cli.test_pty_bridge import _read_until

    marker = tmp_path / "saved"
    script = tmp_path / "helper.sh"
    script.write_text(
        f"trap 'trap \"\" HUP; sleep 0.6; echo ok > {shlex.quote(str(marker))}; exit 0' HUP\n"
        "echo armed\n"
        "while :; do sleep 0.05; done\n"
    )
    bridge = PtyBridge.spawn(["/bin/sh", "-c", f"/bin/sh {shlex.quote(str(script))} & exec sleep 60"])
    try:
        assert b"armed" in _read_until(bridge, b"armed")
        os.kill(bridge.pid, signal.SIGSTOP)
        wait_until(lambda: psutil.Process(bridge.pid).status() == psutil.STATUS_STOPPED,
                   what="PTY leader stopped before shutdown")
        bridge.close()
        assert marker.read_text().strip() == "ok"
    finally:
        bridge.close()


@pytest.mark.platforms("posix")
@pytest.mark.live_system_guard_bypass
def test_close_ends_foreground_job_in_its_own_process_group():
    """An interactive shell puts foreground programs outside its own process group."""
    from hermes_cli.pty_bridge import PtyBridge

    program = (
        "import os,signal; signal.signal(signal.SIGHUP, signal.SIG_IGN); "
        "print('FORE'+'GROUND='+str(os.getpid()), flush=True); signal.pause()"
    )
    bridge = PtyBridge.spawn(["/bin/sh", "-i"])
    helper = None
    try:
        # Use a split response marker so readiness comes from the child.
        command = f"stty -echo; {shlex.quote(sys.executable)} -c {shlex.quote(program)}\n"
        asyncio.run(bridge.write(command.encode()))
        output = bytearray()

        def foreground_pid():
            output.extend(bridge.read(timeout=0.1) or b"")
            return re.search(rb"FOREGROUND=(\d+)\r?\n", output)

        match = wait_until(foreground_pid, what="foreground job PID response")
        helper = psutil.Process(int(match[1]))
        assert os.getpgid(helper.pid) != os.getpgid(bridge.pid)
        bridge.close()
        assert not helper.is_running() or helper.status() == psutil.STATUS_ZOMBIE
    finally:
        if helper is not None:
            try:
                helper.kill()
            except psutil.NoSuchProcess:
                pass
        bridge.close()


@pytest.mark.platforms("posix")
@pytest.mark.live_system_guard_bypass
@pytest.mark.parametrize("stop_leader", [False, True])
def test_close_preserves_helper_grace_after_delayed_hup_dispatch(tmp_path, monkeypatch, stop_leader):
    """Time spent waiting to dispatch HUP must not consume the helper's save grace."""
    from hermes_cli import pty_bridge
    from tests.hermes_cli.test_pty_bridge import _read_until

    marker, handling = tmp_path / "saved", tmp_path / "handling"
    script = tmp_path / "helper.sh"
    script.write_text(
        f"trap 'trap \"\" HUP; echo handling > {shlex.quote(str(handling))}; "
        f"sleep 0.6; echo ok > {shlex.quote(str(marker))}; exit 0' HUP\n"
        "echo armed\nwhile :; do sleep 0.05; done\n"
    )
    bridge = pty_bridge.PtyBridge.spawn(
        ["/bin/sh", "-c", f"/bin/sh {shlex.quote(str(script))} & exec sleep 60"]
    )
    entered, release = threading.Event(), threading.Event()
    real_killpg = os.killpg
    errors = []
    worker = None
    descendants = []

    def dispatch(pgid, sig):
        if pgid == bridge.pid and sig == signal.SIGHUP:
            entered.set()
            assert release.wait(10), "HUP dispatch barrier was not released"
        return real_killpg(pgid, sig)

    def close():
        try:
            bridge.close()
        except BaseException as exc:
            errors.append(exc)

    try:
        assert b"armed" in _read_until(bridge, b"armed")
        descendants = psutil.Process(bridge.pid).children(recursive=True)
        if stop_leader:
            os.kill(bridge.pid, signal.SIGSTOP)
            wait_until(lambda: psutil.Process(bridge.pid).status() == psutil.STATUS_STOPPED,
                       what="PTY leader stopped before delayed dispatch")
        monkeypatch.setattr(os, "killpg", dispatch)
        worker = threading.Thread(target=close)
        worker.start()
        assert entered.wait(10), "close did not reach HUP dispatch"
        # Consume the existing grace BEFORE delivery, without relying on CPU contention.
        assert not release.wait(pty_bridge._helper_shutdown_grace())
        release.set()
        worker.join(10)
        assert not worker.is_alive(), "PTY close did not finish"
        assert not errors
        assert handling.read_text().strip() == "handling"
        assert marker.read_text().strip() == "ok"
    finally:
        release.set()
        if worker is not None:
            worker.join(10)
        bridge.close()
        for child in descendants:
            try:
                child.kill()
            except psutil.NoSuchProcess:
                pass
