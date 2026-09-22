"""Model-visible bridge for arbitrary secure prompted secret capture.

The platform callback owns prompt delivery and persistence. This tool never accepts
or returns the secret value; it only requests a destination name and prompt.
"""
from __future__ import annotations

import json
import re
from typing import Any

from tools.registry import registry

_ENV_VAR_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def request_secret_capture(env_var: str, prompt: str = "", **kwargs: Any) -> str:
    """Request one gateway-mediated secret prompt for any valid destination name."""
    name = str(env_var or "").strip()
    label = str(prompt or "").strip() or f"value for {name}"
    if not _ENV_VAR_NAME_RE.fullmatch(name):
        return json.dumps({"success": False, "error_code": "invalid_env_name"})
    callback = kwargs.get("secret_capture_callback") or kwargs.get("callback")
    if callback is None:
        try:
            from tools.skills_tool import get_secret_capture_callback
            callback = get_secret_capture_callback()
        except Exception:
            callback = None
    if callback is None:
        return json.dumps({"success": False, "error_code": "capture_unavailable"})
    try:
        result = callback(name, label, {"requested_by": "request_secret_capture"})
    except Exception:
        return json.dumps({"success": False, "error_code": "capture_failed"})
    if not isinstance(result, dict):
        return json.dumps({"success": False, "error_code": "capture_failed"})
    safe = {key: result[key] for key in ("success", "stored_as", "validated", "skipped", "error_code") if key in result}
    safe.pop("value", None)
    safe.pop("secret", None)
    return json.dumps(safe)


registry.register(
    name="request_secret_capture",
    toolset="messaging",
    schema={
        "name": "request_secret_capture",
        "description": (
            "Request secure gateway capture for any valid profile environment-variable name. "
            "Gateway prompts user, consumes reply before model dispatch, persists value, and returns metadata only. "
            "Never ask user to paste secret into ordinary chat or include secret in tool arguments."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "env_var": {"type": "string", "description": "Destination environment-variable name."},
                "prompt": {"type": "string", "description": "Human-readable secret prompt."},
            },
            "required": ["env_var"],
        },
    },
    handler=lambda args, **kw: request_secret_capture(
        env_var=args.get("env_var", ""), prompt=args.get("prompt", ""), **kw),
    emoji="🔐",
)
