"""Monitoring exports follow the opt-in secret redaction policy and preserve structure."""

from __future__ import annotations

from unittest import mock

import pytest

import agent.monitoring.redaction as R


@pytest.mark.parametrize("enabled", [False, True])
def test_secret_key_follows_redaction_policy(monkeypatch, enabled):
    monkeypatch.setattr("agent.redact._REDACT_ENABLED", enabled)
    fake_key = "sk-ant-api03-" + "A" * 24  # constructed to dodge literal-scrubbers
    out = R.redact_for_export(f"calling with key {fake_key} and moving on")
    assert out is not None
    assert (fake_key not in out) is enabled




@pytest.mark.parametrize("enabled", [False, True])
def test_bearer_header_follows_redaction_policy(monkeypatch, enabled):
    monkeypatch.setattr("agent.redact._REDACT_ENABLED", enabled)
    out = R.redact_for_export("Authorization: Bearer abc.def-ghi_jkl")
    assert out is not None
    assert ("abc.def-ghi_jkl" not in out) is enabled








def test_structure_preserved():
    out = R.redact_for_export("platform.slack entered fatal after auth_failed")
    assert out is not None
    assert "platform.slack" in out
    assert "auth_failed" in out


def test_secret_redactor_failure_preserves_export_text():
    with mock.patch("agent.redact.redact_sensitive_text", side_effect=RuntimeError):
        out = R.redact_for_export("secret sauce sk-live-key")
    assert out == "secret sauce sk-live-key"
