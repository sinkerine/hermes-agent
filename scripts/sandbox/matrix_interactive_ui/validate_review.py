"""Offline regression checks for T8 review fixes; only disposable processes and stores."""

from copy import deepcopy
from http.client import HTTPException, IncompleteRead
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

from common import ACTION_KEY, SB, read_json, write_json
import evaluate
import lifecycle
from processes import group_members, launch, proc_state, stop_receipt
from replay_action import prepare_content


def wait_for(predicate, seconds=5):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("Disposable test synchronization timed out")


def environment_checks(directory):
    output = directory / "environment.json"
    excluded = {"MUI_SECRET_SENTINEL": "harmless", "OPENAI_API_KEY": "harmless", "NODE_OPTIONS": "harmless",
                "PYTHONPATH": "harmless", "BASH_ENV": "harmless", "npm_config_script_shell": "harmless"}
    code = "import json,os,sys;json.dump(dict(os.environ),open(sys.argv[1],'w',encoding='utf-8'))"
    with patch.dict(os.environ, excluded):
        # This is the same run path used by bootstrap and pnpm lifecycle commands.
        lifecycle.run([sys.executable, "-c", code, str(output)])
        env = read_json(output)
        assert not set(excluded).intersection(env), "Command inherited excluded sentinels"
        for key in ("PNPM_HOME", "COREPACK_HOME", "npm_config_cache", "PNPM_STORE_DIR", "NX_DAEMON",
                    "HERMES_HOME", "HERMES_RUNTIME_DIR", "PYTHONPYCACHEPREFIX", "PATH"):
            assert env[key] == os.environ[key], key
        output.unlink()
        with patch.object(lifecycle, "SB", directory):
            (directory / "run").mkdir()
            (directory / "evidence").mkdir()
            lifecycle.start("sentinel", [sys.executable, "-c", code, str(output)])
            try:
                wait_for(lambda: output.exists() and output.stat().st_size > 0)
                assert not set(excluded).intersection(read_json(output))
            finally:
                lifecycle.stop("sentinel")


def descendant_command(marker):
    # The command leader exits; its child ignores TERM and stays in the owned group.
    code = ("import os,signal,sys,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);"
            "open(sys.argv[1],'w',encoding='utf-8').write(str(os.getpid()));time.sleep(30)")
    return [sys.executable, "-c", "import subprocess,sys;subprocess.Popen([sys.executable,'-c',sys.argv[1],sys.argv[2]])",
            code, str(marker)]


def lifecycle_checks(directory):
    receipt = directory / "descendant.json"
    marker = directory / "descendant.pid"
    child = launch(descendant_command(marker), receipt, lifecycle.OWNER)
    try:
        wait_for(marker.exists)
        descendant = int(marker.read_text(encoding="utf-8-sig"))
        wait_for(lambda: len(group_members(child.pid)) == 2)
        assert proc_state(descendant)["group"] == child.pid and child.poll() is None
        # A forged/reused start time must retain the receipt and leave the child alone.
        record = read_json(receipt)
        write_json(receipt, {**record, "starttime": "invalid"})
        try:
            stop_receipt(receipt, lifecycle.OWNER, grace=0.1)
        except AssertionError:
            assert receipt.exists() and proc_state(descendant)["state"] != "Z"
            # Purge must stop at the same ownership check, before removing mutable state.
            purge_root = directory / "purge"
            (purge_root / "run").mkdir(parents=True)
            (purge_root / "synapse").mkdir()
            (purge_root / ".harness-owner").write_text(lifecycle.OWNER, encoding="utf-8")
            write_json(purge_root / "run/command-test.json", {**record, "starttime": "invalid"})
            with patch.object(lifecycle, "SB", purge_root):
                try:
                    lifecycle.down(True)
                except AssertionError:
                    assert (purge_root / "synapse").exists()
                    assert (purge_root / "run/command-test.json").exists()
                else:
                    raise AssertionError("Purge accepted an unverifiable live group")
        else:
            raise AssertionError("Mismatched PID start time accepted")
        finally:
            write_json(receipt, record)
        stop_receipt(receipt, lifecycle.OWNER, grace=0.1)
        child.wait(timeout=5)
        assert not group_members(child.pid) and not receipt.exists()
    finally:
        stop_receipt(receipt, lifecycle.OWNER, grace=0.1)
        child.wait(timeout=5)

    marker = directory / "timeout.pid"
    before = set((SB / "run").glob("command-*.json"))
    try:
        lifecycle.run(descendant_command(marker), timeout=1)
    except subprocess.TimeoutExpired:
        assert marker.exists()
        state = proc_state(int(marker.read_text(encoding="utf-8-sig")))
        assert state is None or state["state"] == "Z"
        assert set((SB / "run").glob("command-*.json")) == before
    else:
        raise AssertionError("Command did not time out")


