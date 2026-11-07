"""Project bank ordering, retain settings, migration and transaction contracts."""
import sqlite3

import pytest

from hermes_cli import projects_db as db


@pytest.fixture
def store(tmp_path):
    with db.connect_closing(db_path=tmp_path / "projects.db") as conn:
        yield conn, db.create_project(conn, name="Alpha")


def test_bank_append_update_remove_and_project_isolation(store, monkeypatch):
    conn, project_id = store
    other = db.create_project(conn, name="Beta")
    monkeypatch.setattr(db, "_now", lambda: 100)
    first, created = db.add_memory_bank(conn, project_id, " A.bank:1 ", added_by="cli")
    assert created
    assert (first.bank_id, first.position, first.writable, first.added_by, first.added_at) == (
        "A.bank:1", 0, True, "cli", 100,
    )
    assert first.writable is True
    db.add_memory_bank(conn, project_id, "B", writable=False)
    db.add_memory_bank(conn, project_id, "C")
    db.add_memory_bank(conn, other, "B")
    monkeypatch.setattr(db, "_now", lambda: 200)
    updated, created = db.add_memory_bank(conn, project_id, "A.bank:1", writable=False, added_by="second")
    assert not created
    assert updated == db.ProjectMemoryBank(project_id, "A.bank:1", 0, False, "cli", 100)
    assert updated.writable is False
    assert [(b.bank_id, b.position) for b in db.list_memory_banks(conn, project_id)] == [
        ("A.bank:1", 0), ("B", 1), ("C", 2),
    ]
    assert db.remove_memory_bank(conn, project_id, " B ")
    assert not db.remove_memory_bank(conn, project_id, "B")
    assert [(b.bank_id, b.position) for b in db.list_memory_banks(conn, project_id)] == [("A.bank:1", 0), ("C", 1)]
    appended, created = db.add_memory_bank(conn, project_id, "B")
    assert created and appended.position == 2
    assert [(b.bank_id, b.position) for b in db.list_memory_banks(conn, other)] == [("B", 0)]


@pytest.mark.parametrize("bank_id", ["../x", "has spaces", "", "x" * 129, "x\ny", "_x", "/x"])
def test_invalid_bank_ids_are_rejected(store, bank_id):
    conn, project_id = store
    for operation in (
        lambda: db.normalize_bank_id(bank_id),
        lambda: db.add_memory_bank(conn, project_id, bank_id),
        lambda: db.remove_memory_bank(conn, project_id, bank_id),
    ):
        with pytest.raises(ValueError, match="invalid bank id"):
            operation()
    assert db.list_memory_banks(conn, project_id) == []


def test_bank_id_max_length_and_punctuation():
    bank_id = "A" + "a._:-" * 25 + "z9"
    assert len(bank_id) == 128
    assert db.normalize_bank_id(" " + bank_id + "\n") == bank_id


def test_unknown_project_rejected(store):
    conn, project_id = store
    for unknown in ("missing", "alpha"):
        with pytest.raises(ValueError, match="no such project"):
            db.add_memory_bank(conn, unknown, "bank")
        with pytest.raises(ValueError, match="no such project"):
            db.set_memory_settings(conn, unknown, retain_shared=False)
    assert db.list_memory_banks(conn, project_id) == []
    assert conn.execute("SELECT count(*) FROM project_memory_settings").fetchone()[0] == 0


def test_settings_defaults_partial_updates_and_audit(store, monkeypatch):
    conn, project_id = store
    assert db.get_memory_settings(conn, project_id) == db.ProjectMemorySettings(project_id, True, False)
    assert conn.execute("SELECT count(*) FROM project_memory_settings").fetchone()[0] == 0
    monkeypatch.setattr(db, "_now", lambda: 100)
    assert db.set_memory_settings(conn, project_id, retain_shared=False, updated_by="cli") == (
        db.ProjectMemorySettings(project_id, False, False)
    )
    monkeypatch.setattr(db, "_now", lambda: 200)
    expected = db.ProjectMemorySettings(project_id, False, True)
    assert db.set_memory_settings(conn, project_id, retain_project=True, updated_by="admin") == expected
    assert db.set_memory_settings(conn, project_id) == expected
    settings = db.set_memory_settings(conn, project_id, retain_shared=True, updated_by="admin")
    assert settings.retain_shared is True and settings.retain_project is True
    row = conn.execute("SELECT updated_by, updated_at FROM project_memory_settings").fetchone()
    assert tuple(row) == ("admin", 200)


def test_project_delete_cascades_memory_rows_only(store):
    conn, project_id = store
    other = db.create_project(conn, name="Beta")
    for pid in (project_id, other):
        db.add_memory_bank(conn, pid, "bank")
        db.set_memory_settings(conn, pid, retain_project=True)
    assert db.delete_project(conn, project_id)
    assert db.list_memory_banks(conn, project_id) == []
    assert conn.execute("SELECT project_id FROM project_memory_settings").fetchone()[0] == other
    assert len(db.list_memory_banks(conn, other)) == 1


def test_legacy_db_upgrades_on_open_and_persists_memory(tmp_path):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as legacy:
        legacy.executescript(
            "CREATE TABLE projects (id TEXT PRIMARY KEY, slug TEXT NOT NULL UNIQUE, "
            "name TEXT NOT NULL, description TEXT, created_at INTEGER NOT NULL, archived INTEGER DEFAULT 0);"
            "INSERT INTO projects VALUES ('p_old', 'old', 'Old', 'kept', 12, 0);"
        )
    with db.connect_closing(db_path=path) as conn:
        assert db.get_project(conn, "p_old").description == "kept"
        assert db.list_memory_banks(conn, "p_old") == []
        bank, _ = db.add_memory_bank(conn, "p_old", "bank")
        settings = db.set_memory_settings(conn, "p_old", retain_project=True)
    with db.connect_closing(db_path=path) as conn:
        assert db.list_memory_banks(conn, "p_old") == [bank]
        assert db.get_memory_settings(conn, "p_old") == settings


def test_memory_writes_join_caller_transaction_and_rollback(tmp_path):
    path = tmp_path / "projects.db"
    with db.connect_closing(db_path=path) as conn, db.connect_closing(db_path=path) as observer:
        project_id = db.create_project(conn, name="Alpha")
        db.add_memory_bank(conn, project_id, "original")
        with db.write_txn(conn):
            db.add_memory_bank(conn, project_id, "new")
            db.add_memory_bank(conn, project_id, "original", writable=False)
            db.set_memory_settings(conn, project_id, retain_shared=False)
            assert db.remove_memory_bank(conn, project_id, "original")
            assert conn.in_transaction
            assert [b.bank_id for b in db.list_memory_banks(observer, project_id)] == ["original"]
            assert db.get_memory_settings(observer, project_id).retain_shared is True
        assert not conn.in_transaction
        expected = db.list_memory_banks(observer, project_id)
        assert [(b.bank_id, b.position) for b in expected] == [("new", 0)]
        assert db.get_memory_settings(observer, project_id).retain_shared is False
        with pytest.raises(RuntimeError, match="abort memory"):
            with db.write_txn(conn):
                db.add_memory_bank(conn, project_id, "temporary")
                db.remove_memory_bank(conn, project_id, "new")
                db.set_memory_settings(conn, project_id, retain_project=True)
                raise RuntimeError("abort memory")
        assert not conn.in_transaction
        assert db.list_memory_banks(observer, project_id) == expected
        assert db.get_memory_settings(observer, project_id) == db.ProjectMemorySettings(project_id, False, False)
