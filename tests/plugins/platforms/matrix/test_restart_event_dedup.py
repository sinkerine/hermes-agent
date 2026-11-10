"""Recent Matrix management events must not be delivered again after restart."""
from __future__ import annotations

import asyncio
import json
import sys
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.matrix import adapter as matrix
from tests.gateway.test_matrix import _make_fake_mautrix


@pytest.fixture
def connect_adapter(monkeypatch):
    """Exercise real connect/store binding with transport and downstream dispatch stood in."""
    fake_modules = _make_fake_mautrix()
    client_class = fake_modules["mautrix.client"].Client
    for name, module in fake_modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setattr(matrix.adapter_transport, "create_matrix_session", lambda proxy: MagicMock())
    monkeypatch.setattr(matrix.MatrixAdapter, "_sync_loop", AsyncMock())
    monkeypatch.setattr(matrix.MatrixAdapter, "_connect_initial_sync", AsyncMock())

    async def connect(store, *, user="@bot:example.org", server="https://matrix.example.org"):
        monkeypatch.setattr(matrix, "_get_hermes_dir", lambda *args: store)
        monkeypatch.setattr(client_class, "whoami", AsyncMock(return_value=SimpleNamespace(
            user_id=user, device_id="DEVICE")), raising=False)
        adapter = matrix.MatrixAdapter(PlatformConfig(enabled=True, token="test-token", extra={
            "homeserver": server, "e2ee_mode": "off"}))
        adapter._is_allowed_matrix_room_event = AsyncMock(return_value=True)
        adapter._handle_text_message = AsyncMock()
        assert await adapter.connect()
        return adapter

    return connect


def recent_event(event_id, body="!project memory add team-kb --recall-only"):
    return SimpleNamespace(room_id="!room:example.org", sender="@alice:example.org",
                           event_id=event_id, timestamp=int(time.time() * 1000),
                           content={"msgtype": "m.text", "body": body})


@pytest.mark.asyncio
async def test_restart_suppresses_handled_management_but_admits_unseen_recent_event(tmp_path, connect_adapter):
    store = tmp_path / "profile" / "store"
    handled = recent_event("$handled")
    unseen = recent_event("$unseen")
    first = await connect_adapter(store)
    try:
        await first._on_room_message(handled)
        await first._on_room_message(handled)
        first._handle_text_message.assert_awaited_once()
    finally:
        await first.disconnect()

    restarted = await connect_adapter(store)
    try:
        # Both messages are inside the startup grace. Only one was handled before restart.
        assert restarted._startup_ts - 5 <= handled.timestamp / 1000 < restarted._startup_ts
        assert restarted._startup_ts - 5 <= unseen.timestamp / 1000 < restarted._startup_ts
        await restarted._on_room_message(handled)
        restarted._handle_text_message.assert_not_awaited()
        await restarted._on_room_message(unseen)
        restarted._handle_text_message.assert_awaited_once()
        assert restarted._handle_text_message.call_args.args[2] == "$unseen"
        # A reconnect of this same adapter retains the duplicate claim too.
        assert await restarted.connect(is_reconnect=True)
        await restarted._on_room_message(recent_event("$unseen"))
        restarted._handle_text_message.assert_awaited_once()
    finally:
        await restarted.disconnect()


@pytest.mark.asyncio
@pytest.mark.parametrize("separation", ["profile", "store", "bot", "homeserver"])
async def test_event_receipts_are_isolated(tmp_path, connect_adapter, separation):
    store = tmp_path / "profile-a" / "store"
    first = await connect_adapter(store)
    try:
        await first._on_room_message(recent_event("$same-id"))
    finally:
        await first.disconnect()
    second_store, identity = store, {}
    if separation == "profile":
        second_store = tmp_path / "profile-b" / "store"
    elif separation == "store":
        second_store = tmp_path / "profile-a" / "another-store"
    elif separation == "bot":
        identity["user"] = "@another:example.org"
    else:
        identity["server"] = "https://another.example.org"
    second = await connect_adapter(second_store, **identity)
    try:
        await second._on_room_message(recent_event("$same-id"))
        second._handle_text_message.assert_awaited_once()
    finally:
        await second.disconnect()


@pytest.mark.asyncio
async def test_event_history_remains_bounded_after_restart(tmp_path, connect_adapter):
    store = tmp_path / "store"
    first = await connect_adapter(store)
    limit = first._processed_events.maxlen
    try:
        for index in range(limit + 1):
            await first._on_room_message(recent_event(f"$event-{index}"))
        assert len(first._processed_events) == len(first._processed_events_set) == limit
    finally:
        await first.disconnect()
    restarted = await connect_adapter(store)
    try:
        await restarted._on_room_message(recent_event(f"$event-{limit}"))
        restarted._handle_text_message.assert_not_awaited()
        await restarted._on_room_message(recent_event("$event-0"))
        restarted._handle_text_message.assert_awaited_once()
        assert len(restarted._processed_events) == len(restarted._processed_events_set) == limit
        receipts = list(store.glob("handled_events_*.json"))
        assert len(receipts) == 1
        assert len(json.loads(receipts[0].read_text())) == limit
    finally:
        await restarted.disconnect()


@pytest.mark.asyncio
async def test_concurrent_redelivery_and_pinned_store(tmp_path, connect_adapter, monkeypatch):
    store = tmp_path / "owner" / "store"
    adapter = await connect_adapter(store)
    # Deferred callbacks must keep the store selected during connect, regardless of ambient scope.
    monkeypatch.setattr(matrix, "_get_hermes_dir", lambda *args: tmp_path / "other-profile")
    try:
        await asyncio.gather(*(adapter._on_room_message(recent_event("$concurrent")) for _ in range(4)))
        adapter._handle_text_message.assert_awaited_once()
    finally:
        await adapter.disconnect()
    restarted = await connect_adapter(store)
    try:
        await restarted._on_room_message(recent_event("$concurrent"))
        restarted._handle_text_message.assert_not_awaited()
        assert not (tmp_path / "other-profile").exists()
    finally:
        await restarted.disconnect()
