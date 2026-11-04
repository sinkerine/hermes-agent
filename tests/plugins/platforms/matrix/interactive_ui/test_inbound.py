"""Inbound actions exercise real Matrix guards, session keys and SQLite acceptance."""

import asyncio
from copy import deepcopy
import time
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent.i18n import t
from gateway.config import PlatformConfig
from plugins.platforms.matrix.adapter import MatrixAdapter
from plugins.platforms.matrix.interactive_ui import INTERACTIVE_UI_ACTION_KEY as ACTION, INTERACTIVE_UI_KEY as UI
from plugins.platforms.matrix.interactive_ui.producer import InteractiveUiController
from plugins.platforms.matrix.interactive_ui.store import InteractiveUiStore

ROOM = "!room:example.org"
BOT = "@bot:example.org"
ALICE = "@alice:example.org"
BOB = "@bob:example.org"
ROOT = "$root"


class Harness:
    def __init__(self, path, *, profile=None, extra=None):
        self.adapter = MatrixAdapter(PlatformConfig(enabled=True, extra={
            "homeserver": "https://example.org", "user_id": BOT, "e2ee_mode": "optional",
            "allowed_users": [ALICE, BOB], "require_mention": True, "session_scope": "room",
            **(extra or {})}))
        adapter = self.adapter
        adapter._owner_profile = profile
        adapter._startup_ts = time.time() - 60
        adapter._is_dm_room = AsyncMock(return_value=False)
        adapter._resolve_room_identity = AsyncMock(return_value=SimpleNamespace(
            display_name="Room", room_topic="Topic", server_name="example.org"))
        adapter._get_display_name = AsyncMock(return_value="Alice")
        adapter._background_read_receipt = lambda *args: None
        self.sent = []
        self.order = []
        self.turns = []
        self.store = InteractiveUiStore(path)

        async def send(room, event_type, content):
            self.order.append("edit" if "m.new_content" in content else content["msgtype"])
            self.sent.append((str(room), deepcopy(content)))
            return f"$sent{len(self.sent)}"

        async def handle(event):
            self.order.append("turn")
            self.turns.append(event)

        adapter._client = SimpleNamespace(mxid=BOT, send_message_event=AsyncMock(side_effect=send),
                                          crypto=None, get_event=AsyncMock(), api=SimpleNamespace(request=AsyncMock()))
        adapter.handle_message = AsyncMock(side_effect=handle)
        adapter._enqueue_text_event = lambda event: pytest.fail("Actions must bypass text batching")
        self.controller = InteractiveUiController(adapter, self.store)
        adapter._interactive_ui_controller = self.controller
        adapter._interactive_ui_store = self.store

    def source(self, sender=ALICE, thread=ROOT, room=ROOM):
        return self.adapter.build_source(chat_id=room, chat_type="group", user_id=sender,
                                         thread_id=thread, parent_chat_id=room if thread else None)

    async def present(self, *, single_use=True, thread=ROOT, sender=ALICE):
        ui = {"title": "Choose a plan", "state": {"plan": "basic"}, "root": "stack", "nodes": [
            {"id": "stack", "type": "stack", "direction": "vertical", "children": ["plan", "submit"]},
            {"id": "plan", "type": "select", "bind": "plan", "label": "Plan", "options": [
                {"value": "basic", "label": "Basic"}, {"value": "plus", "label": "Plus"}]},
            {"id": "submit", "type": "button", "label": "Pick plan", "action": {
                "type": "submit", "action_id": "pick", "include": ["plan"], "single_use": single_use}}]}
        key = self.adapter._source_session_key(self.source(sender=sender, thread=thread))
        result = await self.controller.present(chat_id=ROOM, thread_metadata={"thread_id": thread},
                                               session_key=key, ui=ui)
        assert result["ok"], result
        self.card = result
        self.original = deepcopy(self.sent[-1][1])
        self.reset()
        return result

    def reset(self):
        self.sent.clear()
        self.order.clear()
        self.turns.clear()
        self.adapter._client.send_message_event.reset_mock()
        self.adapter.handle_message.reset_mock()

    def event(self, *, event_id="$action", nonce="a" * 22, sender=ALICE, room=ROOM, thread=ROOT, action=None):
        payload = {"version": 1, "ui_event_id": self.card["event_id"], "ui_id": self.card["ui_id"],
                   "revision": 0, "action_id": "pick", "values": {"plan": "plus"}, "nonce": nonce,
                   **(action or {})}
        relation = {"m.in_reply_to": {"event_id": payload["ui_event_id"]}}
        if thread:
            relation.update(rel_type="m.thread", event_id=thread)
        return SimpleNamespace(room_id=room, sender=sender, event_id=event_id, timestamp=int(time.time() * 1000),
                               content={"msgtype": "m.text", "body": "CLIENT BODY MUST NOT BECOME INTENT",
                                        "m.mentions": {"user_ids": [BOT]}, "m.relates_to": relation, ACTION: payload})

    async def act(self, **kwargs):
        await self.adapter._on_room_message(self.event(**kwargs))

    def notice(self, code, *, room=ROOM, thread=ROOT):
        assert len(self.sent) == 1
        actual_room, content = self.sent[0]
        assert actual_room == room and content["msgtype"] == "m.notice"
        assert content["body"] == t(f"platform.matrix.interactive_ui.{code}")
        if thread:
            assert content["m.relates_to"]["event_id"] == thread
        else:
            assert "rel_type" not in content["m.relates_to"]
        assert not self.turns


