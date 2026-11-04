"""Offline budget migration and targeted evaluation checks; no listeners or credentials."""

import argparse
from pathlib import Path
import tempfile
from unittest.mock import patch

from common import SB, read_json, write_json
import evaluate
import fake_openai
import lifecycle


def budget_checks(directory):
    path = directory / "evidence/live-budget.json"
    with patch.object(fake_openai, "SB", directory):
        fake_openai.configure_live_budget()
        assert read_json(path) == {"limit": 30, "reserved_requests": 0}
        write_json(path, {"limit": 30, "reserved_requests": 30})
        fake_openai.configure_live_budget()
        assert fake_openai.reserve_live() is None
        assert read_json(path) == {"limit": 30, "reserved_requests": 30}
        fake_openai.configure_live_budget(42)
        assert read_json(path) == {"limit": 42, "reserved_requests": 30}
        assert fake_openai.reserve_live() is None  # Default runtime cannot spend the extension.
        for expected in range(31, 43):
            # A fresh configuration/read each time simulates restarts without resetting reservations.
            fake_openai.configure_live_budget(42)
            assert fake_openai.reserve_live(42) == expected
        fake_openai.configure_live_budget()
        assert read_json(path) == {"limit": 42, "reserved_requests": 42}
        assert fake_openai.reserve_live(42) is None
        assert fake_openai.reserve_live() is None
        write_json(path, {"limit": 10, "reserved_requests": 10})
        fake_openai.configure_live_budget()
        assert read_json(path) == {"limit": 10, "reserved_requests": 10}
        assert fake_openai.reserve_live() is None
    for value in ("0", "43", "-1"):
        try:
            fake_openai.live_request_limit(value)
        except argparse.ArgumentTypeError:
            pass
        else:
            raise AssertionError("Out-of-range cap accepted")
    assert fake_openai.live_request_limit("1") == 1
    assert fake_openai.live_request_limit("42") == 42


def lifecycle_limit_checks(directory):
    marker = directory / ".harness-owner"
    marker.write_text(lifecycle.OWNER, encoding="utf-8")
    with patch.object(lifecycle, "SB", directory), patch.object(lifecycle, "preflight", return_value=True), \
            patch.object(lifecycle, "synapse"), patch.object(lifecycle, "run"), \
            patch.object(lifecycle, "element"), patch.object(lifecycle, "provider_ready"), \
            patch.object(lifecycle, "start") as start, patch("builtins.print"), \
            patch("sys.argv", ["lifecycle.py", "up", "--live", "--live-request-limit", "42"]):
        lifecycle.main()
        provider = start.call_args_list[0].args
        assert provider[0] == "provider" and provider[1][-3:] == ["--live", "--live-request-limit", "42"]
    with patch.object(lifecycle, "preflight") as preflight, \
            patch("sys.argv", ["lifecycle.py", "up", "--live-request-limit", "42"]):
        try:
            lifecycle.main()
        except SystemExit as error:
            assert error.code == 2
        else:
            raise AssertionError("Limit accepted without explicit live mode")
        preflight.assert_not_called()


def targeted_evaluation_checks(directory):
    (directory / "run/provider.json").parent.mkdir(parents=True)
    write_json(directory / "run/provider.json", {})
    write_json(directory / "run/provider-mode.json", {"mode": "deterministic"})
    original = directory / "evidence/deterministic-eval.json"
    write_json(original, {"historical": "preserve"})

    def response(_url, data, **_kwargs):
        assert any(f"[case:{case}]" in data["messages"][1]["content"] for case in ("invalid", "fact"))
        message, reason, _ = fake_openai.completion(data)
        return {"choices": [{"message": message, "finish_reason": reason}]}

    with patch.object(evaluate, "SB", directory), patch.object(evaluate, "request", response), \
            patch("sys.argv", ["evaluate.py", "--mode", "deterministic", "--case", "invalid"]):
        evaluate.main()
        evaluate.main()
    reports = list((directory / "evidence").glob("deterministic-eval-invalid-*.json"))
    assert len(reports) == 2
    assert read_json(original) == {"historical": "preserve"}
    for path in reports:
        report = read_json(path)
        assert [row["id"] for row in report["cases"]] == ["invalid"]
        assert report["cases"][0]["requests"] == 3
        assert report["forced_invalid_self_corrected"] is True
    with patch.object(evaluate, "SB", directory), patch.object(evaluate, "request", response), \
            patch("sys.argv", ["evaluate.py", "--mode", "deterministic", "--case", "fact"]):
        evaluate.main()
    fact = read_json(next((directory / "evidence").glob("deterministic-eval-fact-*.json")))
    assert fact["forced_invalid_self_corrected"] is None
    assert len(fact["cases"]) == 1 and fact["cases"][0]["requests"] == 1
    with patch.object(evaluate, "request") as request, \
            patch("sys.argv", ["evaluate.py", "--mode", "live", "--case", "typo"]):
        try:
            evaluate.main()
        except SystemExit as error:
            assert error.code == 2
        else:
            raise AssertionError("Unknown selection accepted")
        request.assert_not_called()


def main():
    with tempfile.TemporaryDirectory(prefix="quality-", dir=SB / "tmp") as temporary:
        directory = Path(temporary)
        budget_checks(directory)
        lifecycle_limit_checks(directory)
        targeted_evaluation_checks(directory)
    print("Offline quality: persistent 30→42 migration, default cap, restart exhaustion, case selection and distinct receipts passed.")
