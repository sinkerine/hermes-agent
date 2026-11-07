"""CLI memory management shares profile state with the room commands."""
import argparse

import pytest

from hermes_cli import projects_cmd, projects_db as db
from hermes_cli.config import atomic_config_write
from tests.fakes.hindsight_standin import HindsightStandin


@pytest.fixture
def cli(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    parser = argparse.ArgumentParser()
    projects_cmd.build_parser(parser.add_subparsers(dest="command"))
    with db.connect_closing() as conn:
        db.create_project(conn, name="Alpha", slug="alpha")
    return lambda *args: projects_cmd.projects_command(parser.parse_args(["project", "memory", *args]))


def test_cli_management(cli, tmp_path, capsys):
    with HindsightStandin() as server:
        atomic_config_write(tmp_path / "config.yaml", {"project_memory": {"hindsight_url": server.url}})
        assert cli("init", "alpha", "--bank", "team") == 0
        assert "Attached bank team" in capsys.readouterr().out
        assert cli("add", "alpha", "kb", "--recall-only") == 0
        assert "warning" in capsys.readouterr().out
        assert cli("set", "alpha", "--retain-shared", "off", "--retain-project", "on") == 0
        assert cli("show", "alpha") == 0
        out = capsys.readouterr().out
        assert "team (rw)" in out and "kb (recall-only)" in out
        assert "retain-shared: off" in out and "retain-project: on" in out
        assert cli("remove", "alpha", "team") == 0
        assert "team" in server.banks
        with db.connect_closing() as conn:
            proj = db.get_project(conn, "alpha")
            assert [b.bank_id for b in db.list_memory_banks(conn, proj.id)] == ["kb"]
            assert not db.get_memory_settings(conn, proj.id).retain_shared


def test_cli_errors_and_offline_attach(cli, capsys):
    assert cli("init", "alpha") == 2
    assert "project: Project memory service is not configured" in capsys.readouterr().err
    with db.connect_closing() as conn:
        assert db.list_memory_banks(conn, db.get_project(conn, "alpha").id) == []
    assert cli("add", "alpha", "../invalid") == 2
    assert cli("remove", "alpha", "missing") == 2
    assert cli("set", "alpha") == 2
    assert cli("show", "unknown") == 1
    assert cli("add", "alpha", "offline") == 0
    assert cli("show", "alpha") == 0
    assert "not configured" in capsys.readouterr().out
