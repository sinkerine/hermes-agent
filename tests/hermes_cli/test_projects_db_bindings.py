"""Room binding persistence, upgrades and dangling project references."""
import sqlite3

import pytest

from gateway.project_routing import ProjectRouteError, validate_project
from hermes_cli import projects_db as db


KEY = {"platform": "matrix", "transport_profile": "default", "chat_id": "!room:example"}


def test_binding_crud_keeps_creation_and_isolates_keys(tmp_path, monkeypatch):
    path = tmp_path / "projects.db"
    with db.connect_closing(db_path=path) as conn:
        alpha = db.create_project(conn, name="Alpha")
        beta = db.create_project(conn, name="Beta")
        assert db.get_chat_binding(conn, **KEY) is None
        monkeypatch.setattr(db, "_now", lambda: 100)
        first = db.set_chat_binding(conn, **KEY, project_id=alpha, bound_by="@first")
        assert first.created_at == first.updated_at == 100
        monkeypatch.setattr(db, "_now", lambda: 200)
        updated = db.set_chat_binding(conn, **KEY, project_id=beta, bound_by="@second")
        assert (updated.created_at, updated.updated_at, updated.bound_by) == (100, 200, "@second")
        assert updated.project_id == beta
        for field in KEY:
            other_key = {**KEY, field: "other"}
            assert db.get_chat_binding(conn, **other_key) is None
            db.set_chat_binding(conn, **other_key, project_id=alpha)
        assert db.get_chat_binding(conn, **KEY) == updated
    with db.connect_closing(db_path=path) as conn:
        assert db.get_chat_binding(conn, **KEY) == updated
        assert db.delete_chat_binding(conn, **KEY)
        assert not db.delete_chat_binding(conn, **KEY)
        assert db.get_chat_binding(conn, **{**KEY, "transport_profile": "other"}) is not None


def test_unknown_project_id_refused_without_changing_binding(tmp_path):
    with db.connect_closing(db_path=tmp_path / "projects.db") as conn:
        project_id = db.create_project(conn, name="Alpha", slug="alpha")
        original = db.set_chat_binding(conn, **KEY, project_id=project_id)
        for invalid in ("missing", "alpha", "' OR 1=1 --"):
            with pytest.raises(ValueError, match="no such project"):
                db.set_chat_binding(conn, **KEY, project_id=invalid)
            assert db.get_chat_binding(conn, **KEY) == original
        assert conn.execute("SELECT count(*) FROM projects").fetchone()[0] == 1


def test_deleted_project_leaves_binding_for_fail_closed_lookup(tmp_path):
    with db.connect_closing(db_path=tmp_path / "projects.db") as conn:
        project_id = db.create_project(conn, name="Alpha")
        binding = db.set_chat_binding(conn, **KEY, project_id=project_id)
        assert db.delete_project(conn, project_id)
        assert db.get_chat_binding(conn, **KEY) == binding
        with pytest.raises(ProjectRouteError) as error:
            validate_project(db.get_project(conn, binding.project_id), binding.project_id)
        assert error.value.reason == "missing"


def test_legacy_db_upgrades_without_losing_projects(tmp_path):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as legacy:
        legacy.executescript(
            "CREATE TABLE projects (id TEXT PRIMARY KEY, slug TEXT NOT NULL UNIQUE, "
            "name TEXT NOT NULL, description TEXT, created_at INTEGER NOT NULL, archived INTEGER DEFAULT 0);"
            "INSERT INTO projects VALUES ('p_old', 'old', 'Old', 'kept', 12, 0);"
        )
    for _ in range(2):
        with db.connect_closing(db_path=path) as conn:
            project = db.get_project(conn, "p_old")
            assert (project.name, project.description, project.created_at) == ("Old", "kept", 12)
            db.set_chat_binding(conn, **KEY, project_id=project.id)
            assert db.get_chat_binding(conn, **KEY).project_id == project.id
