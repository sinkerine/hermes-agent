"""Skill readiness: required env vars, secret capture, and setup notes. Split out of
``tools.skills_tool`` (names re-imported there); module state stays there and is read lazily.
"""

import logging
import re
from enum import Enum
from typing import Any, Dict, List

from hermes_constants import display_hermes_home
from utils import env_var_enabled

logger = logging.getLogger("tools.skills_tool")

_ENV_VAR_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_REMOTE_ENV_BACKENDS = frozenset({"docker", "singularity", "modal", "ssh", "daytona", "vercel_sandbox"})


class SkillReadinessStatus(str, Enum):
    AVAILABLE = "available"
    SETUP_NEEDED = "setup_needed"
    UNSUPPORTED = "unsupported"


def _is_remote_env_backend(backend: str) -> bool:
    if backend in _REMOTE_ENV_BACKENDS or not backend or backend == "local":
        return backend in _REMOTE_ENV_BACKENDS
    try:
        from agent.terminal_env_registry import provider_flag
        return bool(provider_flag(backend, "is_remote", False))
    except Exception:
        return False


def _as_dict_list(raw: Any) -> list:
    return [raw] if isinstance(raw, dict) else raw if isinstance(raw, list) else []


def _clean_str(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _get_required_environment_variables(frontmatter: dict[str, Any]) -> list[dict[str, Any]]:
    """Merge supported skill env-var declaration formats, deduping by validated name."""
    setup = frontmatter.get("setup")
    setup = setup if isinstance(setup, dict) else {}
    setup_help = _clean_str(setup.get("help"))
    prereqs = frontmatter.get("prerequisites")
    legacy = (prereqs.get("env_vars") if isinstance(prereqs, dict) else None) or []
    legacy = [legacy] if isinstance(legacy, str) else legacy
    required: dict[str, dict[str, Any]] = {}
    declared = _as_dict_list(frontmatter.get("required_environment_variables"))
    entries = [{"name": item} if isinstance(item, str) else item for item in declared
               if isinstance(item, (str, dict))]
    entries += [
        {"name": item.get("env_var"), "prompt": item.get("prompt"),
         "url": str(item.get("provider_url") or item.get("url") or "").strip() or None}
        for item in _as_dict_list(setup.get("collect_secrets")) if isinstance(item, dict)]
    entries += [{"name": str(value)} for value in legacy if str(value).strip()]
    for entry in entries:
        name = str(entry.get("name") or entry.get("env_var") or "").strip()
        if not name or name in required or not _ENV_VAR_NAME_RE.match(name):
            continue
        normalized: dict[str, Any] = {
            "name": name, "prompt": str(entry.get("prompt") or f"Enter value for {name}").strip()}
        if help_text := _clean_str(entry.get("help") or entry.get("provider_url") or entry.get("url") or setup_help):
            normalized["help"] = help_text
        if required_for := _clean_str(entry.get("required_for")):
            normalized["required_for"] = required_for
        if entry.get("optional"):
            normalized["optional"] = True
        required[name] = normalized
    return list(required.values())


def _capture_result(missing_names, setup_skipped=False, gateway_setup_hint=None, **outcome):
    result = {"missing_names": missing_names, "setup_skipped": setup_skipped,
              "gateway_setup_hint": gateway_setup_hint}
    result.update({key: value for key, value in outcome.items() if value is not None})
    return result


def _capture_required_environment_variables(
    skill_name: str, missing_entries: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Invoke turn-local or registered capture callbacks; explain unsupported gateways."""
    if not missing_entries:
        return _capture_result([])
    missing_names = [entry["name"] for entry in missing_entries]
    from tools import skills_tool as skills
    callback = skills.get_secret_capture_callback()
    if callback is None and _is_gateway_surface() and not env_var_enabled("HERMES_INTERACTIVE"):
        try:
            from gateway.platforms.base import GATEWAY_SECRET_CAPTURE_UNSUPPORTED_MESSAGE as hint
        except Exception:
            hint = (f"Secure secret entry is not available. Load this skill in the local CLI to be "
                    f"prompted, or add the key to {display_hermes_home()}/.env manually.")
        return _capture_result(missing_names, gateway_setup_hint=hint, capture_supported=False,
                               capture_outcome="unavailable")
    if callback is None:
        return _capture_result(missing_names, capture_supported=False, capture_outcome="unavailable")

    remaining: List[str] = []
    attempted = False
    for entry in missing_entries:
        metadata = {"skill_name": skill_name,
                    **{key: entry[key] for key in ("help", "required_for") if entry.get(key)}}
        try:
            attempted = True
            result = callback(entry["name"], entry["prompt"], metadata)
        except Exception:
            logger.warning("Secret capture callback failed for %s", entry["name"], exc_info=True)
            result = {"success": False, "skipped": True}
        if not (isinstance(result, dict) and result.get("success") and not result.get("skipped")):
            remaining.append(entry["name"])
    stored = attempted and not remaining
    return _capture_result(remaining, bool(remaining),
                           gateway_setup_hint=None if stored else
                           "Secure prompted capture was attempted; no secret value is exposed to the model.",
                           capture_supported=True,
                           capture_outcome="stored" if stored else ("failed" if attempted else "unavailable"))


def _is_gateway_surface() -> bool:
    if env_var_enabled("HERMES_GATEWAY_SESSION"):
        return True
    from gateway.session_context import get_session_env
    return bool(get_session_env("HERMES_SESSION_PLATFORM"))


def _is_env_var_persisted(var_name: str, env_snapshot: Dict[str, str]) -> bool:
    """Check profile snapshot first, then the active scoped secret source."""
    if var_name in env_snapshot:
        return bool(env_snapshot[var_name])
    try:
        from agent.secret_scope import UnscopedSecretError, get_secret
        return bool(get_secret(var_name))
    except UnscopedSecretError:
        return False


def _build_setup_note(
    readiness_status: SkillReadinessStatus, missing: list[str], setup_help: str | None = None) -> str | None:
    if readiness_status != SkillReadinessStatus.SETUP_NEEDED:
        return None
    note = f"Setup needed before using this skill: missing {', '.join(missing) if missing else 'required prerequisites'}."
    return f"{note} {setup_help}" if setup_help else note
