"""Matrix command spelling and MSC4391 command-description manifests."""

from __future__ import annotations

import base64
import hashlib
import logging
import re
from collections.abc import Mapping, Sequence
from typing import Any

from agent.i18n import t

logger = logging.getLogger(__name__)

COMMAND_DESCRIPTION_EVENT_TYPE = "org.matrix.msc4391.command_description"
MAX_COMMANDS_PER_BOT = 500
DESCRIPTION_LIMIT = 200
_COMMAND_TOKEN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_PARAMETER_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_PLACEHOLDER = re.compile(r"^(?:<([A-Za-z][A-Za-z0-9 _-]{0,62})>|\[([A-Za-z][A-Za-z0-9 _-]{0,62})\])$")

_MATRIX_BANG_COMMAND_RE = re.compile(r"^!([A-Za-z][A-Za-z0-9_-]*)(?=$|\s)(.*)$", re.DOTALL)


def _resolve_matrix_bang_command(name: str) -> str | None:
    """Resolve a ``!command`` token (Matrix clients reserve ``/``) to a dispatchable token.
    Only known gateway/skill commands resolve, so ordinary exclamations stay chat text. Returns
    whichever candidate resolved — raw lowercased first, then ``_``→``-`` — never a forced
    canonical form: aliases pass through for the dispatcher."""
    if not name:
        return None
    candidates = list(dict.fromkeys((name.lower(), name.lower().replace("_", "-"))))
    try:
        from hermes_cli.commands import is_gateway_known_command
        for candidate in candidates:
            if is_gateway_known_command(candidate):
                return candidate
    except Exception:
        logger.debug("Matrix: is_gateway_known_command failed for %r", name, exc_info=True)
    try:
        from agent.skill_commands import get_skill_commands
        skill_commands = get_skill_commands() or {}  # keys are slash-prefixed ("/arxiv")
        for candidate in candidates:
            if f"/{candidate}" in skill_commands:
                return candidate
    except Exception:
        logger.debug("Matrix: get_skill_commands failed for %r", name, exc_info=True)
    return None


def _normalize_matrix_bang_command(text: str) -> str:
    """Convert Matrix ``!command`` aliases to normal Hermes ``/command`` text."""
    if not text or not text.startswith("!"):
        return text
    match = _MATRIX_BANG_COMMAND_RE.match(text)
    resolved = _resolve_matrix_bang_command(match.group(1)) if match else None
    if resolved is None:
        return text
    return f"/{resolved}{match.group(2) or ''}"


def command_state_key(command: str, bot_user_id: str) -> str:
    """MSC4391 uses padded standard base64, including slash and plus characters."""
    return base64.standard_b64encode(hashlib.sha256((command + bot_user_id).encode("utf-8")).digest()).decode()


def _text_description(text: str) -> dict[str, Any]:
    return {"m.text": [{"body": text}]}


def command_parameters(args_hint: str, *, subcommands: Sequence[str] = (),
                       default_args: bool = False) -> list[dict[str, Any]]:
    hint = args_hint.strip()
    if not hint and not default_args:
        return []
    match = _PLACEHOLDER.fullmatch(hint)
    placeholder = (match.group(1) or match.group(2)) if match else None
    parameter: dict[str, Any] = {
        "key": "args", "schema": {"schema_type": "primitive", "type": "string"},
        "optional": not hint.startswith("<"),
    }
    if placeholder and placeholder not in subcommands:
        parameter["key"] = re.sub(r"[^a-z0-9_]+", "_", placeholder.lower()).strip("_")
    elif hint:
        parameter["description"] = _text_description(hint[:DESCRIPTION_LIMIT])
    return [parameter]


def description_content(command: str, description: str, parameters: list[dict[str, Any]],
                        aliases: Sequence[str] = ()) -> dict[str, Any]:
    text = " ".join(description.split())
    if len(text) > DESCRIPTION_LIMIT:
        text = text[:DESCRIPTION_LIMIT - 3] + "..."
    content = {"command": command, "description": _text_description(text), "parameters": parameters}
    if aliases:
        content["aliases"] = list(aliases)
    return content


def _valid_parameter(parameter: Any) -> bool:
    return (
        isinstance(parameter, Mapping)
        and isinstance(parameter.get("key"), str)
        and bool(_PARAMETER_KEY.fullmatch(parameter["key"]))
        and parameter.get("schema") == {"schema_type": "primitive", "type": "string"}
        and isinstance(parameter.get("optional"), bool)
    )


