"""Project-scoped recall and retention, attached alongside shared memory."""
from __future__ import annotations

import contextvars
import copy
import json
import logging
import sqlite3
import threading
from time import monotonic
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from agent.memory_provider import MemoryProvider, RecallStatus, ctx_bound
from agent.project_memory.backend import ProjectMemoryBackend, RecallHit, RetainItem
from agent.project_memory.config import ProjectMemoryServiceConfig, load_service_config
from hermes_cli.projects_db import (
    ProjectMemoryBank, ProjectMemorySettings, connect_closing, get_memory_settings, list_memory_banks,
)

if TYPE_CHECKING:
    from agent.memory_manager import MemoryManager

logger = logging.getLogger(__name__)
PROJECT_MEMORY_RECALL_TOOL = "project_memory_recall"
_TOOL_SCHEMA = {
    "name": PROJECT_MEMORY_RECALL_TOOL,
    "description": "Search this conversation's project memory banks for relevant memories.",
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "What to search for."},
            "bank": {"type": "string", "description": "Optional attached bank to search."},
        },
        "required": ["query"],
    },
}


@dataclass(frozen=True)
class ProjectMemoryTarget:
    project_id: str
    slug: str
    name: str
    projects_db: Path


@dataclass(frozen=True)
class _TurnSnapshot:
    banks: tuple[ProjectMemoryBank, ...]
    settings: ProjectMemorySettings
    turn_index: int = 0


