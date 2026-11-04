"""Authenticate and atomically accept declarative Matrix action replies."""

from contextlib import nullcontext
from copy import deepcopy
import json
import logging
import re
from typing import TYPE_CHECKING

from agent.i18n import t
from gateway.platforms.event import MessageEvent, MessageType

from ..adapter_html import sanitize_matrix_html
from . import INTERACTIVE_UI_ACTION_KEY, INTERACTIVE_UI_VERSION
from ._inbound_recovery import recover_record
from .content import build_ui_content, build_ui_edit
from .fallback import render_fallback_markdown
from .protocol import InteractiveUiError, parse_interactive_ui
from .submission import submit_sources, validate_action_values

if TYPE_CHECKING:
    from ..adapter import MatrixAdapter

logger = logging.getLogger(__name__)
_NONCE = re.compile(r"[A-Za-z0-9_-]{22,}\Z")


def _valid_action(action):
    if type(action) is not dict or type(action.get("version")) is not int or action["version"] != INTERACTIVE_UI_VERSION:
        return False
    if any(type(action.get(key)) is not str or not action[key] for key in
           ("ui_event_id", "ui_id", "action_id", "nonce")):
        return False
    revision = action.get("revision")
    return (action["ui_event_id"].startswith("$") and _NONCE.fullmatch(action["nonce"]) is not None
            and type(revision) in (int, float) and 0 <= revision <= 9007199254740991
            and revision == int(revision) and type(action.get("values")) is dict)


async def _notice(adapter, room_id, event_id, thread_id, code):
    content = {"msgtype": "m.notice", "body": t(f"platform.matrix.interactive_ui.{code}"), "m.mentions": {}}
    relation = {"m.in_reply_to": {"event_id": event_id}}
    if thread_id:
        relation.update(rel_type="m.thread", event_id=thread_id, is_falling_back=True)
    content["m.relates_to"] = relation
    await adapter._send_content_event(room_id, content)


def _confirmation(record):
    payload = deepcopy(record.payload)
    payload.update(revision=record.revision + 1, status="submitted",
                   status_text=t("platform.matrix.interactive_ui.submitted_status"))
    content = build_ui_content(payload, render_fallback_markdown(payload), sanitize_html=sanitize_matrix_html,
                               thread_metadata=None)
    if isinstance(content, InteractiveUiError):
        return content
    edit = build_ui_edit(record.ui_event_id, content, payload["status_text"])
    return edit if isinstance(edit, InteractiveUiError) else (payload, edit)


def _turn_text(record, source, values):
    quote = lambda value: json.dumps(value, ensure_ascii=False, allow_nan=False)
    lines = [f'[Interactive UI action] The user pressed {quote(source.label)} on card {quote(record.ui_id)} '
             f'({quote(record.payload.get("title", ""))}, revision {record.revision}).',
             f"action_id: {source.action_id}", "values:"]
    labels = {}
    for node in record.payload["nodes"]:
        key = node.get("bind", node.get("bind_selected"))
        if key:
            labels[key] = node.get("label", key)
    lines.extend(f"- {quote(key)} ({quote(labels.get(key, key))}) = {quote(value)}"
                 for key, value in sorted(values.items()))
    lines.append(f"To update this card, call present_ui with replace_ui_id={quote(record.ui_id)}.")
    return "\n".join(lines)


async def _record(adapter, controller, action, room_id, session_key, thread_id):
    record = controller.store.get_by_event(action["ui_event_id"])
    if record is None:
        record = await recover_record(adapter, controller.store, room_id, action["ui_event_id"], session_key, thread_id)
    return record


