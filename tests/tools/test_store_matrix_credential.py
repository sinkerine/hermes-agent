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


def test_registered_tool_has_no_availability_or_credential_check(monkeypatch, tmp_path):
    from tools.registry import registry

    monkeypatch.delenv("HERMES_SESSION_PLATFORM", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    entry = registry.get_entry("store_matrix_credential")
    assert entry is not None
    assert entry.check_fn is None
    assert entry.requires_env == []

    raw_result = registry.dispatch(
        "store_matrix_credential",
        {"env_var": "ANY_DESTINATION", "value": "value"},
    )
    result = raw_result if isinstance(raw_result, dict) else json.loads(raw_result)
    assert result["success"] is True


def test_matrix_storage_isolated_across_profiles(monkeypatch, tmp_path):
    from tools.skills_tool import load_env

    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    for home, value in ((first, "first-sentinel"), (second, "second-sentinel"), (first, "first-sentinel")):
        monkeypatch.setenv("HERMES_HOME", str(home))
        result = json.loads(store_matrix_credential("FIXTURE_KEY", value))
        assert result["success"] is True
        assert result["profile_home"] == str(home)
        assert load_env()["FIXTURE_KEY"] == value

    assert (first / ".env").read_bytes() != (second / ".env").read_bytes()


def test_writer_rejects_only_invalid_destination(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    invalid = json.loads(store_matrix_credential("PATH=bad", "value"))
    assert invalid["success"] is False
    assert not (tmp_path / ".env").exists()
