"""Persist plaintext credentials explicitly supplied through Matrix chat."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from tools.registry import registry

_ENV_VAR_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_MAX_VALUE_BYTES = 64 * 1024


def _result(**fields: Any) -> str:
    return json.dumps(fields, ensure_ascii=False)


def store_matrix_credential(env_var: str, value: str, description: str = "", **kwargs: Any) -> str:
    """Store exact user-supplied value in current profile .env."""
    if not isinstance(env_var, str) or not _ENV_VAR_NAME_RE.fullmatch(env_var):
        return _result(success=False, error_code="invalid_env_name")
    if not isinstance(value, str):
        return _result(success=False, error_code="value_not_string")
    if "\x00" in value:
        return _result(success=False, error_code="value_contains_nul")
    if len(value.encode("utf-8")) > _MAX_VALUE_BYTES:
        return _result(success=False, error_code="value_too_large")

    try:
        from hermes_cli.config import save_env_value_secure
        from hermes_constants import get_hermes_home
        from tools.skills_tool import load_env
        from agent.secret_scope import current_secret_scope

        home = Path(get_hermes_home())
        saved = save_env_value_secure(env_var, value)
        if not isinstance(saved, dict) or saved.get("success") is False:
            return _result(success=False, stored_as=env_var, error_code="persistence_failed")

        active_scope = current_secret_scope()
        if isinstance(active_scope, dict):
            active_scope[env_var] = value
        verified = load_env().get(env_var) == value
        if not verified:
            return _result(success=False, stored_as=env_var, error_code="verification_failed")
        return _result(
            success=True,
            stored_as=env_var,
            profile_home=str(home),
            validated=True,
            **({"description": description} if description else {}),
        )
    except ValueError as exc:
        # Canonical writer validation includes denylisted process-control names.
        text = str(exc).lower()
        return _result(
            success=False,
            stored_as=env_var,
            error_code="env_name_denied" if "denylist" in text else "invalid_env_name",
        )
    except Exception:
        return _result(success=False, stored_as=env_var, error_code="persistence_failed")


registry.register(
    name="store_matrix_credential",
    toolset="messaging",
    schema={
        "name": "store_matrix_credential",
        "description": (
            "Store plaintext credential received through ordinary Matrix chat in the active "
            "profile .env. Value may already exist in Matrix history, Hermes logs, session "
            "history, model context, tool transcripts, memories, skills, traces, and backups. "
            "Use only after explicit user intent; preserve value exactly."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "env_var": {"type": "string", "description": "Destination environment-variable name."},
                "value": {"type": "string", "description": "Exact plaintext value supplied by the user."},
                "description": {"type": "string", "description": "Optional audit description."},
            },
            "required": ["env_var", "value"],
        },
    },
    handler=lambda args, **kw: store_matrix_credential(
        env_var=args.get("env_var", ""),
        value=args.get("value", ""),
        description=args.get("description", ""),
        **kw,
    ),
    emoji="🔓",
)
