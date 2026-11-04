"""Serialize Matrix UI creation and revisions across send and durable commit."""

import asyncio
from copy import deepcopy
import secrets
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..adapter import MatrixAdapter

from ..adapter_html import sanitize_matrix_html
from . import INTERACTIVE_UI_KEY, INTERACTIVE_UI_VERSION
from .content import build_ui_content, build_ui_edit
from .fallback import render_fallback_markdown
from .media import prepare_uploads, prepare_wire_uploads, resolve_uploads
from .protocol import InteractiveUiError, canonical_json_bytes, parse_interactive_ui
from .store import InteractiveUiStore, UiRecord

_OWNED_FIELDS = ("version", "ui_id", "revision", "status")


def ui_error(error: InteractiveUiError) -> dict[str, Any]:
    return {"ok": False, "error": f"interactive_ui: {error.path}: {error.code}: {error.hint or 'Check the v1 field type, limit and references'}"}


def _content(payload, thread_metadata, record, summary):
    parsed = parse_interactive_ui(payload, strict=True)
    if isinstance(parsed, InteractiveUiError):
        return parsed
    fallback = render_fallback_markdown(parsed.payload)
    content = build_ui_content(parsed.payload, fallback, sanitize_html=sanitize_matrix_html,
                               thread_metadata=thread_metadata)
    if isinstance(content, InteractiveUiError) or record is None:
        return content
    return build_ui_edit(record.ui_event_id, content, summary or "")


class InteractiveUiController:
    def __init__(self, adapter: "MatrixAdapter", store: InteractiveUiStore) -> None:
        self.adapter = adapter
        self.store = store
        # Inbound action acceptance/status edits must use this same lock.
        self.lock = asyncio.Lock()

    async def present(
        self, *, chat_id: str, thread_metadata: dict | None, session_key: str,
        ui: dict[str, Any], summary: str | None = None, replace_ui_id: str | None = None,
        status: str | None = None,
    ) -> dict[str, Any]:
        async with self.lock:
            if self.adapter._closing or self.adapter._client is None:
                return {"ok": False, "error": "send_failed: disconnected"}
            return await self._present(chat_id, thread_metadata, session_key, ui, summary, replace_ui_id, status)

    def _replacement(self, replace_ui_id, chat_id, session_key, thread_metadata):
        record = self.store.get_by_ui_id(replace_ui_id)
        if record is None:
            return {"ok": False, "error": "unknown_ui_id"}
        thread_id = (thread_metadata or {}).get("thread_id") or None
        bot = str(self.adapter._user_id or self.adapter._client.mxid)
        if (record.room_id != chat_id or record.session_key != session_key
                or record.thread_id != thread_id or record.bot_mxid != bot):
            return {"ok": False, "error": "ui_in_other_conversation"}
        return record

    async def _present(self, chat_id, thread_metadata, session_key, ui, summary, replace_ui_id, status):
        if not isinstance(ui, dict):
            return ui_error(InteractiveUiError("ui", "not_object", "ui must be an object"))
        if status is not None and (status not in ("active", "expired", "superseded") or not replace_ui_id):
            return ui_error(InteractiveUiError("status", "bad_value", "Use active, expired or superseded only with replace_ui_id"))
        if summary is not None and not isinstance(summary, str):
            return ui_error(InteractiveUiError("summary", "bad_type", "summary must be a string"))
        record = None
        if replace_ui_id is not None:
            if not isinstance(replace_ui_id, str) or not replace_ui_id:
                return ui_error(InteractiveUiError("replace_ui_id", "bad_id", "Use a previously returned ui_id"))
            record = self._replacement(replace_ui_id, chat_id, session_key, thread_metadata)
            if isinstance(record, dict):
                return record
        payload = deepcopy(ui)
        ignored = [key for key in _OWNED_FIELDS if key in payload]
        payload.update(version=INTERACTIVE_UI_VERSION,
                       ui_id=record.ui_id if record else "ui_" + secrets.token_urlsafe(12),
                       revision=record.revision + 1 if record else 0, status=status or "active")
        # Validate JSON types/size before accessing/sanitizing model-owned nodes.
        uploads = prepare_uploads(payload, max_bytes=self.adapter._max_media_bytes)
        if isinstance(uploads, InteractiveUiError):
            return ui_error(uploads)
        preliminary = _content(payload, thread_metadata, record, summary)
        if isinstance(preliminary, InteractiveUiError):
            return ui_error(preliminary)
        try:
            await prepare_wire_uploads(self.adapter, chat_id, uploads)
            prepared = _content(payload, thread_metadata, record, summary)
            if isinstance(prepared, InteractiveUiError):
                return ui_error(prepared)
            await resolve_uploads(self.adapter, uploads)
        except Exception:  # health: allow BLE001 -- transport exceptions can contain private paths or credentials
            return {"ok": False, "error": "send_failed: image_upload"}
        for node in payload["nodes"]:
            if node.get("type") == "rich_text" and "html" in node:
                node["html"] = sanitize_matrix_html(node["html"])
        content = _content(payload, thread_metadata, record, summary)
        if isinstance(content, InteractiveUiError):
            return ui_error(content)
        try:
            event_id = await self.adapter._send_room_message(chat_id, content)
        except Exception:  # health: allow BLE001 -- never log transport exception text or payloads
            return {"ok": False, "error": "send_failed: transport"}
        saved_payload = (content["m.new_content"] if record else content)[INTERACTIVE_UI_KEY]
        if record:
            if not self.store.commit_revision(record.ui_event_id, expected_revision=record.revision, payload=saved_payload):
                return {"ok": False, "error": "revision_conflict"}
        else:
            self.store.insert(UiRecord(
                ui_event_id=event_id, room_id=chat_id, ui_id=saved_payload["ui_id"], session_key=session_key,
                thread_id=(thread_metadata or {}).get("thread_id") or None,
                revision=0, status=saved_payload["status"], payload=saved_payload,
                bot_mxid=str(self.adapter._user_id or self.adapter._client.mxid)))
        result = {"ok": True, "ui_id": saved_payload["ui_id"], "revision": saved_payload["revision"],
                  "event_id": event_id, "status": saved_payload["status"], "content_bytes": len(canonical_json_bytes(content))}
        if ignored:
            result["ignored_fields"] = ignored
        return result
