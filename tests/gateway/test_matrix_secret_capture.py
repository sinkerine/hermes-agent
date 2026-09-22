"""Matrix structured secret capture stays outside normal event dispatch."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from concurrent.futures import Future
from contextlib import nullcontext
import threading

import pytest

from tools import secret_gateway


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
        destination_home="/tmp/fixture-profile",
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


def _completed_future(coro):
    future = Future()
    future.set_result(__import__("asyncio").run(coro))
    return future
