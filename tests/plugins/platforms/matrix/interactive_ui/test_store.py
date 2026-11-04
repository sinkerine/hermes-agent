"""Durable action acceptance with real SQLite connections, races, and rollback."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import sqlite3
from threading import Barrier

import pytest

from plugins.platforms.matrix.interactive_ui.store import InteractiveUiStore, UiRecord


def record(event="$ui", ui_id="card", **kwargs):
    payload = {"version": 1, "ui_id": ui_id, "revision": 0, "status": "active", "state": {},
               "root": "root", "nodes": [{"id": "root", "type": "divider"}]}
    return UiRecord(event, "!room:example", ui_id, "profile:matrix:room:thread:user", "$thread", 0,
                    "active", payload, "@bot:example", **kwargs)


@pytest.fixture
def store(tmp_path):
    instance = InteractiveUiStore(tmp_path / "profile" / "interactive_ui.sqlite3")
    instance.insert(record())
    yield instance
    instance.close()


def accept(store, *, event="$ui", nonce="n" * 22, action="$action", revision=0, payload=None):
    return store.accept_action(event, nonce=nonce, action_event_id=action, sender="@alice:example",
                               expected_revision=revision, single_use_payload=payload)


def revision_payload(store, *, status="submitted"):
    current = store.get_by_event("$ui")
    return {**current.payload, "revision": current.revision + 1, "status": status}


def test_round_trip_and_snapshot_ownership(store):
    expected = record()
    assert store.get_by_event("$ui") == expected
    assert store.get_by_ui_id("card") == expected
    assert store.get_by_event("$missing") is None
    assert store.get_by_ui_id("missing") is None
    snapshot = store.get_by_event("$ui")
    snapshot.payload["nodes"][0]["type"] = "unknown"
    snapshot.session_key = "other"
    assert store.get_by_event("$ui") == expected
    recovered = record("$recovered", "recovered", recovered=True)
    original = deepcopy(recovered)
    store.insert(recovered)
    recovered.payload["state"]["later"] = "mutation"
    assert store.get_by_event("$recovered") == original


def test_unique_ui_ids_and_events_do_not_overwrite(store):
    for duplicate in (record(), record("$another"), record(ui_id="another")):
        with pytest.raises(sqlite3.IntegrityError):
            store.insert(duplicate)
    assert store.get_by_event("$ui") == record()
    assert accept(store) == "accepted"


def test_repeatable_stale_and_duplicate_event_dedupe(store):
    assert accept(store, revision=1) == "stale"
    assert accept(store) == "accepted"  # Rejection did not reserve either identifier.
    assert accept(store, action="$retry", revision=99) == "duplicate"
    assert accept(store, nonce="different", revision=99) == "duplicate"
    assert accept(store, nonce="different", action="$new") == "accepted"
    assert store.get_by_event("$ui") == record()
    store.insert(record("$second", "second"))
    assert accept(store, event="$second", nonce="fresh", revision=99) == "duplicate"


@pytest.mark.parametrize("status", ["submitted", "expired", "superseded"])
def test_inactive_does_not_reserve_nonce(store, status):
    payload = revision_payload(store, status=status)
    assert store.commit_revision("$ui", expected_revision=0, payload=payload)
    assert accept(store, revision=1) == "inactive"
    assert accept(store, revision=0) == "stale"
    active = revision_payload(store, status="active")
    assert store.commit_revision("$ui", expected_revision=1, payload=active)
    assert accept(store, revision=2) == "accepted"


def test_single_use_atomic_transition_and_ownership(store):
    before = store.get_by_event("$ui")
    payload = {**revision_payload(store), "status_text": "Submitted"}
    original_payload = deepcopy(payload)
    assert accept(store, payload=payload) == "accepted"
    expected = replace(before, payload=original_payload, revision=1, status="submitted")
    payload["status"] = "active"
    assert store.get_by_event("$ui") == expected
    assert accept(store, revision=0) == "duplicate"
    assert accept(store, nonce="fresh", revision=1) == "duplicate"  # Same action event.
    assert accept(store, nonce="fresh", action="$fresh", revision=1) == "inactive"
    assert accept(store, nonce="fresh", action="$fresh", revision=0) == "stale"


@pytest.mark.parametrize("shared", [True, False], ids=["shared-connection", "independent-connections"])
def test_twenty_thread_nonce_race_exactly_one_accept(tmp_path, shared):
    path = tmp_path / "interactive_ui.sqlite3"
    stores = [InteractiveUiStore(path) for _ in range(1 if shared else 20)]
    stores[0].insert(record())
    ready = Barrier(20)

    def deliver(index):
        instance = stores[0] if shared else stores[index]
        ready.wait(timeout=10)
        return accept(instance, action=f"$delivery{index}")

    try:
        with ThreadPoolExecutor(max_workers=20) as pool:
            results = list(pool.map(deliver, range(20)))
        assert results.count("accepted") == 1
        assert results.count("duplicate") == 19
    finally:
        for instance in stores:
            instance.close()


def test_two_single_use_actions_first_wins_across_connections(tmp_path):
    path = tmp_path / "interactive_ui.sqlite3"
    first, second = InteractiveUiStore(path), InteractiveUiStore(path)
    first.insert(record())
    payload = revision_payload(first)
    ready = Barrier(2)

    def deliver(args):
        instance, identifier = args
        ready.wait(timeout=10)
        return accept(instance, nonce=identifier, action=identifier, payload=payload)

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(deliver, [(first, "$one"), (second, "$two")]))
        assert sorted(results) == ["accepted", "stale"]
        assert first.get_by_event("$ui").revision == 1
    finally:
        first.close()
        second.close()


def test_restart_retains_nonces_submitted_revision_and_wal(tmp_path):
    path = tmp_path / "interactive_ui.sqlite3"
    first = InteractiveUiStore(path)
    first.insert(record(recovered=True))
    payload = revision_payload(first)
    assert accept(first, payload=payload) == "accepted"
    first.close()
    first.close()
    fresh = InteractiveUiStore(path)
    try:
        assert fresh.get_by_ui_id("card") == replace(record(recovered=True), revision=1, status="submitted", payload=payload)
        assert accept(fresh, revision=0) == "duplicate"
        assert accept(fresh, nonce="new", action="$new", revision=0) == "stale"
        assert accept(fresh, nonce="new", action="$new", revision=1) == "inactive"
        with sqlite3.connect(path) as observer:
            assert observer.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        fresh.close()


def test_revision_compare_and_swap_preserves_ownership(store):
    before = store.get_by_event("$ui")
    payload = revision_payload(store, status="superseded")
    assert not store.commit_revision("$ui", expected_revision=9, payload=payload)
    assert store.commit_revision("$ui", expected_revision=0, payload=payload)
    assert not store.commit_revision("$ui", expected_revision=0, payload=payload)
    assert not store.commit_revision("$missing", expected_revision=0, payload=payload)
    assert store.get_by_event("$ui") == replace(before, revision=1, status="superseded", payload=payload)


@pytest.mark.parametrize("field,value", [("ui_id", "other"), ("revision", 0), ("revision", True),
                                         ("status", "invalid"), ("state", {"bad": float("nan")})])
@pytest.mark.parametrize("operation", ["revision", "accept"])
def test_invalid_replacement_rolls_back_without_mutation(store, field, value, operation):
    before = store.get_by_event("$ui")
    payload = {**revision_payload(store), field: value}
    with pytest.raises(ValueError):
        if operation == "revision":
            store.commit_revision("$ui", expected_revision=0, payload=payload)
        else:
            accept(store, payload=payload)
    assert store.get_by_event("$ui") == before
    assert accept(store) == "accepted"


def test_single_use_requires_submitted_payload(store):
    with pytest.raises(ValueError):
        accept(store, payload=revision_payload(store, status="active"))
    assert accept(store, payload=revision_payload(store)) == "accepted"


def test_sql_failure_rolls_back_nonce_and_revision(store, tmp_path):
    # Force a genuine write failure after nonce insertion in the transaction.
    path = tmp_path / "profile" / "interactive_ui.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TRIGGER fail_update BEFORE UPDATE ON uis BEGIN SELECT RAISE(ABORT, 'write failed'); END")
    with pytest.raises(sqlite3.IntegrityError, match="write failed"):
        accept(store, payload=revision_payload(store))
    assert store.get_by_event("$ui") == record()
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM nonces").fetchone()[0] == 0
        connection.execute("DROP TRIGGER fail_update")
    assert accept(store, payload=revision_payload(store)) == "accepted"


def test_missing_record_raises_keyerror_after_event_dedupe(store):
    assert accept(store) == "accepted"
    assert accept(store, event="$missing") == "duplicate"
    with pytest.raises(KeyError, match="missing"):
        accept(store, event="$missing", action="$unknown")
    store.insert(record("$missing", "missing"))
    assert accept(store, event="$missing", action="$unknown") == "accepted"


def test_prune_old_records_and_nonces_uses_last_revision_age(tmp_path, monkeypatch):
    now = 2_000_000_000
    monkeypatch.setattr("plugins.platforms.matrix.interactive_ui.store.time", lambda: now)
    path = tmp_path / "interactive_ui.sqlite3"
    instance = InteractiveUiStore(path)
    try:
        instance.insert(record())
        instance.insert(record("$revised", "revised"))
        assert accept(instance) == "accepted"
        assert accept(instance, event="$revised", nonce="old", action="$old") == "accepted"
        now += 89 * 86400
        payload = {**instance.get_by_event("$revised").payload, "revision": 1}
        assert instance.commit_revision("$revised", expected_revision=0, payload=payload)
        now += 2 * 86400
        instance.insert(record("$fresh", "fresh"))
        assert instance.prune() == 1
        assert instance.get_by_event("$ui") is None
        assert instance.get_by_event("$revised") is not None
        assert instance.get_by_event("$fresh") is not None
        with sqlite3.connect(path) as observer:
            assert observer.execute("SELECT COUNT(*) FROM nonces").fetchone()[0] == 0
        assert instance.prune() == 0
        with pytest.raises(ValueError):
            instance.prune(older_than_days=-1)
    finally:
        instance.close()


def test_explicit_path_is_pinned_and_profiles_are_isolated(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    first = InteractiveUiStore(Path("first/interactive_ui.sqlite3"))
    second = InteractiveUiStore(tmp_path / "second" / "interactive_ui.sqlite3")
    first.insert(record())
    monkeypatch.chdir(tmp_path / "second")
    try:
        assert first.get_by_event("$ui") == record()
        assert second.get_by_event("$ui") is None
        second.insert(record())
        assert accept(first) == "accepted"
        assert accept(second) == "accepted"
    finally:
        first.close()
        second.close()
