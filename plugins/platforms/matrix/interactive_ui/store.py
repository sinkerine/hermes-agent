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


@dataclass(frozen=True)
class UiOwnership:
    ui_event_id: str
    room_id: str
    ui_id: str
    session_key: str
    thread_id: str | None
    bot_mxid: str


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
                CREATE TABLE IF NOT EXISTS ownership (
                    ui_event_id TEXT PRIMARY KEY, room_id TEXT NOT NULL, ui_id TEXT NOT NULL,
                    session_key TEXT NOT NULL, thread_id TEXT, bot_mxid TEXT NOT NULL
                );
            """)
            self._migrate_replay_history()
        except (sqlite3.Error, ValueError):
            self._db.close()
            raise

    def _migrate_replay_history(self) -> None:
        """Retain replay tombstones (v1) and authoritative state watermarks (v2).

        Legacy pruning could erase nonces even for surviving UI rows, so no
        legacy receipt can attest complete replay history. Never upgrade it.
        DDL, history copy and the version stamp commit or roll back together.
        """
        with self._db:
            self._db.execute("BEGIN IMMEDIATE")
            version = self._db.execute("PRAGMA user_version").fetchone()[0]
            if version > 2:
                raise ValueError("Unsupported interactive UI store schema")
            if version == 0:
                self._db.execute("ALTER TABLE ownership ADD COLUMN replay_safe INTEGER NOT NULL DEFAULT 0")
                self._db.execute("""INSERT OR IGNORE INTO ownership
                    (ui_event_id, room_id, ui_id, session_key, thread_id, bot_mxid)
                    SELECT ui_event_id, room_id, ui_id, session_key, thread_id, bot_mxid FROM uis""")
                legacy = self._db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'nonces'"
                ).fetchone()
                if legacy:
                    self._db.execute("ALTER TABLE nonces RENAME TO legacy_nonces")
                self._db.execute("""CREATE TABLE nonces (
                    ui_event_id TEXT NOT NULL REFERENCES ownership(ui_event_id),
                    nonce TEXT NOT NULL, action_event_id TEXT NOT NULL UNIQUE, sender TEXT NOT NULL,
                    accepted_at REAL NOT NULL, PRIMARY KEY (ui_event_id, nonce))""")
                if legacy:
                    self._db.execute("INSERT INTO nonces SELECT * FROM legacy_nonces")
                    self._db.execute("DROP TABLE legacy_nonces")
            if version < 2:
                self._migrate_watermarks()
                self._db.execute("PRAGMA user_version=2")
            self._db.execute("""INSERT OR IGNORE INTO ownership
                (ui_event_id, room_id, ui_id, session_key, thread_id, bot_mxid)
                SELECT ui_event_id, room_id, ui_id, session_key, thread_id, bot_mxid FROM uis""")

    def _migrate_watermarks(self) -> None:
        """Only matching local rows can attest state for version 1 receipts."""
        self._db.execute("ALTER TABLE ownership ADD COLUMN latest_revision INTEGER")
        self._db.execute("ALTER TABLE ownership ADD COLUMN latest_status TEXT")
        rows = self._db.execute("""SELECT u.* FROM uis u JOIN ownership o
            ON u.ui_event_id = o.ui_event_id AND u.room_id = o.room_id AND u.ui_id = o.ui_id
            AND u.session_key = o.session_key AND u.thread_id IS o.thread_id
            AND u.bot_mxid = o.bot_mxid WHERE o.replay_safe = 1""").fetchall()
        for row in rows:
            _payload_json(json.loads(row["payload_json"]), ui_id=row["ui_id"],
                          revision=row["revision"], status=row["status"])
            self._db.execute("UPDATE ownership SET latest_revision = ?, latest_status = ? WHERE ui_event_id = ?",
                             (row["revision"], row["status"], row["ui_event_id"]))
        self._db.execute("UPDATE ownership SET replay_safe = 0 WHERE latest_revision IS NULL OR latest_status IS NULL")

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

    def get_ownership(self, ui_event_id: str) -> UiOwnership | None:
        with self._lock:
            row = self._db.execute(
                "SELECT ui_event_id, room_id, ui_id, session_key, thread_id, bot_mxid FROM ownership WHERE ui_event_id = ?",
                (ui_event_id,),
            ).fetchone()
        return UiOwnership(**dict(row)) if row else None

    def has_replay_history(self, ui_event_id: str) -> bool:
        with self._lock:
            row = self._db.execute("SELECT replay_safe FROM ownership WHERE ui_event_id = ?", (ui_event_id,)).fetchone()
        return row is not None and bool(row["replay_safe"]) and self.get_watermark(ui_event_id) is not None

    def get_watermark(self, ui_event_id: str) -> tuple[int, str] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT latest_revision, latest_status FROM ownership WHERE ui_event_id = ?", (ui_event_id,),
            ).fetchone()
        if row is None or row["latest_revision"] is None or row["latest_status"] is None:
            return None
        return row["latest_revision"], row["latest_status"]

    def can_restore(self, ui_event_id: str, revision: int, status: str) -> bool:
        watermark = self.get_watermark(ui_event_id)
        if watermark is None:
            return False
        latest_revision, latest_status = watermark
        return (revision >= latest_revision and (revision != latest_revision or status == latest_status)
                and (latest_status == "active" or status != "active"))

    def is_duplicate_action(self, ui_event_id: str, nonce: str, action_event_id: str) -> bool:
        with self._lock:
            return self._db.execute(
                "SELECT 1 FROM nonces WHERE (ui_event_id = ? AND nonce = ?) OR action_event_id = ?",
                (ui_event_id, nonce, action_event_id),
            ).fetchone() is not None

    def insert(self, record: UiRecord) -> None:
        payload_json = _payload_json(record.payload, ui_id=record.ui_id, revision=record.revision, status=record.status)
        now = time()
        with self._lock, self._db:
            self._db.execute("BEGIN IMMEDIATE")
            ownership = UiOwnership(record.ui_event_id, record.room_id, record.ui_id,
                                    record.session_key, record.thread_id, record.bot_mxid)
            receipt = self.get_ownership(record.ui_event_id)
            if receipt is not None and receipt != ownership:
                raise ValueError("UI ownership receipt is immutable")
            if record.recovered and receipt is None:
                raise ValueError("Recovery requires a durable ownership receipt")
            prior_identity = self._db.execute(
                "SELECT 1 FROM ownership WHERE ui_id = ? AND ui_event_id != ?",
                (record.ui_id, record.ui_event_id),
            ).fetchone()
            if prior_identity:
                raise sqlite3.IntegrityError("UI identity already belongs to another original event")
            if receipt is not None and not self.has_replay_history(record.ui_event_id):
                raise ValueError("UI replay history is uncertain; create a new card")
            if receipt is not None and not self.can_restore(record.ui_event_id, record.revision, record.status):
                raise ValueError("UI state cannot downgrade the durable watermark")
            self._db.execute("""INSERT OR IGNORE INTO ownership
                (ui_event_id, room_id, ui_id, session_key, thread_id, bot_mxid, replay_safe,
                 latest_revision, latest_status) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)""",
                             (*vars(ownership).values(), record.revision, record.status))
            self._db.execute(
                "INSERT INTO uis VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (record.ui_event_id, record.room_id, record.ui_id, record.session_key, record.thread_id,
                 record.bot_mxid, record.revision, record.status, payload_json, int(record.recovered), now, now),
            )
            self._write_watermark(record.ui_event_id, record.revision, record.status)

    def _write_watermark(self, ui_event_id: str, revision: int, status: str) -> None:
        updated = self._db.execute("""UPDATE ownership SET latest_revision = ?, latest_status = ?
            WHERE ui_event_id = ? AND (latest_revision IS NULL OR latest_revision < ?
                OR (latest_revision = ? AND latest_status = ?))""",
            (revision, status, ui_event_id, revision, revision, status)).rowcount
        if updated != 1:
            raise ValueError("UI state cannot downgrade the durable watermark")

    def _write_payload(self, ui_event_id: str, payload: dict[str, Any], payload_json: str) -> None:
        self._db.execute(
            "UPDATE uis SET revision = ?, status = ?, payload_json = ?, updated_at = ? WHERE ui_event_id = ?",
            (payload["revision"], payload["status"], payload_json, time(), ui_event_id),
        )
        self._write_watermark(ui_event_id, payload["revision"], payload["status"])

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
            if not self.has_replay_history(ui_event_id):
                return "inactive"
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
            else:
                self._db.execute("UPDATE uis SET updated_at = ? WHERE ui_event_id = ?", (time(), ui_event_id))
            return "accepted"

    def prune(self, *, older_than_days: int = 90) -> int:
        """Delete old mutable records while retaining receipts and tombstones.

        Age is measured from the last UI revision or nonce acceptance. Returns
        the number of UI records removed, not the number of nonce rows.
        Ownership receipts persist for the lifetime of this database: pruning
        must never allow a room replay to assign a card to another session.
        """
        if older_than_days < 0:
            raise ValueError("Retention days must be nonnegative")
        cutoff = time() - older_than_days * 86400
        with self._lock, self._db:
            self._db.execute("BEGIN IMMEDIATE")
            removed = self._db.execute("DELETE FROM uis WHERE updated_at < ?", (cutoff,)).rowcount
            return removed