async def _accept(adapter, controller, action, room_id, sender, event_id, session_key, thread_id):
    """Return a turn's stored context, or a notice code; None means silent."""
    store = controller.store
    if not store.has_replay_history(action["ui_event_id"]):
        return "unknown"
    record = await _record(adapter, controller, action, room_id, session_key, thread_id)
    if record is None:
        return "unknown"
    if record.room_id != room_id or record.bot_mxid != str(adapter._user_id):
        return "unknown"
    if record.session_key != session_key or record.thread_id != thread_id:
        return "other_conversation"
    if record.ui_id != action["ui_id"]:
        return "invalid"
    if record.revision != action["revision"]:
        return "stale"
    if record.status != "active":
        return "inactive"
    parsed = parse_interactive_ui(record.payload, strict=True)
    if isinstance(parsed, InteractiveUiError):
        return "invalid"
    source = submit_sources(parsed).get(action["action_id"])
    if source is None or validate_action_values(parsed, source, action["values"]) is not None:
        return "invalid"
    confirmation = _confirmation(record) if source.single_use else None
    if isinstance(confirmation, InteractiveUiError):
        return "invalid"
    try:
        outcome = store.accept_action(record.ui_event_id, nonce=action["nonce"], action_event_id=event_id,
                                      sender=sender, expected_revision=action["revision"],
                                      single_use_payload=confirmation[0] if confirmation else None)
    except KeyError:
        # A missing record is not an acceptance. Reconstruct, then revalidate on a fresh delivery.
        await recover_record(adapter, store, room_id, record.ui_event_id, session_key, thread_id)
        return "unknown"
    if outcome == "duplicate":
        return None
    if outcome != "accepted":
        return outcome
    if confirmation:
        for _ in range(2):
            result = await adapter._send_content_event(room_id, confirmation[1])
            if result.success:
                break
        else:
            logger.warning("Matrix UI status edit failed for event %s", record.ui_event_id)
    return record, source


async def handle_action_event(adapter: "MatrixAdapter", *, room_id: str, sender: str, event_id: str,
                              event_ts: float, content: dict, relates_to: dict) -> None:
    ctx = await adapter._resolve_message_context(room_id, sender, event_id, content.get("body", "") or "",
                                                 content, relates_to, defer_effects=True)
    if ctx is None:
        return
    source = ctx[-1]
    identity = adapter._canonicalize(source)
    if getattr(source, "profile_route_rejected", False):
        return
    if not adapter._is_authorized_source(source):
        logger.debug("Matrix UI unauthorized event %s", event_id)
        return
    from gateway.run import _profile_runtime_scope
    # This callback runs before the gateway turn scope; localization and deferred
    # work must inherit the canonical runtime home, while the store stays pinned.
    scope = _profile_runtime_scope(identity.runtime_home, hydrate_secrets=False) if identity else nullcontext()
    with scope:
        await adapter._apply_message_context(room_id, event_id, ctx[3])
        await _handle_authorized_action(adapter, room_id, sender, event_id, content, ctx)


async def _handle_authorized_action(adapter, room_id, sender, event_id, content, ctx):
    source = ctx[-1]
    session_key = adapter._source_session_key(source)
    thread_id = ctx[3]
    controller = adapter._interactive_ui_controller
    if controller is None:
        await _notice(adapter, room_id, event_id, thread_id, "unknown")
        return
    action = content.get(INTERACTIVE_UI_ACTION_KEY)
    async with controller.lock:
        if adapter._closing or adapter._client is None:
            return
        # Event-id and nonce retries stay silent even when the card changed meanwhile.
        identifiers = action if type(action) is dict else {}
        target = identifiers.get("ui_event_id")
        nonce = identifiers.get("nonce")
        if controller.store.is_duplicate_action(target if type(target) is str else "",
                                                nonce if type(nonce) is str else "", event_id):
            return
        if not _valid_action(action):
            result = "invalid"
        else:
            result = await _accept(adapter, controller, action, room_id, sender, event_id, session_key, thread_id)
    if result is None:
        return
    if isinstance(result, str):
        await _notice(adapter, room_id, event_id, thread_id, result)
        return
    record, submit = result
    await adapter.handle_message(MessageEvent(
        text=_turn_text(record, submit, action["values"]), source=source, message_id=event_id,
        reply_to_message_id=record.ui_event_id, reply_to_text=record.payload.get("title"),
        message_type=MessageType.TEXT, raw_message=content, user_id=sender, user_name=ctx[4]))