def is_valid_description(content: Mapping[str, Any]) -> bool:
    """Validate the single-token/string-schema subset we publish to Element."""
    command = content.get("command")
    aliases = content.get("aliases", [])
    parameters = content.get("parameters")
    if not isinstance(command, str) or not _COMMAND_TOKEN.fullmatch(command):
        return False
    if not isinstance(aliases, list) or len(aliases) > 10:
        return False
    if any(not isinstance(alias, str) or not _COMMAND_TOKEN.fullmatch(alias) for alias in aliases):
        return False
    if not isinstance(parameters, list) or len(parameters) > 32:
        return False
    if not all(_valid_parameter(parameter) for parameter in parameters):
        return False
    keys = [parameter["key"] for parameter in parameters]
    return len(keys) == len(set(keys))


def _core_entries() -> list[tuple[str, dict[str, Any]]]:
    from hermes_cli.commands_platforms import _gateway_available_commands

    entries = []
    for cmd in _gateway_available_commands():
        try:
            entries.append((cmd.name, description_content(
                cmd.name, cmd.describe(), command_parameters(cmd.args_hint, subcommands=cmd.subcommands), cmd.aliases)))
        except Exception:
            logger.exception("Matrix: could not describe a core command")
    return entries


def _plugin_entries() -> list[tuple[str, dict[str, Any]]]:
    from hermes_cli.plugins import get_plugin_commands

    entries = []
    for name, meta in sorted(get_plugin_commands().items()):
        try:
            entries.append((name, description_content(
                name, meta.get("description") or t("slash.shared.plugin_command_desc"),
                command_parameters(meta.get("args_hint") or "", default_args=True))))
        except Exception:
            logger.exception("Matrix: could not describe a plugin command")
    return entries


def _skill_entries() -> list[tuple[str, dict[str, Any]]]:
    from hermes_cli.commands_platforms import _iter_gateway_skills

    entries = []
    for cmd_key, info, _rel in _iter_gateway_skills("matrix"):
        try:
            name = cmd_key.lstrip("/")
            entries.append((name, description_content(
                name, info.get("description") or "", command_parameters("", default_args=True))))
        except Exception:
            logger.exception("Matrix: could not describe a skill command")
    return sorted(entries, key=lambda entry: entry[0])


def _load_entries(source) -> list[tuple[str, dict[str, Any]]]:
    try:
        return source()
    except Exception:
        logger.exception("Matrix: could not discover command-description source %s", source.__name__)
        return []


def _command_name(raw: str) -> str | None:
    if not isinstance(raw, str):
        logger.debug("Matrix: omitted non-string command-description name")
        return None
    name = raw.lower()
    if _COMMAND_TOKEN.fullmatch(name):
        return name
    logger.debug("Matrix: omitted invalid command-description name")
    return None


def _claim_aliases(raw_aliases: Sequence[str], reserved: set[str]) -> list[str]:
    aliases = []
    for raw in raw_aliases:
        alias = _command_name(raw)
        if alias is not None and alias not in reserved:
            aliases.append(alias)
            reserved.add(alias)
            if len(aliases) == 10:
                break
    return aliases


def build_command_manifest(bot_user_id: str) -> dict[str, dict[str, Any]]:
    """Discover dispatchable commands in core > plugin > skill order, without renaming."""
    core = _load_entries(_core_entries)
    reserved = {name for raw, _content in core if (name := _command_name(raw)) is not None}
    manifest: dict[str, dict[str, Any]] = {}
    for raw, content in core:
        name = _command_name(raw)
        if name is None:
            continue
        aliases = _claim_aliases(content.pop("aliases", []), reserved)
        if aliases:
            content["aliases"] = aliases
        content["command"] = name
        if is_valid_description(content):
            manifest.setdefault(command_state_key(name, bot_user_id), content)
        else:
            logger.debug("Matrix: omitted invalid core command description")
    for source in (_plugin_entries, _skill_entries):
        for raw, content in _load_entries(source):
            name = _command_name(raw)
            if name is None or name in reserved:
                continue
            content["command"] = name
            if not is_valid_description(content):
                logger.debug("Matrix: omitted invalid command description")
                continue
            reserved.add(name)
            manifest[command_state_key(name, bot_user_id)] = content
    hidden = max(0, len(manifest) - MAX_COMMANDS_PER_BOT)
    if hidden:
        logger.info("Matrix: command-description cap hides %d commands", hidden)
    return dict(list(manifest.items())[:MAX_COMMANDS_PER_BOT])
