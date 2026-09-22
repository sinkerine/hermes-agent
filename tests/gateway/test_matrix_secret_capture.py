"""Matrix structured secret capture stays outside normal event dispatch."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from concurrent.futures import Future
from contextlib import nullcontext
import threading

import pytest

from tools import secret_gateway
from hermes_constants import get_hermes_home


@pytest.fixture
def matrix_adapter(monkeypatch):
    from plugins.platforms.matrix.adapter import MatrixAdapter
    from gateway.config import PlatformConfig

    monkeypatch.setenv("MATRIX_REQUIRE_MENTION", "false")
    adapter = MatrixAdapter(PlatformConfig(enabled=True, token="fixture-token", extra={
        "homeserver": "https://matrix.example.org", "user_id": "@bot:example.org"}))
    adapter._text_batch_delay_seconds = 0
    adapter._client = None
    adapter._resolve_room_identity = AsyncMock(return_value=SimpleNamespace(
        display_name="fixture", room_topic=None, server_name="example.org", chat_type="dm"))
    adapter.handle_message = AsyncMock()
    adapter.redact_message = AsyncMock(return_value=False)
    return adapter


@pytest.fixture(autouse=True)
def clear_capture():
    secret_gateway.clear()
    yield
    secret_gateway.clear()


@pytest.mark.anyio
async def test_pending_value_consumed_before_message_event(matrix_adapter):
    seen = []
    entry = secret_gateway.register(
        env_var="FIXTURE_CAPTURE_KEY", prompt="Enter fixture key", skill_name="fixture",
        destination_home=str(get_hermes_home()),
        handler=lambda value, redacted: (
            seen.append((value, redacted)) or secret_gateway.SecretCaptureResult(True, "FIXTURE_CAPTURE_KEY")))
    await matrix_adapter._handle_text_message(
        "!room:example.org", "@alice:example.org", "$secret", 0,
        {"body": "fixture-secret-value", "msgtype": "m.text"}, {})
    assert seen == [("fixture-secret-value", False)]
    matrix_adapter.handle_message.assert_not_awaited()
    assert matrix_adapter.redact_message.await_count == 1


@pytest.mark.anyio
async def test_without_pending_capture_normal_message_dispatches(matrix_adapter):
    await matrix_adapter._handle_text_message(
        "!room:example.org", "@alice:example.org", "$normal", 0,
        {"body": "ordinary fixture text", "msgtype": "m.text"}, {})
    matrix_adapter.handle_message.assert_awaited_once()


@pytest.mark.anyio
async def test_without_pending_capture_keeps_room_authorization(matrix_adapter, monkeypatch):
    monkeypatch.setattr(matrix_adapter, "_is_allowed_matrix_room_event", AsyncMock(return_value=False))
    event = SimpleNamespace(
        room_id="!blocked:example.org", sender="@alice:example.org", event_id="$blocked",
        content={"body": "ordinary fixture text", "msgtype": "m.text"},
    )
    await matrix_adapter._on_room_message(event)
    matrix_adapter.handle_message.assert_not_awaited()


@pytest.mark.anyio
async def test_pending_capture_precedes_room_and_mention_authorization(matrix_adapter, monkeypatch):
    seen = []
    entry = secret_gateway.register(
        env_var="FIXTURE_CAPTURE_KEY", prompt="Enter fixture key", skill_name="fixture",
        destination_home=str(get_hermes_home()),
        handler=lambda value, redacted: (
            seen.append(value) or secret_gateway.SecretCaptureResult(True, "FIXTURE_CAPTURE_KEY")),
    )
    matrix_adapter._ignored_user_patterns = [__import__("re").compile("alice")]
    matrix_adapter._allowed_rooms = {"!other:example.org"}
    matrix_adapter._allowed_room_ids = set(matrix_adapter._allowed_rooms)
    matrix_adapter._require_mention = True
    monkeypatch.setattr(matrix_adapter, "_is_allowed_matrix_room_event", AsyncMock(return_value=False))
    event = SimpleNamespace(
        room_id="!room:example.org", sender="@alice:example.org", event_id="$event",
        content={"body": "room-secret", "msgtype": "m.text", "m.relates_to": {"rel_type": "m.thread", "event_id": "$root"}},
    )
    await matrix_adapter._on_room_message(event)
    assert seen == ["room-secret"]
    matrix_adapter.handle_message.assert_not_awaited()


@pytest.mark.anyio
async def test_pending_capture_uses_same_path_for_decrypted_encrypted_event(matrix_adapter):
    seen = []
    entry = secret_gateway.register(
        env_var="FIXTURE_CAPTURE_KEY", prompt="Enter fixture key", skill_name="fixture",
        destination_home=str(get_hermes_home()),
        handler=lambda value, redacted: (
            seen.append(value) or secret_gateway.SecretCaptureResult(True, "FIXTURE_CAPTURE_KEY")),
    )
    event = SimpleNamespace(
        room_id="!encrypted:example.org", sender="@alice:example.org", event_id="$encrypted",
        content={"body": "encrypted-secret", "msgtype": "m.text", "m.relates_to": {"rel_type": "m.thread", "event_id": "$root"}},
    )
    await matrix_adapter._on_room_message(event)
    assert seen == ["encrypted-secret"]
    matrix_adapter.handle_message.assert_not_awaited()


def test_turn_callback_registers_prompt_and_returns_no_value(monkeypatch, tmp_path):
    from gateway.run_turn_runner import TurnRunner

    sent = []
    adapter = SimpleNamespace()

    async def send(chat_id, prompt, metadata=None):
        sent.append((chat_id, prompt, metadata))
        return SimpleNamespace(success=True)

    adapter.send = send
    ctx = SimpleNamespace(_status_adapter=adapter, _status_chat_id="!room:example.org",
                          _status_thread_metadata={}, _loop_for_step=None)
    runner = object.__new__(TurnRunner)
    runner._ctx = ctx
    runner._schedule = lambda coro, _message: _completed_future(coro)

    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr("gateway.run._profile_runtime_scope", lambda *_args, **_kwargs: nullcontext())
    monkeypatch.setattr("hermes_cli.config.save_env_value_secure", lambda *_args: {"success": True})
    monkeypatch.setattr("tools.skills_tool.load_env", lambda: {"FIXTURE_CAPTURE_KEY": "present"})

    result = []
    thread = threading.Thread(target=lambda: result.append(
        runner._secret_capture_callback_sync("FIXTURE_CAPTURE_KEY", "Enter fixture key", {"skill_name": "fixture"})))
    thread.start()
    while not secret_gateway.has_pending():
        pass
    entry = secret_gateway.get_pending()
    secret_gateway.resolve_with_value(entry.capture_id, "fixture-secret-value")
    thread.join(timeout=2)
    assert result[0]["success"] is True
    assert "fixture-secret-value" not in repr(result)
    assert sent[0][0] == "!room:example.org"
    assert sent[0][1].startswith("Reply with the Enter fixture key.")
    assert "before model dispatch" in sent[0][1]


def test_matrix_adapter_owner_profile_routes_capture_home(monkeypatch, tmp_path):
    from gateway.config import PlatformConfig
    from plugins.platforms.matrix.adapter import MatrixAdapter

    profile_home = tmp_path / "profiles" / "entertainment"
    monkeypatch.setattr("hermes_cli.profiles.get_profile_dir", lambda name: profile_home)
    adapter = MatrixAdapter(PlatformConfig(enabled=True, token="fixture-token", extra={}))
    adapter.set_owner_profile("entertainment")
    assert adapter._capture_home == str(profile_home)


def _completed_future(coro):
    future = Future()
    future.set_result(__import__("asyncio").run(coro))
    return future