class ProjectMemoryProvider(MemoryProvider):
    def __init__(self, target: ProjectMemoryTarget, service: ProjectMemoryServiceConfig,
                 backend: ProjectMemoryBackend) -> None:
        self.target, self.service, self.backend = target, service, backend
        self._snapshot: Optional[_TurnSnapshot] = None
        # Policy evaluation captures this for queued writes; live reads use the shared snapshot.
        self._sync_snapshot: contextvars.ContextVar[Optional[_TurnSnapshot]] = contextvars.ContextVar(
            "project_memory_sync", default=None,
        )
        self._lock = threading.RLock()
        self._cooldowns: dict[tuple[str, str], float] = {}
        self._session_id = ""
        self._metadata: dict[str, str] = {}
        self._recall_count = 0

    @property
    def name(self) -> str:
        return "project"

    def is_available(self) -> bool:
        return True

    def initialize(self, session_id: str, **kwargs) -> None:
        self._session_id = session_id
        self._metadata = {key: str(kwargs.get(key) or "") for key in ("platform", "chat_id", "thread_id")}

    def system_prompt_block(self) -> str:
        return ("This conversation belongs to a Hermes project with its own memory banks; relevant project "
                "memories are attached to each user message, and `project_memory_recall` searches them.")

    def _refresh(self, turn_index: int) -> _TurnSnapshot:
        with self._lock:
            previous = self._snapshot or _TurnSnapshot((), ProjectMemorySettings(self.target.project_id))
            try:
                with connect_closing(db_path=self.target.projects_db) as conn:
                    snapshot = _TurnSnapshot(tuple(list_memory_banks(conn, self.target.project_id)),
                                             get_memory_settings(conn, self.target.project_id), turn_index)
            except (OSError, sqlite3.Error):
                logger.debug("Project memory snapshot refresh failed", exc_info=True)
                snapshot = _TurnSnapshot(previous.banks, previous.settings, turn_index)
            self._snapshot = snapshot
            return snapshot

    def _current(self) -> _TurnSnapshot:
        with self._lock:
            return self._snapshot or self._refresh(0)

    def on_turn_start(self, turn_number: int, message: str, **kwargs) -> None:
        self._refresh(turn_number)
        self._sync_snapshot.set(None)

    def _ready(self, bank_id: str, operation: str) -> bool:
        with self._lock:
            until = self._cooldowns.get((bank_id, operation))
            return until is None or monotonic() >= until

    def _failed(self, bank_id: str, operation: str) -> None:
        with self._lock:
            key = bank_id, operation
            if key not in self._cooldowns:
                logger.warning("Project memory bank %s %s failed; cooling down for 300s", bank_id, operation)
            self._cooldowns[key] = monotonic() + 300

    def _recovered(self, bank_id: str, operation: str) -> None:
        with self._lock:
            if self._cooldowns.pop((bank_id, operation), None) is not None:
                logger.info("Project memory bank %s %s recovered", bank_id, operation)

    def _recall(self, query: str, banks: tuple[ProjectMemoryBank, ...]) -> tuple[list[RecallHit], list[str]]:
        active = [bank for bank in banks if self._ready(bank.bank_id, "recall")]
        failed = {bank.bank_id for bank in banks if bank not in active}
        if not active:
            return [], [bank.bank_id for bank in banks if bank.bank_id in failed]
        pool = ThreadPoolExecutor(max_workers=min(8, len(active)), thread_name_prefix="project-recall")
        futures = {bank.bank_id: pool.submit(ctx_bound(self.backend.recall), bank.bank_id, query) for bank in active}
        # Leave headroom under the manager's eight-second prefetch cap. Never join overdue requests.
        done, _ = wait(futures.values(), timeout=min(self.service.timeout, 8.0) * 0.8)
        hits: list[RecallHit] = []
        seen: set[str] = set()
        try:
            for bank in active:
                future = futures[bank.bank_id]
                try:
                    if future not in done:
                        raise TimeoutError("Project recall deadline exceeded")
                    results = future.result()
                except Exception:
                    logger.debug("Project memory bank %s recall failed", bank.bank_id, exc_info=True)
                    self._failed(bank.bank_id, "recall")
                    failed.add(bank.bank_id)
                    continue
                self._recovered(bank.bank_id, "recall")
                for hit in results:
                    normalized = " ".join(hit.text.split()).casefold()
                    if normalized and normalized not in seen:
                        seen.add(normalized)
                        hits.append(hit)
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        return hits, [bank.bank_id for bank in banks if bank.bank_id in failed]

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        snapshot = self._current()
        hits, _ = self._recall(query, snapshot.banks)
        self._recall_count = len(hits)
        if not hits:
            return ""
        block = f"## Project memory — {self.target.name}\n" + "\n".join(f"[{hit.bank_id}] {hit.text}" for hit in hits)
        return block[:self.service.recall_max_tokens * len(snapshot.banks) * 4]

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        return None

    def recall_status(self) -> Optional[RecallStatus]:
        return RecallStatus("Project memory", self._recall_count) if self._recall_count else None

    def sync_turn(self, user_content: str, assistant_content: str, *, session_id: str = "", **kwargs) -> None:
        snapshot = self._sync_snapshot.get() or self._current()
        if not snapshot.settings.retain_project:
            return
        sid = session_id or self._session_id
        item = RetainItem(
            content=f"User: {user_content}\nAssistant: {assistant_content}",
            context=f"Hermes project {self.target.slug} conversation",
            document_id=f"{sid}-{snapshot.turn_index}",
            tags=("hermes", f"project:{self.target.slug}", f"session:{sid}"), metadata=self._metadata,
        )
        for bank in snapshot.banks:
            if not bank.writable or not self._ready(bank.bank_id, "retain"):
                continue
            try:
                self.backend.retain(bank.bank_id, [item])
            except Exception:
                logger.debug("Project memory bank %s retain failed", bank.bank_id, exc_info=True)
                self._failed(bank.bank_id, "retain")
            else:
                self._recovered(bank.bank_id, "retain")

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        return [copy.deepcopy(_TOOL_SCHEMA)]

    def handle_tool_call(self, tool_name: str, args: dict[str, Any], **kwargs) -> str:
        from tools.registry import tool_error
        if tool_name != PROJECT_MEMORY_RECALL_TOOL:
            return tool_error(f"Unknown project memory tool: {tool_name}")
        banks = self._current().banks
        bank_filter = args.get("bank")
        if bank_filter is not None:
            selected = tuple(bank for bank in banks if bank.bank_id == bank_filter)
            if not selected:
                return tool_error(f"Unknown project memory bank: {bank_filter}")
            banks = selected
        query = args.get("query")
        if not isinstance(query, str) or not query.strip():
            return tool_error("Project memory recall requires a query")
        if not banks:
            return json.dumps({"results": [], "note": "no banks attached"})
        hits, failed = self._recall(query, banks)
        return json.dumps({"results": [{"bank": hit.bank_id, "text": hit.text, "kind": hit.kind,
                                       "occurred_at": hit.occurred_at} for hit in hits], "failed_banks": failed})

    def auto_retain_allows(self, provider: MemoryProvider) -> bool:
        snapshot = self._current()
        if provider is self:
            self._sync_snapshot.set(snapshot)
            return snapshot.settings.retain_project
        if provider.name == "builtin":
            return True
        return snapshot.settings.retain_shared

    def shutdown(self) -> None:
        self.backend.close()


def build_project_memory_provider(target: Optional[ProjectMemoryTarget]) -> Optional[ProjectMemoryProvider]:
    if target is None:
        return None
    from agent.agent_init_memory_provider import _warn_once
    from agent.project_memory.hindsight import backend_for_current_profile
    from hermes_cli.config import load_config
    try:
        service = load_service_config(load_config())
    except (ValueError, OSError) as exc:
        _warn_once("project", f"Project memory configuration is invalid: {exc}", kind="config")
        return None
    if service is None:
        return None
    return ProjectMemoryProvider(target, service, backend_for_current_profile(service))


def attach_project_memory(agent: Any, manager: Optional[MemoryManager],
                          target: Optional[ProjectMemoryTarget]) -> Optional[MemoryManager]:
    """Add the project slot before the caller initializes the combined manager."""
    provider = build_project_memory_provider(target)
    if provider is None:
        return manager
    if manager is None:
        from agent.memory_manager import MemoryManager
        manager = MemoryManager()
    manager.add_provider(provider, slot="project")
    manager.set_auto_retain_policy(provider.auto_retain_allows)
    return manager