@pytest.fixture
def inbound(tmp_path):
    harness = Harness(tmp_path / "ui.sqlite3")
    yield harness
    harness.store.close()


@pytest.mark.asyncio
async def test_single_use_immediate_edit_then_one_turn_and_durable_transition(inbound):
    await inbound.present()
    async def handle(event):
        assert inbound.order == ["edit"]
        assert inbound.store.get_by_event(inbound.card["event_id"]).status == "submitted"
        inbound.order.append("turn")
        inbound.turns.append(event)
    inbound.adapter.handle_message.side_effect = handle
    await inbound.act()
    assert inbound.order == ["edit", "turn"]
    edit = inbound.sent[0][1]
    assert edit["m.relates_to"] == {"rel_type": "m.replace", "event_id": inbound.card["event_id"]}
    assert UI not in edit
    assert edit["m.new_content"][UI]["revision"] == 1
    assert edit["m.new_content"][UI]["status"] == "submitted"
    assert edit["m.new_content"][UI]["status_text"] == t("platform.matrix.interactive_ui.submitted_status")
    turn = inbound.turns[0]
    assert turn.source.thread_id == ROOT and turn.reply_to_message_id == inbound.card["event_id"]
    assert turn.source._transport_adapter_ref() is inbound.adapter
    assert turn.user_id == ALICE
    assert "Pick plan" in turn.text and '"plan"' in turn.text and '"plus"' in turn.text
    assert "CLIENT BODY" not in turn.text


@pytest.mark.asyncio
async def test_repeatable_no_edit_and_nonce_event_duplicates_silent_after_revision(inbound):
    await inbound.present(single_use=False)
    await inbound.act()
    assert inbound.order == ["turn"]
    inbound.reset()
    # A duplicate nonce stays silent even when its other fields are now invalid.
    await inbound.act(event_id="$retry", action={"revision": 9, "action_id": "unknown"})
    await inbound.act(event_id="$action", nonce="b" * 22)
    assert inbound.order == []
    await inbound.act(event_id="$next", nonce="c" * 22)
    assert inbound.order == ["turn"]


@pytest.mark.asyncio
async def test_single_use_duplicate_before_stale_and_inactive(inbound):
    await inbound.present()
    await inbound.act()
    inbound.reset()
    await inbound.act(event_id="$retry")
    assert not inbound.sent and not inbound.turns
    await inbound.act(event_id="$stale", nonce="b" * 22)
    inbound.notice("stale")
    inbound.reset()
    await inbound.act(event_id="$inactive", nonce="c" * 22, action={"revision": 1})
    inbound.notice("inactive")


