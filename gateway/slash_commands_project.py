"""Native messaging commands for durable room project bindings."""
from __future__ import annotations

import asyncio
import logging
import shlex
import sqlite3
from functools import partial
from pathlib import Path
from typing import Any

from agent.i18n import t
from gateway.platforms.event import MessageEvent
from gateway.project_routing import (
    PIN_METADATA_KEY, ProjectPin, ProjectRouteError, binding_key, require_local_backend,
    runtime_projects_db, validate_project,
)
from gateway.session_identity import identity_of
from gateway.slash_commands_project_memory import GatewayProjectMemoryCommandsMixin
from gateway.slash_access import policy_for_runner_source
from hermes_cli import projects_db as db

logger = logging.getLogger("gateway.run")


def _parse_args(tokens: list[str], *, create: bool = False) -> tuple[str, bool, str]:
    if not tokens or tokens[0].startswith("--"):
        raise ValueError("reference required")
    ref, *flags = tokens
    force, name = False, ""
    while flags:
        flag = flags.pop(0)
        if flag == "--force" and not force:
            force = True
        elif flag == "--name" and create and not name and flags:
            name = flags.pop(0).strip()
            if not name:
                raise ValueError("empty name")
        else:
            raise ValueError("unknown flag")
    return ref, force, name


def _binding(conn: sqlite3.Connection, key: tuple[str, str, str]) -> Any:
    return db.get_chat_binding(conn, platform=key[0], transport_profile=key[1], chat_id=key[2])


def _binding_project(conn: sqlite3.Connection, binding: Any) -> Any:
    project = db.get_project(conn, binding.project_id) if binding else None
    # Interactive references accept slugs; persisted references must remain exact IDs.
    return project if project and project.id == binding.project_id else None


def _bind(conn: sqlite3.Connection, key: tuple[str, str, str], project: db.Project,
          user_id: str, force: bool) -> str:
    cwd = validate_project(project, project.slug)
    require_local_backend()
    # Different conversation threads share this room binding, so check under the write lock.
    with db.write_txn(conn):
        old = _binding(conn, key)
        if old and old.project_id == project.id:
            return t("gateway.project.already_bound", project=project.slug, path=cwd)
        if old and not force:
            current = _binding_project(conn, old)
            return t("gateway.project.rebind_needs_force", project=current.slug if current else old.project_id)
        db.set_chat_binding(conn, platform=key[0], transport_profile=key[1], chat_id=key[2],
                            project_id=project.id, bound_by=user_id)
    return t("gateway.project.bound", project=project.slug, path=cwd)


def _existing_workspace_project(conn: sqlite3.Connection, slug: str, target: Path) -> tuple[db.Project | None, str]:
    project = next((p for p in db.list_projects(conn, include_archived=True) if p.slug == slug), None)
    if project and (not project.primary_path or Path(project.primary_path).resolve() != target):
        return None, t("gateway.project.slug_conflict", project=slug)
    project = project or db.find_by_primary_path(conn, str(target), include_archived=True)
    if project and project.archived:
        raise ProjectRouteError("archived", project=project.slug)
    return project, ""


def _create_project(conn: sqlite3.Connection, root_setting: str | None, slug_arg: str,
                    name: str) -> tuple[db.Project | None, str]:
    if not root_setting:
        return None, t("gateway.project.no_workspace_root")
    root = Path(root_setting).expanduser().resolve()
    if not root.is_dir():
        return None, t("gateway.project.unsafe_path")
    try:
        slug = db.normalize_slug(slug_arg)
    except ValueError:
        return None, t("gateway.project.invalid_slug")
    if not slug:
        return None, t("gateway.project.invalid_slug")
    target = root / slug
    if target.is_symlink() or target.resolve().parent != root:
        return None, t("gateway.project.unsafe_path")
    project, error = _existing_workspace_project(conn, slug, target)
    if error:
        return None, error
    # Refuse symlinked scaffold children too: retries must never write outside the workspace.
    if any((target / child).is_symlink() for child in ("AGENTS.md", "sources")):
        return None, t("gateway.project.unsafe_path")
    target.mkdir(exist_ok=True)
    agents = target / "AGENTS.md"
    if not agents.exists():
        try:
            with agents.open("x", encoding="utf-8") as stream:
                stream.write(f"# {name or slug}\n\nKeep project source material in sources/.\n")
        except FileExistsError:
            # A concurrent creator owns this file; never overwrite its instructions.
            if agents.is_symlink() or not agents.is_file():
                return None, t("gateway.project.unsafe_path")
    (target / "sources").mkdir(exist_ok=True)
    # Scaffolding can race or be retried; serialize the final reuse decision with insertion.
    with db.write_txn(conn):
        project, error = _existing_workspace_project(conn, slug, target)
        if error:
            return None, error
        reused = project is not None
        if project is None:
            project = db.get_project(conn, db.create_project(conn, name=name or slug, slug=slug, primary_path=str(target)))
    return project, t("gateway.project.reused" if reused else "gateway.project.created", project=project.slug)


