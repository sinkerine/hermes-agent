"""Plaintext Matrix credential storage contracts."""

import json

from tools.store_matrix_credential import store_matrix_credential


def test_matrix_credential_stores_exact_value_in_active_profile(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "matrix")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    result = json.loads(store_matrix_credential("FIXTURE_API_KEY", "  exact=value  "))

    assert result["success"] is True
    assert result["stored_as"] == "FIXTURE_API_KEY"
    assert (tmp_path / ".env").read_text()  # canonical writer created it
    assert "FIXTURE_API_KEY" in (tmp_path / ".env").read_text()
    assert "exact=value" in (tmp_path / ".env").read_text()


def test_ambiguous_non_matrix_or_invalid_destination_never_writes(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "matrix")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    invalid = json.loads(store_matrix_credential("PATH=bad", "value"))
    assert invalid["success"] is False
    assert not (tmp_path / ".env").exists()

    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "telegram")
    rejected = json.loads(store_matrix_credential("OTHER_KEY", "value"))
    assert rejected == {"success": False, "error_code": "matrix_only"}
    assert not (tmp_path / ".env").exists()
