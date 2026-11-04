"""Producer contracts against a real Matrix adapter and SQLite store."""

import asyncio
from copy import deepcopy
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.matrix.adapter import MatrixAdapter
from plugins.platforms.matrix.interactive_ui import INTERACTIVE_UI_KEY
from plugins.platforms.matrix.interactive_ui.producer import InteractiveUiController
from plugins.platforms.matrix.interactive_ui.protocol import canonical_json_bytes
from plugins.platforms.matrix.interactive_ui.store import InteractiveUiStore

_FIXTURES = Path(__file__).resolve().parents[5] / "plugins/platforms/matrix/interactive_ui/vendor/element-web-bcb0801/fixtures/valid"


def knowledge_map():
    return json.loads((_FIXTURES / "knowledge-map.en.json").read_text(encoding="utf-8-sig"))


@pytest.fixture
def producer(tmp_path, request):
    adapter = MatrixAdapter(PlatformConfig(enabled=True, extra={
        "homeserver": "https://example.org", "user_id": "@bot:example.org",
        "e2ee_mode": getattr(request, "param", "optional")}))
    from types import SimpleNamespace
    from mautrix.client.state_store import MemoryStateStore
    state_store = MemoryStateStore()
    state_store.encryption["!room:example.org"] = None
    adapter._client = SimpleNamespace(
        send_message_event=AsyncMock(side_effect=[f"$event{i}" for i in range(20)]),
        state_store=state_store, crypto=object())
    store = InteractiveUiStore(tmp_path / "interactive_ui.sqlite3")
    controller = InteractiveUiController(adapter, store)
    adapter._interactive_ui_controller = controller
    adapter._interactive_ui_store = store
    yield controller
    store.close()


async def present(controller, ui=None, **kwargs):
    return await controller.present(chat_id=kwargs.pop("chat_id", "!room:example.org"),
                                    thread_metadata=kwargs.pop("thread_metadata", {"thread_id": "$root"}),
                                    session_key=kwargs.pop("session_key", "session"),
                                    ui=knowledge_map() if ui is None else ui, **kwargs)


@pytest.mark.asyncio
async def test_map_one_complete_event_with_server_metadata_and_record(producer):
    ui = knowledge_map()
    result = await present(producer, ui)
    assert result["ok"] and result["revision"] == 0 and result["status"] == "active"
    assert result["ui_id"].startswith("ui_") and result["ui_id"] != ui["ui_id"]
    assert result["ignored_fields"] == ["version", "ui_id", "revision", "status"]
    send = producer.adapter._client.send_message_event
    send.assert_awaited_once()
    room, event_type, content = send.await_args.args
    assert room == "!room:example.org" and str(event_type) == "m.room.message"
    assert set(content) == {"msgtype", "body", "format", "formatted_body", "m.mentions", "m.relates_to", INTERACTIVE_UI_KEY}
    assert content["msgtype"] == "m.text" and content["m.mentions"] == {}
    assert content["format"] == "org.matrix.custom.html"
    assert content["m.relates_to"] == {"rel_type": "m.thread", "event_id": "$root", "is_falling_back": True,
                                         "m.in_reply_to": {"event_id": "$root"}}
    for text in ("Maillard reaction", "Heat creates flavour", "Sugar", "Explain this mechanism", "Sugar → Maillard reaction"):
        assert text in content["body"]
    assert result["content_bytes"] == len(canonical_json_bytes(content))
    record = producer.store.get_by_event(result["event_id"])
    assert record.payload == content[INTERACTIVE_UI_KEY]
    assert (record.session_key, record.thread_id, record.bot_mxid) == ("session", "$root", "@bot:example.org")
    assert ui == knowledge_map()


@pytest.mark.asyncio
async def test_revision_send_before_commit_and_failed_send_retains_revision(producer):
    first = await present(producer)
    original_send = producer.adapter._client.send_message_event

    async def send(room, event_type, content):
        assert producer.lock.locked()
        assert producer.store.get_by_ui_id(first["ui_id"]).revision == 0
        assert INTERACTIVE_UI_KEY not in content
        assert content["body"] == "* New summary"
        assert content["m.relates_to"] == {"rel_type": "m.replace", "event_id": first["event_id"]}
        assert "m.relates_to" not in content["m.new_content"]
        assert content["m.new_content"][INTERACTIVE_UI_KEY]["revision"] == 1
        return "$edit"

    producer.adapter._client.send_message_event = AsyncMock(side_effect=send)
    result = await present(producer, replace_ui_id=first["ui_id"], summary="New summary", status="expired")
    assert result["ok"] and result["revision"] == 1 and result["status"] == "expired"
    producer.adapter._client.send_message_event.side_effect = RuntimeError("secret token and private payload")
    result = await present(producer, replace_ui_id=first["ui_id"])
    assert result == {"ok": False, "error": "send_failed: transport"}
    assert producer.store.get_by_ui_id(first["ui_id"]).revision == 1
    original_send.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("override", [{"session_key": "other"}, {"chat_id": "!other:example.org"},
                                      {"thread_metadata": {"thread_id": "$other"}}])
async def test_other_conversation_refused(producer, override):
    first = await present(producer)
    result = await present(producer, replace_ui_id=first["ui_id"], **override)
    assert result["error"] == "ui_in_other_conversation"
    assert producer.adapter._client.send_message_event.await_count == 1