class GatewayProjectCommandsMixin(GatewayProjectMemoryCommandsMixin):
    async def _handle_project_command(self, event: MessageEvent) -> str:
        denied = self._check_slash_access(event.source, "project")
        if denied:
            return denied
        try:
            tokens = shlex.split(event.get_command_args())
        except ValueError:
            return t("gateway.project.usage")
        command = tokens.pop(0).lower() if tokens else "info"
        handlers = {name: partial(self._project_work, name) for name in
                    ("info", "list", "create", "bind", "unbind", "help", "memory")}
        handler = handlers.get(command)
        if handler is None:
            return t("gateway.project.usage")
        mutates = command in {"create", "bind", "unbind"} or (
            command == "memory" and bool(tokens) and tokens[0] in {"init", "add", "remove", "set"})
        if mutates and not policy_for_runner_source(self, event.source).is_admin(event.source.user_id):
            return t("gateway.project.admin_only")
        try:
            async with self._async_profile_scope_for_source(event.source):
                return await handler(event, tokens)
        except ProjectRouteError as exc:
            logger.warning("Project command refused: reason=%s platform=%s room=%s", exc.reason,
                           event.source.platform.value, event.source.parent_chat_id or event.source.chat_id)
            return exc.user_message()
        except (OSError, sqlite3.Error) as exc:
            logger.warning("Project command failed: %s", exc)
            return t("gateway.project.failed", error=str(exc))
        except ValueError:
            return t("gateway.project.usage")

    async def _project_work(self, command: str, event: MessageEvent, args: list[str]) -> str:
        def work() -> str:
            with self._profile_scope_for_source(event.source):
                with db.connect_closing(db_path=runtime_projects_db(self, event.source)) as conn:
                    key = binding_key(self, event.source)
                    operations = {"info": self._project_info, "list": self._project_list,
                                  "create": self._project_create, "bind": self._project_bind,
                                  "unbind": self._project_unbind, "help": self._project_help,
                                  "memory": self._project_memory}
                    return operations[command](conn, key, event, args)
        return await asyncio.to_thread(work)

    def _project_help(self, conn: Any, key: Any, event: MessageEvent, args: list[str]) -> str:
        return t("gateway.project.usage")

    def _project_info(self, conn: Any, key: Any, event: MessageEvent, args: list[str]) -> str:
        if args:
            return t("gateway.project.usage")
        binding = _binding(conn, key)
        project = _binding_project(conn, binding)
        identity = identity_of(event.source)
        profile = identity.runtime_profile if identity else (
            event.source.profile or getattr(self, "_primary_profile_name", None) or "default")
        profile = f"{profile} ({runtime_projects_db(self, event.source).parent})"
        text = t("gateway.project.info_unbound", profile=profile)
        if binding:
            text = (t("gateway.project.info_bound", name=project.name, project=project.slug, id=project.id,
                      path=project.primary_path or "", profile=profile) if project else
                    t("gateway.project.info_dangling", project=binding.project_id))
        pin = ProjectPin.from_dict(self.session_store.get_session_metadata(
            self._session_key_for_source(event.source), PIN_METADATA_KEY))
        if pin is not None:
            text += "\n" + t("gateway.project.info_pin", project=pin.slug or pin.project_id or "—", path=pin.cwd or "—")
            if pin.project_id != (binding.project_id if binding else None):
                text += "\n" + t("gateway.project.info_pin_differs", project=project.slug if project else "—")
        return text

    def _project_list(self, conn: Any, key: Any, event: MessageEvent, args: list[str]) -> str:
        if args:
            return t("gateway.project.usage")
        projects = db.list_projects(conn)
        binding = _binding(conn, key)
        lines = [t("gateway.project.list_header")]
        lines.extend(t("gateway.project.list_item", mark="*" if binding and binding.project_id == p.id else " ",
                       name=p.name, project=p.slug, id=p.id, path=p.primary_path or "—") for p in projects)
        return "\n".join(lines) if projects else t("gateway.project.list_empty")

    def _project_bind(self, conn: Any, key: Any, event: MessageEvent, args: list[str]) -> str:
        ref, force, _ = _parse_args(args)
        project = db.get_project(conn, ref)
        if project is None:
            return t("gateway.project.unknown_project", project=ref)
        return _bind(conn, key, project, event.source.user_id, force)

    def _project_create(self, conn: Any, key: Any, event: MessageEvent, args: list[str]) -> str:
        ref, force, name = _parse_args(args, create=True)
        require_local_backend()
        project, text = _create_project(conn, self.config.project_workspace_root, ref, name)
        if project is None:
            return text
        return text + "\n" + _bind(conn, key, project, event.source.user_id, force)

    def _project_unbind(self, conn: Any, key: Any, event: MessageEvent, args: list[str]) -> str:
        if args:
            return t("gateway.project.usage")
        deleted = db.delete_chat_binding(conn, platform=key[0], transport_profile=key[1], chat_id=key[2])
        return t("gateway.project.unbound" if deleted else "gateway.project.not_bound")
