"""Durable room bindings and immutable conversation workspace pins."""
from __future__ import annotations

import os
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterator, Optional

from agent.i18n import t
from agent.runtime_cwd import reset_session_cwd, set_session_cwd
from gateway.session import SessionSource
from gateway.session_identity import identity_of
from hermes_cli.projects_db import Project, connect_closing, get_chat_binding, get_project
from hermes_constants import get_hermes_home
from tools.terminal_scope import (
    TerminalPolicyRefusal,
    get_terminal_scope,
    prepare_profile_terminal_scope,
    reset_terminal_scope,
    set_terminal_scope,
    terminal_env,
)

PIN_METADATA_KEY = "project_pin"


@dataclass(frozen=True)
class ProjectPin:
    project_id: Optional[str]
    slug: str = ""
    name: str = ""
    cwd: str = ""
    profile: str = ""
    pinned_at: int = 0

    @property
    def bound(self) -> bool:
        return self.project_id is not None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Any) -> Optional[ProjectPin]:
        if not isinstance(data, dict) or "project_id" not in data:
            return None
        project_id = data["project_id"]
        if project_id is not None and (not isinstance(project_id, str) or not project_id.strip()):
            return None
        strings = {key: data.get(key, "") for key in ("slug", "name", "cwd", "profile")}
        if not all(isinstance(value, str) for value in strings.values()):
            return None
        pinned_at = data.get("pinned_at", 0)
        if type(pinned_at) is not int or pinned_at < 0:
            return None
        return cls(project_id=project_id, pinned_at=pinned_at, **strings)


UNBOUND_PIN = ProjectPin(project_id=None)


class ProjectRouteError(RuntimeError):
    """A binding cannot safely supply a workspace for this turn."""

    def __init__(self, reason: str, **params: str) -> None:
        self.reason = reason
        self.params = params
        super().__init__(reason)

    def user_message(self) -> str:
        return t(f"gateway.project.error.{self.reason}", **self.params)


def binding_chat_id(source: SessionSource) -> str:
    return source.parent_chat_id or source.chat_id


def binding_key(runner: Any, source: SessionSource) -> tuple[str, str, str]:
    identity = identity_of(source)
    if identity is not None:
        transport_profile = identity.transport_profile
    elif bool(getattr(getattr(runner, "config", None), "multiplex_profiles", False)):
        raise ProjectRouteError("no_identity")
    else:
        transport_profile = getattr(runner, "_primary_profile_name", None) or "default"
    return source.platform.value, transport_profile, binding_chat_id(source)


def runtime_projects_db(runner: Any, source: SessionSource) -> Path:
    return Path(runner._resolve_profile_home_for_source(source)) / "projects.db"


def _project_terminal_base() -> dict[str, str]:
    base = get_terminal_scope()
    if base is None:
        # Only unscoped standalone launchers may supply ambient terminal policy.
        return prepare_profile_terminal_scope(
            get_hermes_home(), env_overlay={key: value for key, value in os.environ.items() if key.startswith("TERMINAL_")},
        )
    return base


def require_local_backend() -> None:
    token = set_terminal_scope(_project_terminal_base())
    try:
        backend = terminal_env("TERMINAL_ENV", "")
        if backend not in ("", "local"):
            raise ProjectRouteError("backend", backend=backend)
    finally:
        reset_terminal_scope(token)


def validate_project(project: Optional[Project], ref: str) -> str:
    if project is None:
        raise ProjectRouteError("missing", project=ref)
    if project.archived:
        raise ProjectRouteError("archived", project=ref)
    if not project.primary_path or not project.primary_path.strip():
        raise ProjectRouteError("no_path", project=ref)
    path = Path(project.primary_path).expanduser().resolve()
    if not path.is_dir():
        raise ProjectRouteError("path_missing", project=ref, path=str(path))
    return str(path)


def pin_from_binding(runner: Any, source: SessionSource, *, now: Optional[int] = None) -> ProjectPin:
    platform, transport_profile, chat_id = binding_key(runner, source)
    with connect_closing(db_path=runtime_projects_db(runner, source)) as conn:
        binding = get_chat_binding(conn, platform=platform, transport_profile=transport_profile, chat_id=chat_id)
        if binding is None:
            return UNBOUND_PIN
        project = get_project(conn, binding.project_id)
        # Durable references are IDs; a later project's slug must never retarget them.
        if project is not None and project.id != binding.project_id:
            project = None
        cwd = validate_project(project, binding.project_id)
        require_local_backend()
        # validate_project has rejected None before any project fields are read.
        assert project is not None
        identity = identity_of(source)
        profile = identity.runtime_profile if identity else (
            source.profile or getattr(runner, "_primary_profile_name", None) or "default")
        return ProjectPin(
            project_id=project.id, slug=project.slug, name=project.name, cwd=cwd,
            profile=profile, pinned_at=int(time.time()) if now is None else now,
        )


def revalidate_pin(runner: Any, source: SessionSource, pin: ProjectPin) -> None:
    if not pin.bound:
        return
    with connect_closing(db_path=runtime_projects_db(runner, source)) as conn:
        project = get_project(conn, pin.project_id)
        if project is not None and project.id != pin.project_id:
            project = None
        validate_project(project, pin.slug or pin.project_id)
    if not pin.cwd or not Path(pin.cwd).is_dir():
        raise ProjectRouteError("path_missing", project=pin.slug or pin.project_id, path=pin.cwd)
    require_local_backend()


def resolve_turn_pin(
    runner: Any, source: SessionSource, existing: Optional[ProjectPin], *, history_empty: bool,
) -> tuple[ProjectPin, bool]:
    if existing is not None:
        revalidate_pin(runner, source, existing)
        return existing, False
    if not history_empty:
        return UNBOUND_PIN, True
    return pin_from_binding(runner, source), True


@contextmanager
def project_turn_scope(pin: Optional[ProjectPin]) -> Iterator[None]:
    if pin is None or not pin.bound:
        yield
        return
    base = _project_terminal_base()
    overlay = base if isinstance(base, TerminalPolicyRefusal) else {**base, "TERMINAL_CWD": pin.cwd}
    terminal_token = set_terminal_scope(overlay)
    try:
        if not isinstance(base, TerminalPolicyRefusal):
            require_local_backend()
        cwd_token = set_session_cwd(pin.cwd)
        try:
            yield
        finally:
            reset_session_cwd(cwd_token)
    finally:
        reset_terminal_scope(terminal_token)
