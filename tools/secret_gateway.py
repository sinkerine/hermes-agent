"""Profile-routed secret capture primitive for messaging gateways.

One pending capture is allowed per profile. Inbound adapters consume the next
text value for their profile before normal dispatch.
"""
from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional


@dataclass(frozen=True)
class SecretCaptureResult:
    success: bool
    stored_as: str
    validated: bool = False
    skipped: bool = False
    error_code: str | None = None


@dataclass
class SecretCaptureEntry:
    capture_id: str
    env_var: str
    prompt: str
    skill_name: str | None
    destination_home: str
    handler: Callable[[str, bool], SecretCaptureResult]
    event: threading.Event = field(default_factory=threading.Event)
    result: SecretCaptureResult | None = None
    state: str = "pending"


_lock = threading.RLock()
_pending_by_home: dict[str, SecretCaptureEntry] = {}
_notify: Optional[Callable[[SecretCaptureEntry], bool]] = None


def _home_key(home: str) -> str:
    from hermes_constants import hermes_home_key
    return hermes_home_key(home)


def set_notify_callback(callback: Optional[Callable[[SecretCaptureEntry], bool]]) -> None:
    global _notify
    with _lock:
        _notify = callback


def register(
    *,
    env_var: str,
    prompt: str,
    skill_name: str | None,
    destination_home: str,
    handler: Callable[[str, bool], SecretCaptureResult],
    notify: Optional[Callable[[SecretCaptureEntry], bool]] = None,
) -> SecretCaptureEntry | None:
    """Register one pending capture for ``destination_home``.

    A second capture in the same profile is busy; profiles do not contend.
    """
    with _lock:
        home_key = _home_key(destination_home)
        existing = _pending_by_home.get(home_key)
        if existing is not None and existing.state == "pending":
            return None
        entry = SecretCaptureEntry(
            capture_id=uuid.uuid4().hex,
            env_var=env_var,
            prompt=prompt,
            skill_name=skill_name,
            destination_home=destination_home,
            handler=handler,
        )
        _pending_by_home[home_key] = entry
        notify = notify or _notify
    if notify is not None:
        try:
            if not notify(entry):
                cancel(entry.capture_id, "prompt_delivery_failed")
                return None
        except Exception:
            cancel(entry.capture_id, "prompt_delivery_failed")
            return None
    return entry


def get_pending(destination_home: str | None = None) -> SecretCaptureEntry | None:
    with _lock:
        if destination_home is not None:
            entry = _pending_by_home.get(_home_key(destination_home))
            return entry if entry is not None and entry.state == "pending" else None
        entries = [entry for entry in _pending_by_home.values() if entry.state == "pending"]
        return entries[0] if len(entries) == 1 else None


def resolve_with_value(capture_id: str, value: str, *, redacted: bool = False) -> SecretCaptureResult:
    with _lock:
        entry = next((item for item in _pending_by_home.values() if item.capture_id == capture_id), None)
        if entry is None or entry.state != "pending":
            return SecretCaptureResult(False, "", error_code="no_pending_capture")
        entry.state = "consuming"
    try:
        result = entry.handler(value, redacted)
    except Exception:
        result = SecretCaptureResult(False, entry.env_var, error_code="persistence_failed")
    with _lock:
        entry.result = result
        entry.state = "stored" if result.success else "failed"
        entry.event.set()
        _pending_by_home.pop(_home_key(entry.destination_home), None)
    return result


def reject(capture_id: str, reason: str = "invalid_value") -> SecretCaptureResult:
    """Finish a capture without handing an invalid inbound value to its handler."""
    with _lock:
        entry = next((item for item in _pending_by_home.values() if item.capture_id == capture_id), None)
        if entry is None or entry.state != "pending":
            return SecretCaptureResult(False, "", error_code="no_pending_capture")
        result = SecretCaptureResult(False, entry.env_var, error_code=reason)
        entry.state = "failed"
        entry.result = result
        entry.event.set()
        _pending_by_home.pop(_home_key(entry.destination_home), None)
        return result


def cancel(capture_id: str, reason: str = "cancelled") -> bool:
    with _lock:
        entry = next((item for item in _pending_by_home.values() if item.capture_id == capture_id), None)
        if entry is None:
            return False
        entry.state = "cancelled"
        entry.result = SecretCaptureResult(False, entry.env_var, skipped=True, error_code=reason)
        entry.event.set()
        _pending_by_home.pop(_home_key(entry.destination_home), None)
        return True


def wait(entry: SecretCaptureEntry) -> SecretCaptureResult:
    entry.event.wait()
    return entry.result or SecretCaptureResult(False, entry.env_var, error_code="capture_cancelled")


def clear() -> None:
    with _lock:
        entries = list(_pending_by_home.values())
    for entry in entries:
        cancel(entry.capture_id, "gateway_shutdown")


def cancel_for_home(destination_home: str, reason: str = "gateway_shutdown") -> bool:
    """Cancel the capture owned by one profile, without touching other profiles."""
    with _lock:
        entry = _pending_by_home.get(_home_key(destination_home))
    return entry is not None and cancel(entry.capture_id, reason)


def has_pending() -> bool:
    with _lock:
        return any(entry.state == "pending" for entry in _pending_by_home.values())
