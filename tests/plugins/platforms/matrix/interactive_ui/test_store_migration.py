"""Legacy replay uncertainty fails closed; migration preserves every surviving tombstone."""

import json
import sqlite3
from dataclasses import replace

import pytest

from plugins.platforms.matrix.interactive_ui.store import InteractiveUiStore
from .test_store import accept, record
from .test_inbound import Harness


def legacy_database(path, *, pruned=False, ownership=True, orphan=False):
    card = record()
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE uis (
                ui_event_id TEXT PRIMARY KEY, room_id TEXT NOT NULL, ui_id TEXT NOT NULL UNIQUE,
                session_key TEXT NOT NULL, thread_id TEXT, bot_mxid TEXT NOT NULL,
                revision INTEGER NOT NULL, status TEXT NOT NULL, payload_json TEXT NOT NULL,
                recovered INTEGER NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL);
            CREATE TABLE nonces (
                ui_event_id TEXT NOT NULL REFERENCES uis(ui_event_id) ON DELETE CASCADE,
                nonce TEXT NOT NULL, action_event_id TEXT NOT NULL UNIQUE, sender TEXT NOT NULL,
                accepted_at REAL NOT NULL, PRIMARY KEY (ui_event_id, nonce));
        """)
        if not pruned:
            db.execute("INSERT INTO uis VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                       (card.ui_event_id, card.room_id, card.ui_id, card.session_key, card.thread_id,
                        card.bot_mxid, card.revision, card.status, json.dumps(card.payload), 0, 0, 0))
            db.execute("INSERT INTO nonces VALUES (?, ?, ?, ?, ?)",
                       (card.ui_event_id, "n" * 22, "$action", "@alice:example", 0))
        if ownership:
            db.execute("""CREATE TABLE ownership (ui_event_id TEXT PRIMARY KEY, room_id TEXT NOT NULL,
                ui_id TEXT NOT NULL, session_key TEXT NOT NULL, thread_id TEXT, bot_mxid TEXT NOT NULL)""")
            db.execute("INSERT INTO ownership VALUES (?, ?, ?, ?, ?, ?)",
                       (card.ui_event_id, card.room_id, card.ui_id, card.session_key, card.thread_id, card.bot_mxid))
        if orphan:
            db.execute("INSERT INTO nonces VALUES ('$orphan', 'old', '$orphan-action', '@alice:example', 0)")
    return card


@pytest.mark.parametrize("pruned,ownership", [(False, True), (True, True), (False, False)])
def test_legacy_migration_preserves_history_without_upgrading_uncertain_receipts(tmp_path, pruned, ownership):
    path = tmp_path / "ui.sqlite3"
    card = legacy_database(path, pruned=pruned, ownership=ownership)
    retained = 0 if pruned else 1
    for _ in range(2):
        store = InteractiveUiStore(path)
        try:
            assert store._db.execute("PRAGMA user_version").fetchone()[0] == 2
            assert not store.has_replay_history(card.ui_event_id)
            assert store.get_ownership(card.ui_event_id).session_key == card.session_key
            assert store._db.execute("SELECT COUNT(*) FROM nonces").fetchone()[0] == retained
            if not pruned:
                assert accept(store, action="$retry") == "duplicate"
                assert accept(store, nonce="new", action="$new") == "inactive"
                assert store.commit_revision(card.ui_event_id, expected_revision=0,
                                             payload={**card.payload, "revision": 1}) is (_ == 0)
                assert not store.has_replay_history(card.ui_event_id)
                store._db.execute("DELETE FROM uis")
            with pytest.raises(ValueError, match="replay history is uncertain"):
                store.insert(replace(card, recovered=True))
            pruned = True
        finally:
            store.close()
    store = InteractiveUiStore(path)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="UI identity"):
            store.insert(record("$fresh", card.ui_id))
        assert store.get_ownership("$fresh") is None
        fresh = record("$fresh", "fresh")
        store.insert(fresh)
        assert store.has_replay_history(fresh.ui_event_id)
        assert accept(store, event=fresh.ui_event_id, nonce="fresh", action="$fresh-action") == "accepted"
    finally:
        store.close()


def test_migration_copy_failure_rolls_back_ddl_version_and_history(tmp_path):
    path = tmp_path / "ui.sqlite3"
    legacy_database(path, orphan=True)
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        InteractiveUiStore(path)
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 0
        assert "replay_safe" not in {row[1] for row in db.execute("PRAGMA table_info(ownership)")}
        assert db.execute("SELECT COUNT(*) FROM nonces").fetchone()[0] == 2
        assert db.execute("PRAGMA foreign_key_list(nonces)").fetchone()[2] == "uis"
        assert db.execute("SELECT 1 FROM sqlite_master WHERE name = 'legacy_nonces'").fetchone() is None
        db.execute("DELETE FROM nonces WHERE ui_event_id = '$orphan'")
    store = InteractiveUiStore(path)
    try:
        assert store.is_duplicate_action("$ui", "n" * 22, "$retry")
        assert not store.has_replay_history("$ui")
    finally:
        store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("pruned", [False, True])
async def test_legacy_cards_refuse_actions_and_recovery_but_new_card_succeeds(tmp_path, pruned):
    path = tmp_path / "ui.sqlite3"
    legacy_database(path, pruned=pruned)
    harness = Harness(path)
    try:
        harness.card = {"event_id": "$ui", "ui_id": "card"}
        await harness.act(event_id="$legacy", nonce="fresh" * 5)
        harness.notice("unknown")
        harness.adapter._client.get_event.assert_not_awaited()
        assert not harness.store.has_replay_history("$ui")
        harness.reset()
        await harness.present()
        await harness.act(event_id="$new-card")
        assert harness.order == ["edit", "turn"]
    finally:
        harness.store.close()


def version_one_database(path, *, missing=False, safe=True, mismatch=False):
    card = legacy_database(path)
    with sqlite3.connect(path) as db:
        db.execute("ALTER TABLE ownership ADD COLUMN replay_safe INTEGER NOT NULL DEFAULT 0")
        db.execute("UPDATE ownership SET replay_safe = ?", (int(safe),))
        db.executescript("""
            ALTER TABLE nonces RENAME TO old_nonces;
            CREATE TABLE nonces (
                ui_event_id TEXT NOT NULL REFERENCES ownership(ui_event_id),
                nonce TEXT NOT NULL, action_event_id TEXT NOT NULL UNIQUE, sender TEXT NOT NULL,
                accepted_at REAL NOT NULL, PRIMARY KEY (ui_event_id, nonce));
            INSERT INTO nonces SELECT * FROM old_nonces;
            DROP TABLE old_nonces;
            PRAGMA user_version=1;
        """)
        if missing:
            db.execute("DELETE FROM uis")
        if mismatch:
            db.execute("UPDATE uis SET session_key = 'untrusted'")
    return card


@pytest.mark.parametrize("status", ["active", "submitted", "expired", "superseded"])
def test_version_one_trusted_rows_backfill_state_and_keep_dedupe_across_reopen(tmp_path, status):
    path = tmp_path / "ui.sqlite3"
    card = version_one_database(path)
    payload = {**card.payload, "revision": 4, "status": status}
    with sqlite3.connect(path) as db:
        db.execute("UPDATE uis SET revision = 4, status = ?, payload_json = ?", (status, json.dumps(payload)))
    for _ in range(2):
        store = InteractiveUiStore(path)
        try:
            assert store.has_replay_history(card.ui_event_id)
            assert store.get_watermark(card.ui_event_id) == (4, status)
            assert accept(store, action="$retry") == "duplicate"
            assert accept(store, nonce="fresh", action="$fresh", revision=0) == "stale"
            store._db.execute("DELETE FROM uis")
            with pytest.raises(ValueError, match="watermark"):
                store.insert(replace(card, recovered=True))
            store.insert(replace(card, revision=4, status=status, payload=payload, recovered=True))
        finally:
            store.close()


@pytest.mark.parametrize("missing,safe,mismatch", [(True, True, False), (False, False, False),
                                                    (False, True, True)])
def test_version_one_unprovable_state_stays_unsafe_on_reopen(tmp_path, missing, safe, mismatch):
    path = tmp_path / "ui.sqlite3"
    card = version_one_database(path, missing=missing, safe=safe, mismatch=mismatch)
    for _ in range(2):
        store = InteractiveUiStore(path)
        try:
            assert not store.has_replay_history(card.ui_event_id)
            assert store.get_watermark(card.ui_event_id) is None
            assert store.is_duplicate_action(card.ui_event_id, "n" * 22, "$retry")
            store._db.execute("DELETE FROM uis")
            with pytest.raises(ValueError, match="uncertain"):
                store.insert(replace(card, recovered=True))
        finally:
            store.close()


def test_version_one_watermark_migration_fault_rolls_back_and_can_retry(tmp_path):
    path = tmp_path / "ui.sqlite3"
    card = version_one_database(path)
    with sqlite3.connect(path) as db:
        db.execute("""CREATE TRIGGER fail_watermark BEFORE UPDATE ON ownership
            BEGIN SELECT RAISE(ABORT, 'watermark failed'); END""")
    with pytest.raises(sqlite3.IntegrityError, match="watermark failed"):
        InteractiveUiStore(path)
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1
        assert "latest_revision" not in {row[1] for row in db.execute("PRAGMA table_info(ownership)")}
        assert db.execute("SELECT replay_safe FROM ownership").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM nonces").fetchone()[0] == 1
        db.execute("DROP TRIGGER fail_watermark")
    store = InteractiveUiStore(path)
    try:
        assert store.get_watermark(card.ui_event_id) == (0, "active")
        assert store.is_duplicate_action(card.ui_event_id, "n" * 22, "$retry")
    finally:
        store.close()


@pytest.mark.asyncio
async def test_version_one_missing_state_refuses_inbound_recovery_without_room_fetch(tmp_path):
    path = tmp_path / "ui.sqlite3"
    card = version_one_database(path, missing=True)
    harness = Harness(path)
    try:
        harness.card = {"event_id": card.ui_event_id, "ui_id": card.ui_id}
        await harness.act(event_id="$fresh", nonce="c" * 22)
        harness.notice("unknown")
        harness.adapter._client.get_event.assert_not_awaited()
        harness.reset()
        await harness.act(event_id="$retry", nonce="n" * 22)
        assert not harness.order
    finally:
        harness.store.close()
