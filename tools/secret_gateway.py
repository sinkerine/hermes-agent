"""Process-wide secret capture primitive for messaging gateways.

One pending capture is allowed per process. The inbound adapter consumes the
next eligible text value before normal dispatch and resolves this entry.
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
_pending: SecretCaptureEntry | None = None
_notify: Optional[Callable[[SecretCaptureEntry], bool]] = None


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
    """Register sole pending capture. Return None when another is active."""
    global _pending
    with _lock:
        if _pending is not None and _pending.state == "pending":
            return None
        entry = SecretCaptureEntry(
            capture_id=uuid.uuid4().hex,
            env_var=env_var,
            prompt=prompt,
            skill_name=skill_name,
            destination_home=destination_home,
            handler=handler,
        )
        _pending = entry
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


def get_pending() -> SecretCaptureEntry | None:
    with _lock:
        if _pending is None or _pending.state != "pending":
            return None
        return _pending


def resolve_with_value(capture_id: str, value: str, *, redacted: bool = False) -> SecretCaptureResult:
    global _pending
    with _lock:
        entry = _pending
        if entry is None or entry.capture_id != capture_id or entry.state != "pending":
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
        _pending = None
    return result


def cancel(capture_id: str, reason: str = "cancelled") -> bool:
    global _pending
    with _lock:
        entry = _pending
        if entry is None or entry.capture_id != capture_id:
            return False
        entry.state = "cancelled"
        entry.result = SecretCaptureResult(False, entry.env_var, skipped=True, error_code=reason)
        entry.event.set()
        _pending = None
        return True


def wait(entry: SecretCaptureEntry) -> SecretCaptureResult:
    entry.event.wait()
    return entry.result or SecretCaptureResult(False, entry.env_var, error_code="capture_cancelled")


def clear() -> None:
    with _lock:
        entry = _pending
    if entry is not None:
        cancel(entry.capture_id, "gateway_shutdown")


def has_pending() -> bool:
    return get_pending() is not None