def signal_checks(directory):
    helper = directory / "interrupt.py"
    helper.write_text(
        "import sys,signal\n"
        f"sys.path.insert(0,{str(Path(__file__).resolve().parent)!r})\n"
        "import lifecycle\nfrom common import guard\nguard()\n"
        "signal.signal(signal.SIGINT,lifecycle.interrupted)\n"
        "signal.signal(signal.SIGTERM,lifecycle.interrupted)\n"
        "lifecycle.run([sys.executable,'-c',"
        "\"import os,sys,time;open(sys.argv[1],'w',encoding='utf-8').write(str(os.getpid()));time.sleep(30)\",sys.argv[1]])\n",
        encoding="utf-8",
    )
    for sig in (signal.SIGINT, signal.SIGTERM):
        marker = directory / f"signal-{sig}.pid"
        parent = subprocess.Popen([sys.executable, str(helper), str(marker)])
        try:
            wait_for(marker.exists)
            pid = int(marker.read_text(encoding="utf-8-sig"))
            group = proc_state(pid)["group"]
            parent.send_signal(sig)
            assert parent.wait(timeout=10) == 128 + sig
            assert not group_members(group)
            assert not any(read_json(p)["pid"] == group for p in (SB / "run").glob("command-*.json"))
        finally:
            if parent.poll() is None:
                parent.send_signal(signal.SIGTERM)
                parent.wait(timeout=10)


def replay_checks(directory):
    path = directory / "store.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE uis (ui_event_id TEXT, room_id TEXT, ui_id TEXT, revision INTEGER, status TEXT)")
    connection.execute("INSERT INTO uis VALUES ('$event','!room:localhost','sandbox_ui',1,'submitted')")
    connection.commit()
    connection.close()
    content = {"msgtype": "m.text", "body": "test", "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread"},
               ACTION_KEY: {"ui_event_id": "$event", "ui_id": "sandbox_ui", "revision": 0, "nonce": "a" * 24}}
    original = deepcopy(content)
    assert prepare_content(content, "!room:localhost", "duplicate", path) == content
    for mode, revision in (("stale", 0), ("inactive", 1)):
        result = prepare_content(content, "!room:localhost", mode, path)
        assert result[ACTION_KEY]["revision"] == revision
        assert result[ACTION_KEY]["nonce"] != content[ACTION_KEY]["nonce"]
        assert result["m.relates_to"] == content["m.relates_to"]
    assert content == original
    for field, value in (("ui_event_id", "$other"), ("ui_id", "other")):
        wrong = deepcopy(content)
        wrong[ACTION_KEY][field] = value
        assert_replay_rejected(wrong, "!room:localhost", path)
    assert_replay_rejected(content, "!other:localhost", path)


def assert_replay_rejected(content, room, path):
    try:
        prepare_content(content, room, "inactive", path)
    except AssertionError:
        return
    raise AssertionError("Unverified replay identity accepted")


def evaluator_failure_check(exception):
    def failure(*_args, **kwargs):
        assert kwargs["timeout"] > 90
        raise exception
    with patch.object(evaluate, "request", failure):
        row = evaluate.evaluate({"id": "transport", "ui_useful": True, "prompt": "offline"})
    assert row["status"] == "failed" and row["requests"] == 1
    # Exercise the real report writer after transport failure, without an HTTP listener.
    mode = f"offline-transport-check-{type(exception).__name__}"
    with patch.object(evaluate.argparse.ArgumentParser, "parse_args", return_value=type("Args", (), {"mode": mode})()), \
            patch.object(evaluate, "read_json", return_value={"mode": mode}), \
            patch.object(evaluate.Path, "exists", return_value=True), \
            patch.object(evaluate, "request", failure):
        try:
            evaluate.main()
        except AssertionError:
            pass  # Forced-invalid gate must fail after saving all failed-case receipts.
    report = read_json(SB / f"evidence/{mode}-eval.json")
    assert len(report["failed_cases"]) == len(report["cases"]) == 9
    assert all(case["status"] == "failed" and case["requests"] == 1 for case in report["cases"])
    for case in [row, *report["cases"]]:
        assert case["failure"] == {
            "kind": "transport_error", "exception": type(exception).__name__,
            "budget_may_be_spent": True, "ledger": "evidence/live-budget.json",
        }
    for serialized in (json.dumps(row), json.dumps(report)):
        assert str(exception) not in serialized
        assert "harmless sentinel" not in serialized
    return row


def evaluator_checks():
    for exception in (TimeoutError("harmless sentinel must not appear in receipt"),
                      IncompleteRead(b"", 1), HTTPException("harmless sentinel HTTP content")):
        row = evaluator_failure_check(exception)
    return row


def main():
    with tempfile.TemporaryDirectory(prefix="review-", dir=SB / "tmp") as temporary:
        directory = Path(temporary)
        environment_checks(directory)
        lifecycle_checks(directory)
        signal_checks(directory)
        replay_checks(directory)
        failure = evaluator_checks()
    write_json(SB / "evidence/offline-review.json", {
        "status": "passed", "checks": ["command/service environment sentinels and cache retention",
        "surviving descendant and PID reuse refusal", "timeout cleanup", "SIGINT/SIGTERM cleanup",
        "store-verified revision-zero export probes and identity refusal", "evaluator timeout/failure receipt"],
        "synthetic_failed_case": failure,
    })
    print("Offline review: environment, descendant/timeout/signals, replay identity/revisions, evaluator failure passed.")
