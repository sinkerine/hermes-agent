"""Concurrent first project lookups must not race journal setup or additive migrations."""
from concurrent.futures import ThreadPoolExecutor
import sqlite3
import threading

import pytest

from hermes_cli import projects_db
import hermes_state_wal


@pytest.mark.requires_wal
@pytest.mark.parametrize("legacy", [False, True], ids=["empty", "legacy"])
def test_concurrent_project_openers_share_initialization(tmp_path, monkeypatch, legacy):
    path = tmp_path / "projects.db"
    if legacy:
        with sqlite3.connect(path) as conn:
            conn.executescript(
                "CREATE TABLE projects (id TEXT PRIMARY KEY, slug TEXT NOT NULL UNIQUE, "
                "name TEXT NOT NULL, description TEXT, created_at INTEGER NOT NULL, archived INTEGER DEFAULT 0);"
                "INSERT INTO projects VALUES ('p_old', 'old', 'Old', 'kept', 12, 0);"
            )
    first_reading = threading.Event()
    first_released = threading.Event()
    second_finished = threading.Event()
    probe_lock = threading.Lock()
    first = True
    real_probe = hermes_state_wal._on_disk_journal_mode
    monkeypatch.setattr(hermes_state_wal, "resolve_journal_mode", lambda: "wal")

    def pause_first_reader(conn):
        nonlocal first
        mode = real_probe(conn)
        with probe_lock:
            pause = first
            first = False
        if pause:
            # Hold the shared read lock that a concurrent journal upgrade must promote past.
            # The second opener either finishes (the regression) or waits for setup ownership.
            conn.execute("BEGIN")
            conn.execute("SELECT count(*) FROM sqlite_schema").fetchone()
            first_reading.set()
            try:
                second_finished.wait(10)
            finally:
                conn.rollback()
                first_released.set()
        return mode

    monkeypatch.setattr(hermes_state_wal, "_on_disk_journal_mode", pause_first_reader)

    def lookup(*, second=False):
        try:
            with projects_db.connect_closing(db_path=path) as conn:
                assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
                assert not conn.in_transaction
                binding = projects_db.get_chat_binding(
                    conn, platform="telegram", transport_profile="default", chat_id="topic",
                )
                assert binding is None
                if legacy:
                    assert projects_db.get_project(conn, "p_old").description == "kept"
        finally:
            if second:
                second_finished.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first_open = pool.submit(lookup)
        assert first_reading.wait(10)
        second_open = pool.submit(lookup, second=True)
        # A different profile's store can initialize while this path is occupied.
        with projects_db.connect_closing(db_path=tmp_path / "other" / "projects.db") as other:
            assert not first_released.is_set()
            assert projects_db.list_projects(other) == []
        first_open.result(timeout=20)
        second_open.result(timeout=20)
    with projects_db.connect_closing(db_path=path) as conn:
        project_id = projects_db.create_project(conn, name="New")
        assert projects_db.get_project(conn, project_id).name == "New"
