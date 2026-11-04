"""Matrix interactive UI content and edit envelopes with strict byte budgets."""

from collections.abc import Callable, Mapping
from copy import deepcopy
from typing import Any

from . import INTERACTIVE_UI_KEY, NEW_EVENT_MAX_CONTENT_BYTES
from .fallback import render_fallback_html
from .protocol import InteractiveUiError, canonical_json_bytes


def _within_budget(content: dict[str, Any], limit: int) -> dict[str, Any] | InteractiveUiError:
    if len(canonical_json_bytes(content)) > limit:
        return InteractiveUiError("", "too_large", f"Complete event content must be at most {limit} UTF-8 bytes")
    return content


def build_ui_content(
    payload: Mapping[str, Any], fallback_md: str, *, to_html: Callable[[str], str] | None = None,
    sanitize_html: Callable[[str], str], thread_metadata: Mapping[str, Any] | None,
) -> dict[str, Any] | InteractiveUiError:
    """Build content using T4 HTML by default; explicit converters retain precedence.

    Production callers should omit to_html (or supply render_fallback_html) so the
    generated literals also survive when the optional Markdown package is absent.
    The supplied sanitizer always runs, including for custom converters.
    """
    convert = render_fallback_html if to_html is None else to_html
    safe_payload = deepcopy(dict(payload))
    for node in safe_payload["nodes"]:
        if node["type"] == "rich_text" and "html" in node:
            node["html"] = sanitize_html(node["html"])
    content = {
        "msgtype": "m.text", "body": fallback_md, "format": "org.matrix.custom.html",
        "formatted_body": sanitize_html(convert(fallback_md)), "m.mentions": {},
        INTERACTIVE_UI_KEY: safe_payload,
    }
    thread_id = str((thread_metadata or {}).get("thread_id") or "")
    if thread_id:
        content["m.relates_to"] = {
            "rel_type": "m.thread", "event_id": thread_id, "is_falling_back": True,
            "m.in_reply_to": {"event_id": thread_id},
        }
    return _within_budget(content, NEW_EVENT_MAX_CONTENT_BYTES)


def build_ui_edit(
    original_event_id: str, new_content: dict[str, Any], summary: str,
) -> dict[str, Any] | InteractiveUiError:
    """Keep the complete revision only in m.new_content, targeting the original."""
    from ..adapter import _MAX_CONTENT_BYTES

    replacement = deepcopy(new_content)
    replacement.pop("m.relates_to", None)
    replacement["m.mentions"] = {}
    title = replacement[INTERACTIVE_UI_KEY].get("title", "")
    content = {
        "msgtype": "m.text", "body": "* " + (summary or title), "m.mentions": {},
        "m.new_content": replacement,
        "m.relates_to": {"rel_type": "m.replace", "event_id": original_event_id},
    }
    return _within_budget(content, _MAX_CONTENT_BYTES)
