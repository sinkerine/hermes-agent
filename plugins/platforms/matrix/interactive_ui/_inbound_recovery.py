"""Room reconstruction requires immutable, profile-local ownership provenance."""

from typing import TYPE_CHECKING

from mautrix.api import Method, Path
from mautrix.errors import DecryptionError, MatrixError
from mautrix.types import SerializerError
from mautrix.types import EncryptedEvent, EventID, RoomID

from . import INTERACTIVE_UI_KEY
from .protocol import InteractiveUiError, parse_interactive_ui
from .store import UiRecord

if TYPE_CHECKING:
    from ..adapter import MatrixAdapter


def _wire(event) -> dict:
    return event if isinstance(event, dict) else event.serialize()


def _redacted(event: dict) -> bool:
    return bool((event.get("unsigned") or {}).get("redacted_because"))


async def _decrypt(adapter, event, room_id):
    raw = _wire(event)
    if raw.get("room_id") != room_id or _redacted(raw):
        return None
    if raw.get("type") == "m.room.encrypted":
        if adapter._client.crypto is None:
            return None
        encrypted = EncryptedEvent.deserialize(raw) if isinstance(event, dict) else event
        decrypted = _wire(await adapter._client.crypto.decrypt_megolm_event(encrypted))
        # Decryption must preserve the envelope that authenticates this event.
        if any(decrypted.get(key) != raw.get(key) for key in ("room_id", "event_id", "sender")):
            return None
        raw = decrypted
    if raw.get("type") != "m.room.message" or _redacted(raw):
        return None
    return raw


def _payload(content):
    if not isinstance(content, dict) or content.get("msgtype") != "m.text":
        return None
    parsed = parse_interactive_ui(content.get(INTERACTIVE_UI_KEY), strict=True)
    return None if isinstance(parsed, InteractiveUiError) else parsed.payload


async def _latest_payload(adapter, room_id, event_id, original):
    """No SDK relation helper exists in mautrix 0.21.1; use its escaped API path.

    Include encrypted relations by omitting an event-type filter. A continuation
    at the 50-event bound refuses recovery because the latest edit is unknown.
    """
    latest = original
    seen_tokens = set()
    count = 0
    token = None
    path = Path.v1.rooms[room_id].relations[event_id]["m.replace"]
    while True:
        query = {"limit": 50 - count, "dir": "b"}
        if token is not None:
            query["from"] = token
        page = await adapter._client.api.request(Method.GET, path, query_params=query)
        chunk = page.get("chunk")
        if not isinstance(chunk, list) or len(chunk) > 50 - count:
            return None
        count += len(chunk)
        for event in chunk:
            # Relations can omit room_id, since the endpoint already binds it.
            event = {"room_id": room_id, **event}
            if event.get("sender") != str(adapter._user_id):
                continue
            raw = await _decrypt(adapter, event, room_id)
            if raw is None:
                # An unreadable bot edit could be the latest revision.
                return None
            content = raw.get("content", {})
            relation = content.get("m.relates_to", {})
            if relation.get("rel_type") != "m.replace" or relation.get("event_id") != event_id:
                return None
            payload = _payload(content.get("m.new_content", {}))
            if payload is None or payload["ui_id"] != original["ui_id"]:
                return None
            if payload["revision"] == latest["revision"] and payload != latest:
                return None
            if payload["revision"] > latest["revision"]:
                latest = payload
        token = page.get("next_batch")
        if not token:
            return latest
        if count >= 50 or token in seen_tokens:
            return None
        seen_tokens.add(token)


async def recover_record(adapter: "MatrixAdapter", store, room_id, event_id, session_key, thread_id):
    if not store.has_replay_history(event_id):
        return None
    receipt = store.get_ownership(event_id)
    bot = str(adapter._user_id)
    if (receipt is None or receipt.room_id != room_id or receipt.session_key != session_key
            or receipt.thread_id != thread_id or receipt.bot_mxid != bot):
        return None
    try:
        event = await adapter._client.get_event(RoomID(room_id), EventID(event_id))
        raw = await _decrypt(adapter, event, room_id)
        if raw is None or raw.get("sender") != bot or raw.get("event_id") != event_id:
            return None
        content = raw.get("content", {})
        relation = content.get("m.relates_to", {})
        if relation.get("rel_type") == "m.replace":
            return None
        original_thread = relation.get("event_id") if relation.get("rel_type") == "m.thread" else None
        payload = _payload(content)
        if original_thread != thread_id or payload is None or payload["ui_id"] != receipt.ui_id:
            return None
        payload = await _latest_payload(adapter, room_id, event_id, payload)
        if payload is None or not store.can_restore(event_id, payload["revision"], payload["status"]):
            return None
    except (MatrixError, DecryptionError, SerializerError):
        return None
    record = UiRecord(event_id, room_id, payload["ui_id"], receipt.session_key, thread_id,
                      payload["revision"], payload["status"], payload, bot, recovered=True)
    store.insert(record)
    return record
