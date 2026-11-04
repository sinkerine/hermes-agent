"""Profile-pinned SQLite records and durable interactive UI action deduplication."""

from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3
from threading import RLock
from time import time
from typing import Any, Literal

from .protocol import canonical_json_bytes

AcceptOutcome = Literal["accepted", "duplicate", "stale", "inactive"]


@dataclass
class UiRecord:
    ui_event_id: str
    room_id: str
    ui_id: str
    session_key: str
    thread_id: str | None
    revision: int
    status: str
    payload: dict[str, Any]
    bot_mxid: str
    recovered: bool = False


def _payload_json(payload: dict[str, Any], *, ui_id: str, revision: int, status: str | None = None) -> str:
    """Check metadata consistency; protocol validation belongs to the caller."""
    next_revision = payload.get("revision")
    if payload.get("ui_id") != ui_id:
        raise ValueError("Replacement payload must preserve ui_id")
    if type(next_revision) not in (int, float) or next_revision != revision or not 0 <= revision <= 9007199254740991:
        raise ValueError("Payload revision must match the expected revision")
    if payload.get("status") not in ("active", "submitted", "expired", "superseded"):
        raise ValueError("Payload status must be a valid wire status")
    if status is not None and payload["status"] != status:
        raise ValueError("Payload status must match the record transition")
    return canonical_json_bytes(payload).decode("utf-8")


class InteractiveUiStore:
    """One adapter's store, with a caller-supplied path fixed at construction.

    The lock protects a shared connection across threads. BEGIN IMMEDIATE also
    serializes writers using other connections or processes on the same file.
    """

    def __init__(self, path: Path) -> None:
        self._path = path.resolve()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self._db = sqlite3.connect(self._path, timeout=30, isolation_level=None, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        try:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA foreign_keys=ON")
            self._db.executescript("""
                CREATE TABLE IF NOT EXISTS uis (
                    ui_event_id TEXT PRIMARY KEY, room_id TEXT NOT NULL, ui_id TEXT NOT NULL UNIQUE,
                    session_key TEXT NOT NULL, thread_id TEXT, bot_mxid TEXT NOT NULL,
                    revision INTEGER NOT NULL, status TEXT NOT NULL, payload_json TEXT NOT NULL,
                    recovered INTEGER NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS nonces (
                    ui_event_id TEXT NOT NULL REFERENCES uis(ui_event_id) ON DELETE CASCADE,
                    nonce TEXT NOT NULL, action_event_id TEXT NOT NULL UNIQUE, sender TEXT NOT NULL,
                    accepted_at REAL NOT NULL, PRIMARY KEY (ui_event_id, nonce)
                );
            """)
        except sqlite3.Error:
            self._db.close()
            raise

    def close(self) -> None:
        """Release the connection when the owning adapter disconnects (idempotent)."""
        with self._lock:
            self._db.close()

    def _get(self, column: str, value: str) -> UiRecord | None:
        with self._lock:
            row = self._db.execute(f"SELECT * FROM uis WHERE {column} = ?", (value,)).fetchone()
        if row is None:
            return None
        return UiRecord(
            ui_event_id=row["ui_event_id"], room_id=row["room_id"], ui_id=row["ui_id"],
            session_key=row["session_key"], thread_id=row["thread_id"], revision=row["revision"],
            status=row["status"], payload=json.loads(row["payload_json"]), bot_mxid=row["bot_mxid"],
            recovered=bool(row["recovered"]),
        )

    def get_by_event(self, ui_event_id: str) -> UiRecord | None:
        return self._get("ui_event_id", ui_event_id)

    def get_by_ui_id(self, ui_id: str) -> UiRecord | None:
        return self._get("ui_id", ui_id)

    def insert(self, record: UiRecord) -> None:
        payload_json = _payload_json(record.payload, ui_id=record.ui_id, revision=record.revision, status=record.status)
        now = time()
        with self._lock, self._db:
            self._db.execute("BEGIN IMMEDIATE")
            self._db.execute(
                "INSERT INTO uis VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (record.ui_event_id, record.room_id, record.ui_id, record.session_key, record.thread_id,
                 record.bot_mxid, record.revision, record.status, payload_json, int(record.recovered), now, now),
            )

    def _write_payload(self, ui_event_id: str, payload: dict[str, Any], payload_json: str) -> None:
        self._db.execute(
            "UPDATE uis SET revision = ?, status = ?, payload_json = ?, updated_at = ? WHERE ui_event_id = ?",
            (payload["revision"], payload["status"], payload_json, time(), ui_event_id),
        )

    def commit_revision(self, ui_event_id: str, *, expected_revision: int, payload: dict[str, Any]) -> bool:
        """CAS the revision; malformed replacement metadata raises ValueError."""
        with self._lock, self._db:
            self._db.execute("BEGIN IMMEDIATE")
            row = self._db.execute("SELECT ui_id, revision FROM uis WHERE ui_event_id = ?", (ui_event_id,)).fetchone()
            if row is None or row["revision"] != expected_revision:
                return False
            payload_json = _payload_json(payload, ui_id=row["ui_id"], revision=expected_revision + 1)
            self._write_payload(ui_event_id, payload, payload_json)
            return True

    def accept_action(
        self, ui_event_id: str, *, nonce: str, action_event_id: str, sender: str,
        expected_revision: int, single_use_payload: dict[str, Any] | None,
    ) -> AcceptOutcome:
        """Commit dedupe and optional submitted revision before visible effects.

        Duplicates precede all record checks, including revision/status. A missing
        record raises KeyError(ui_event_id); inbound must recover or reject it.
        Failed transactions retain neither the nonce nor a partial revision.
        """
        with self._lock, self._db:
            self._db.execute("BEGIN IMMEDIATE")
            duplicate = self._db.execute(
                "SELECT 1 FROM nonces WHERE (ui_event_id = ? AND nonce = ?) OR action_event_id = ?",
                (ui_event_id, nonce, action_event_id),
            ).fetchone()
            if duplicate:
                return "duplicate"
            row = self._db.execute("SELECT ui_id, revision, status FROM uis WHERE ui_event_id = ?", (ui_event_id,)).fetchone()
            if row is None:
                raise KeyError(ui_event_id)
            if row["revision"] != expected_revision:
                return "stale"
            if row["status"] != "active":
                return "inactive"
            payload_json = None
            if single_use_payload is not None:
                payload_json = _payload_json(single_use_payload, ui_id=row["ui_id"], revision=expected_revision + 1, status="submitted")
            self._db.execute(
                "INSERT INTO nonces VALUES (?, ?, ?, ?, ?)",
                (ui_event_id, nonce, action_event_id, sender, time()),
            )
            if single_use_payload is not None:
                self._write_payload(ui_event_id, single_use_payload, payload_json)
            return "accepted"

    def prune(self, *, older_than_days: int = 90) -> int:
        """Delete old records (and their nonces), plus expired dedupe entries.

        Age is measured from the last UI revision or nonce acceptance. Returns
        the number of UI records removed, not the number of nonce rows.
        """
        if older_than_days < 0:
            raise ValueError("Retention days must be nonnegative")
        cutoff = time() - older_than_days * 86400
        with self._lock, self._db:
            self._db.execute("BEGIN IMMEDIATE")
            removed = self._db.execute("DELETE FROM uis WHERE updated_at < ?", (cutoff,)).rowcount
            self._db.execute("DELETE FROM nonces WHERE accepted_at < ?", (cutoff,))
            return removed