@pytest.mark.asyncio
async def test_concurrent_single_use_actions_dispatch_once(inbound):
    await inbound.present()
    await asyncio.gather(inbound.act(event_id="$one"), inbound.act(event_id="$two", nonce="b" * 22))
    assert len(inbound.turns) == 1
    assert inbound.order[0:2] == ["edit", "turn"]
    assert sum("m.new_content" in content for _, content in inbound.sent) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("override", [
    {"version": True}, {"version": 2}, {"revision": -1}, {"revision": True}, {"revision": 0.5},
    {"nonce": "short"}, {"nonce": "+" * 22}, {"ui_event_id": "not-an-event"},
    {"values": {"plan": "forged"}}, {"values": {}}, {"values": {"plan": "plus", "extra": True}},
    {"values": {"plan": True}}, {"action_id": "terminal"}, {"ui_id": "spoofed"}, {"values": []},
])
async def test_invalid_action_values_and_unknown_sources(inbound, override):
    await inbound.present()
    await inbound.act(action=override)
    inbound.notice("invalid")


@pytest.mark.asyncio
@pytest.mark.parametrize("guard", ["unauthorized", "live_revoked", "bridge", "ignore", "self", "room", "startup", "edit", "mention"])
async def test_guards_silent_and_action_never_consumes_secret(inbound, guard):
    await inbound.present()
    adapter = inbound.adapter
    adapter._try_secret_capture = AsyncMock(return_value=True)
    event = inbound.event()
    changes = {
        "unauthorized": lambda: setattr(event, "sender", "@stranger:example.org"),
        "live_revoked": lambda: setattr(adapter, "_authorization_check", lambda *args, **kw: False),
        "bridge": lambda: setattr(event, "sender", "@telegram_123:example.org"),
        "ignore": lambda: setattr(adapter, "_ignored_user_patterns", [re.compile("alice")]),
        "self": lambda: setattr(event, "sender", BOT),
        "room": lambda: setattr(adapter, "_allowed_rooms", {"!other:example.org"}),
        "startup": lambda: setattr(event, "timestamp", 1),
        "edit": lambda: event.content.update({"m.relates_to": {"rel_type": "m.replace", "event_id": "$edit"}}),
        "mention": lambda: event.content.update({"m.mentions": {}, "m.relates_to": {}}),
    }
    changes[guard]()
    await adapter._on_room_message(event)
    assert not inbound.order
    adapter._try_secret_capture.assert_not_awaited()


@pytest.mark.asyncio
async def test_authorized_action_skips_secret_and_edit_failure_retries_before_turn(inbound):
    await inbound.present()
    inbound.adapter._try_secret_capture = AsyncMock(return_value=True)
    original_send = inbound.adapter._client.send_message_event.side_effect
    attempts = []
    async def send(*args):
        attempts.append(1)
        if len(attempts) == 1:
            raise OSError("transport unavailable")
        return await original_send(*args)
    inbound.adapter._client.send_message_event.side_effect = send
    await inbound.act()
    assert len(attempts) == 2 and inbound.order == ["edit", "turn"]
    inbound.adapter._try_secret_capture.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_record_at_atomic_accept_is_never_a_turn(inbound, monkeypatch):
    await inbound.present()
    from mautrix.errors import MNotFound
    inbound.adapter._client.get_event.side_effect = MNotFound("missing")
    def missing(*args, **kwargs):
        inbound.store._db.execute("DELETE FROM uis")
        raise KeyError(inbound.card["event_id"])
    monkeypatch.setattr(inbound.store, "accept_action", missing)
    await inbound.act()
    inbound.notice("unknown")


@pytest.mark.asyncio
async def test_unexpected_store_fault_is_not_hidden(inbound, monkeypatch):
    await inbound.present()
    def broken(*args, **kwargs):
        raise RuntimeError("disk fault")
    monkeypatch.setattr(inbound.store, "accept_action", broken)
    with pytest.raises(RuntimeError, match="disk fault"):
        await inbound.act()
    assert not inbound.order


