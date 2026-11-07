"""Project memory management shared by room commands and the CLI."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional

from agent.project_memory.backend import BackendError, ProjectMemoryBackend
from agent.project_memory.config import load_service_config
from agent.project_memory.hindsight import backend_for_current_profile
from hermes_cli import projects_db as db


class ProjectMemoryOpError(ValueError):
    def __init__(self, reason: str, **params: str) -> None:
        self.reason, self.params = reason, params
        messages = {
            "no_service": "Project memory service is not configured; set project_memory.hindsight_url first",
            "invalid_service": "Invalid project memory service configuration: {error}",
            "invalid_bank": "Invalid bank id: {bank}",
            "service_error": "Project memory service request failed: {error}",
            "not_attached": "Bank {bank} is not attached to this project",
        }
        super().__init__(messages[reason].format(**params))


@dataclass(frozen=True)
class MemoryOverview:
    service_url: Optional[str]
    service_error: str
    banks: list[db.ProjectMemoryBank]
    settings: db.ProjectMemorySettings


def overview(conn, project: db.Project, config: Mapping[str, Any]) -> MemoryOverview:
    error, url = "", None
    try:
        service = load_service_config(config)
        url = service.url if service else None
    except ValueError as exc:
        error = str(exc)
    return MemoryOverview(url, error, db.list_memory_banks(conn, project.id),
                          db.get_memory_settings(conn, project.id))


def configured_backend(config: Mapping[str, Any]) -> Optional[ProjectMemoryBackend]:
    """Caller owns and closes the client, inside its runtime profile scope."""
    try:
        service = load_service_config(config)
    except ValueError as exc:
        raise ProjectMemoryOpError("invalid_service", error=str(exc)) from exc
    return backend_for_current_profile(service) if service else None


def _bank_id(bank_id: str) -> str:
    try:
        return db.normalize_bank_id(bank_id)
    except ValueError as exc:
        raise ProjectMemoryOpError("invalid_bank", bank=bank_id) from exc


def init_bank(conn, project: db.Project, config: Mapping[str, Any], *, profile: str,
              bank_id: Optional[str] = None, actor: Optional[str] = None,
              backend: Optional[ProjectMemoryBackend] = None) -> tuple[db.ProjectMemoryBank, bool]:
    bank_id = _bank_id(bank_id if bank_id is not None else f"{profile}-{project.slug}")
    # Validate service presence even when a backend was supplied by a caller.
    try:
        service = load_service_config(config)
    except ValueError as exc:
        raise ProjectMemoryOpError("invalid_service", error=str(exc)) from exc
    if service is None:
        raise ProjectMemoryOpError("no_service")
    owned = backend is None
    backend = backend or backend_for_current_profile(service)
    try:
        backend.ensure_bank(bank_id, name=project.name)
    except BackendError as exc:
        raise ProjectMemoryOpError("service_error", error=str(exc)) from exc
    finally:
        if owned:
            backend.close()
    return db.add_memory_bank(conn, project.id, bank_id, writable=True, added_by=actor)


def attach_bank(conn, project: db.Project, bank_id: str, *, recall_only: bool,
                actor: Optional[str] = None, backend: Optional[ProjectMemoryBackend] = None
                ) -> tuple[db.ProjectMemoryBank, bool, Optional[bool]]:
    bank_id = _bank_id(bank_id)
    try:
        exists = backend.bank_exists(bank_id) if backend else None
    except BackendError as exc:
        raise ProjectMemoryOpError("service_error", error=str(exc)) from exc
    row, created = db.add_memory_bank(conn, project.id, bank_id, writable=not recall_only, added_by=actor)
    return row, created, exists


def detach_bank(conn, project: db.Project, bank_id: str) -> bool:
    bank_id = _bank_id(bank_id)
    if not db.remove_memory_bank(conn, project.id, bank_id):
        raise ProjectMemoryOpError("not_attached", bank=bank_id)
    return True


def update_settings(conn, project: db.Project, *, retain_shared: Optional[bool] = None,
                    retain_project: Optional[bool] = None, actor: Optional[str] = None
                    ) -> db.ProjectMemorySettings:
    return db.set_memory_settings(conn, project.id, retain_shared=retain_shared,
                                  retain_project=retain_project, updated_by=actor)