@pytest.mark.asyncio
async def test_different_bot_refused(producer):
    first = await present(producer)
    producer.adapter._user_id = "@other:example.org"
    assert (await present(producer, replace_ui_id=first["ui_id"]))["error"] == "ui_in_other_conversation"
    assert producer.adapter._client.send_message_event.await_count == 1


@pytest.mark.asyncio
async def test_concurrent_replacements_are_monotonic(producer):
    first = await present(producer)
    results = await asyncio.gather(*(present(producer, replace_ui_id=first["ui_id"]) for _ in range(2)))
    assert [r["revision"] for r in results] == [1, 2]
    assert producer.store.get_by_ui_id(first["ui_id"]).revision == 2


@pytest.mark.asyncio
async def test_failed_cas_is_reported(producer, monkeypatch):
    first = await present(producer)
    monkeypatch.setattr(producer.store, "commit_revision", lambda *a, **kw: False)
    result = await present(producer, replace_ui_id=first["ui_id"])
    assert result == {"ok": False, "error": "revision_conflict"}
    assert producer.store.get_by_ui_id(first["ui_id"]).revision == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["active", "expired", "superseded", "submitted"])
async def test_status_requires_replacement(producer, status):
    result = await present(producer, status=status)
    assert "status: bad_value" in result["error"]
    producer.adapter._client.send_message_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_submitted_is_server_owned(producer):
    first = await present(producer)
    assert "status: bad_value" in (await present(producer, replace_ui_id=first["ui_id"], status="submitted"))["error"]
    assert producer.store.get_by_ui_id(first["ui_id"]).revision == 0


@pytest.mark.asyncio
async def test_invalid_reference_and_generated_node_have_path_errors(producer):
    ui = knowledge_map()
    ui["root"] = "missing"
    result = await present(producer, ui)
    assert "root: unknown_ref:" in result["error"]
    ui = knowledge_map()
    ui["nodes"][1] = {"id": "heading", "type": "javascript", "fallback": "no"}
    assert "nodes[1].type: bad_type:" in (await present(producer, ui))["error"]
    producer.adapter._client.send_message_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_sanitization_and_missing_markdown_literal_fidelity(producer, missing_markdown):
    ui = {"state": {}, "root": "r", "nodes": [{"id": "r", "type": "rich_text", "text": "*literal* & <literal>",
                                   "html": '<script>secret()</script><b onclick="bad()">safe</b>'}]}
    result = await present(producer, ui)
    assert result["ok"]
    content = producer.adapter._client.send_message_event.await_args.args[2]
    assert content[INTERACTIVE_UI_KEY]["nodes"][0]["html"] == "<b>safe</b>"
    assert "*literal* &amp; &lt;literal&gt;" in content["formatted_body"]
    assert "script" not in canonical_json_bytes(content).decode()


@pytest.mark.asyncio
async def test_complete_budget_refuses_without_truncation(producer):
    ui = {"state": {}, "root": "r", "nodes": [
        {"id": "r", "type": "stack", "direction": "vertical", "children": ["a", "b", "c"]},
        *[{"id": name, "type": "text", "text": "<&" * 2000} for name in ("a", "b", "c")]]}
    result = await present(producer, ui)
    assert "too_large" in result["error"]
    producer.adapter._client.send_message_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_unknown_id_and_payload_limit_no_send(producer):
    assert (await present(producer, replace_ui_id="absent"))["error"] == "unknown_ui_id"
    ui = deepcopy(knowledge_map())
    ui["ignored_large_property"] = "x" * 40000
    assert "too_large" in (await present(producer, ui))["error"]
    producer.adapter._client.send_message_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_disconnect_waits_for_send_then_closes_and_stale_controller_refuses(producer):
    adapter = producer.adapter
    entered, release = asyncio.Event(), asyncio.Event()

    async def send(*args):
        entered.set()
        await release.wait()
        return "$sent"

    adapter._client.send_message_event.side_effect = send
    sending = asyncio.create_task(present(producer))
    await asyncio.wait_for(entered.wait(), 5)
    closing = asyncio.create_task(adapter.disconnect())
    release.set()
    assert (await sending)["ok"]
    await closing
    await adapter.disconnect()
    assert adapter._interactive_ui_controller is None and adapter._interactive_ui_store is None
    assert await present(producer) == {"ok": False, "error": "send_failed: disconnected"}


@pytest.mark.asyncio
async def test_connect_pins_store_prunes_and_disconnect_is_idempotent(producer, monkeypatch, tmp_path):
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    adapter = producer.adapter
    await adapter.disconnect()
    adapter._connect_authenticate = AsyncMock(return_value=True)
    adapter._connect_initial_sync = AsyncMock()
    adapter._encryption = False
    adapter._sync_loop = AsyncMock()
    adapter._wire_plugin_handlers = lambda client: None
    calls = []
    original_prune = InteractiveUiStore.prune

    def prune(store, **kwargs):
        calls.append(store._path)
        return original_prune(store, **kwargs)

    monkeypatch.setattr(InteractiveUiStore, "prune", prune)
    token = set_hermes_home_override(str(tmp_path / "owning-profile"))
    try:
        assert await adapter.connect()
    finally:
        reset_hermes_home_override(token)
    controller = adapter._interactive_ui_controller
    assert calls == [adapter._store_dir / "interactive_ui.sqlite3"]
    assert controller.store._path.is_relative_to(tmp_path / "owning-profile")
    await adapter.disconnect()
    await adapter.disconnect()
