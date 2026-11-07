"""Room-bound project memory management, executed in the runtime profile scope."""
from __future__ import annotations

from agent.i18n import t
from agent.project_memory import ops
from gateway.session_identity import identity_of
from hermes_cli.config import load_config


class GatewayProjectMemoryCommandsMixin:
    def _project_memory(self, conn, key, event, args):
        from gateway.slash_commands_project import _binding, _binding_project

        binding = _binding(conn, key)
        if binding is None:
            return t("gateway.project.memory.not_bound")
        project = _binding_project(conn, binding)
        if project is None:
            return t("gateway.project.info_dangling", project=binding.project_id)
        command, *rest = args or ["info"]
        handlers = {"info": self._project_memory_info, "init": self._project_memory_init,
                    "add": self._project_memory_add, "remove": self._project_memory_remove,
                    "set": self._project_memory_set}
        handler = handlers.get(command)
        if handler is None:
            return t("gateway.project.usage")
        try:
            return handler(conn, project, event, rest)
        except ops.ProjectMemoryOpError as exc:
            return t(f"gateway.project.memory.error.{exc.reason}", **exc.params)

    def _project_memory_info(self, conn, project, event, args):
        if args:
            raise ValueError("unexpected arguments")
        info = ops.overview(conn, project, load_config())
        lines = [t("gateway.project.memory.info", project=project.slug,
                   service=info.service_url or t("gateway.project.memory.no_service"),
                   shared="on" if info.settings.retain_shared else "off",
                   project_retain="on" if info.settings.retain_project else "off")]
        if info.service_error:
            lines.append(t("gateway.project.memory.error.invalid_service", error=info.service_error))
        lines.extend(t("gateway.project.memory.bank", bank=b.bank_id,
                       mode="rw" if b.writable else "recall-only") for b in info.banks)
        if not info.banks:
            lines.append(t("gateway.project.memory.empty"))
        return "\n".join(lines)

    def _project_memory_init(self, conn, project, event, args):
        if len(args) > 1:
            raise ValueError("unexpected arguments")
        identity = identity_of(event.source)
        profile = identity.runtime_profile if identity else (
            event.source.profile or getattr(self, "_primary_profile_name", None) or "default")
        row, _ = ops.init_bank(conn, project, load_config(), profile=profile,
                               bank_id=args[0] if args else None, actor=event.source.user_id)
        return t("gateway.project.memory.attached", bank=row.bank_id)

    def _project_memory_add(self, conn, project, event, args):
        if len(args) not in (1, 2) or (len(args) == 2 and args[1] != "--recall-only"):
            raise ValueError("expected bank and optional --recall-only")
        backend = ops.configured_backend(load_config())
        try:
            row, _, exists = ops.attach_bank(conn, project, args[0], recall_only=len(args) == 2,
                                             actor=event.source.user_id, backend=backend)
        finally:
            if backend:
                backend.close()
        text = t("gateway.project.memory.attached", bank=row.bank_id)
        if exists is False:
            text += "\n" + t("gateway.project.memory.unknown_bank", bank=row.bank_id)
        return text

    def _project_memory_remove(self, conn, project, event, args):
        if len(args) != 1:
            raise ValueError("expected bank")
        ops.detach_bank(conn, project, args[0])
        return t("gateway.project.memory.detached", bank=args[0])

    def _project_memory_set(self, conn, project, event, args):
        fields = {"retain-shared": "retain_shared", "retain-project": "retain_project"}
        if len(args) != 2 or args[0] not in fields or args[1] not in ("on", "off"):
            raise ValueError("expected retain flag and on/off")
        ops.update_settings(conn, project, actor=event.source.user_id,
                            **{fields[args[0]]: args[1] == "on"})
        return t("gateway.project.memory.updated")
