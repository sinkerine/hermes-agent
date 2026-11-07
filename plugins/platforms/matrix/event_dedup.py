"""Bounded Matrix event admission receipts, pinned to the receiving bot's store.

Like the in-memory cache, receipts claim events before downstream handling. They
prevent restart replay, but cannot make a command and its receipt one transaction.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from pathlib import Path

from utils import atomic_json_write

logger = logging.getLogger(__name__)


def _load_events(path: Path, limit: int) -> list[str]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(payload, list) or any(not isinstance(item, str) for item in payload):
            raise ValueError("expected a list of event IDs")
        return list(dict.fromkeys(payload))[-limit:]
    except FileNotFoundError:
        return []
    except (OSError, ValueError):
        logger.warning("Matrix: could not load handled-event receipts at %s", path, exc_info=True)
        return []


async def bind_event_receipts(adapter, store_dir: Path, homeserver: str, user_id: str) -> None:
    """Bind after authentication resolves the account, before initial sync dispatch."""
    identity = hashlib.sha256(json.dumps([homeserver, user_id]).encode("utf-8")).hexdigest()
    path = store_dir / f"handled_events_{identity}.json"
    if getattr(adapter, "_event_receipt_path", None) == path:
        return  # Reconnect retains the in-process cache even if a disk write failed.
    events = await asyncio.to_thread(_load_events, path, adapter._processed_events.maxlen)
    adapter._processed_events.clear()
    adapter._processed_events.extend(events)
    adapter._processed_events_set = set(events)
    adapter._event_receipt_path = path
    adapter._event_receipt_lock = asyncio.Lock()


def _remember_event(adapter, event_id: str) -> bool:
    if event_id in adapter._processed_events_set:
        return True
    if len(adapter._processed_events) == adapter._processed_events.maxlen:
        adapter._processed_events_set.discard(adapter._processed_events[0])
    adapter._processed_events.append(event_id)
    adapter._processed_events_set.add(event_id)
    return False


async def is_duplicate_event(adapter, event_id: str) -> bool:
    """Keep existing admission semantics; persist each new claim before its effects."""
    if not event_id:
        return False
    lock = getattr(adapter, "_event_receipt_lock", None)
    if lock is None:
        # Unconnected adapters (including hand-built test instances) retain memory-only dedup.
        return _remember_event(adapter, event_id)
    async with lock:
        if _remember_event(adapter, event_id):
            return True
        path = adapter._event_receipt_path
        events = list(adapter._processed_events)
        write = asyncio.create_task(asyncio.to_thread(
            atomic_json_write, path, events, indent=None, mode=0o600))
        try:
            # Keep writes ordered, including cancellation while a worker is still persisting.
            await asyncio.shield(write)
        except asyncio.CancelledError:
            await write
            raise
        except OSError:
            logger.warning("Matrix: could not persist handled-event receipts at %s", path, exc_info=True)
        return False
