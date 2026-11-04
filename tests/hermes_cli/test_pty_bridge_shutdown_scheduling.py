"""Real PTY shutdown with a delayed leader and job-control process groups."""
import asyncio
import os
import re
import shlex
import signal
import sys

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