@pytest.mark.asyncio
async def test_real_pending_secret_capture_survives_action(inbound):
    from tools import secret_gateway
    from hermes_constants import get_hermes_home
    secret_gateway.clear()
    try:
        pending = secret_gateway.register(
            env_var="FIXTURE_UI_KEY", prompt="Fixture key", skill_name="fixture",
            destination_home=str(get_hermes_home()),
            handler=lambda *args: pytest.fail("An action must not consume a secret capture"))
        await inbound.present()
        await inbound.act()
        assert len(inbound.turns) == 1
        assert secret_gateway.get_pending(inbound.adapter._capture_home).capture_id == pending.capture_id
    finally:
        secret_gateway.clear()


@pytest.mark.asyncio
@pytest.mark.parametrize("msgtype", ["m.notice", "m.image", "m.file"])
async def test_action_key_on_nontext_never_falls_through_to_ordinary_turn(inbound, msgtype):
    await inbound.present()
    inbound.adapter._process_notices = True
    inbound.adapter._try_secret_capture = AsyncMock(return_value=True)
    event = inbound.event()
    event.content["msgtype"] = msgtype
    await inbound.adapter._on_room_message(event)
    assert not inbound.order
    inbound.adapter._try_secret_capture.assert_not_awaited()


@pytest.mark.asyncio
async def test_failure_of_both_status_edits_still_dispatches_one_turn(inbound, caplog):
    await inbound.present()
    inbound.adapter._client.send_message_event.side_effect = OSError("private transport details")
    await inbound.act()
    assert inbound.adapter._client.send_message_event.await_count == 2
    assert len(inbound.turns) == 1
    assert "status edit failed" in caplog.text and "private transport details" not in caplog.text
    inbound.reset()
    await inbound.act(event_id="$retry")
    assert not inbound.turns


@pytest.mark.asyncio
async def test_form_default_single_use_required_values(inbound):
    from pathlib import Path
    import json
    fixtures = Path(__file__).resolve().parents[5] / "plugins/platforms/matrix/interactive_ui/vendor/element-web-bcb0801/fixtures/valid"
    form = json.loads((fixtures / "form.json").read_text(encoding="utf-8-sig"))
    key = inbound.adapter._source_session_key(inbound.source())
    inbound.card = await inbound.controller.present(chat_id=ROOM, thread_metadata={"thread_id": ROOT},
                                                    session_key=key, ui=form)
    assert inbound.card["ok"]
    inbound.reset()
    await inbound.act(event_id="$empty", action={"action_id": "answer", "values": {
        "name": " ", "quantity": 1, "plan": "plus"}})
    inbound.notice("invalid")
    inbound.reset()
    await inbound.act(action={"action_id": "answer", "values": {"name": "Alice", "quantity": 2, "plan": "plus"}})
    assert inbound.order == ["edit", "turn"]
    assert inbound.store.get_by_event(inbound.card["event_id"]).status == "submitted"


@pytest.mark.asyncio
async def test_durable_action_event_id_duplicate_silent_with_malformed_replacement(inbound):
    from plugins.platforms.matrix.interactive_ui.inbound import handle_action_event
    await inbound.present(single_use=False)
    await inbound.act()
    inbound.reset()
    event = inbound.event()
    event.content[ACTION] = None
    await handle_action_event(inbound.adapter, room_id=ROOM, sender=ALICE, event_id=event.event_id,
                              event_ts=0, content=event.content, relates_to=event.content["m.relates_to"])
    assert not inbound.order


@pytest.mark.asyncio
async def test_cancelled_confirmation_retains_acceptance_without_dispatch(inbound):
    await inbound.present()
    entered = asyncio.Event()
    blocked = asyncio.Event()

    async def send(*args):
        entered.set()
        await blocked.wait()

    inbound.adapter._client.send_message_event.side_effect = send
    task = asyncio.create_task(inbound.act())
    await asyncio.wait_for(entered.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert inbound.store.get_by_event(inbound.card["event_id"]).status == "submitted"
    assert not inbound.turns
    await inbound.act(event_id="$cancelled-retry")
    assert not inbound.order
