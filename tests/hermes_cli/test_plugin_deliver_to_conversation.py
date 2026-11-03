"""Live adapter callbacks use the bound session's existing routing and ownership seam."""

import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import pytest_asyncio

from gateway.config import Platform
from gateway.plugin_messages import bind_live_source
from gateway.session import SessionSource
from gateway.session_context import scoped_session_key
from hermes_cli.plugin_messages import ConversationTarget
from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest


@pytest_asyncio.fixture
async def delivery(tmp_path, monkeypatch):
    from gateway import run

    source = SessionSource(platform=Platform.TELEGRAM, chat_id="42", thread_id="thread")
    adapter = object()
    runner = SimpleNamespace(
        _gateway_loop=asyncio.get_running_loop(), _running=True,
        session_store=SimpleNamespace(lookup_by_session_key=Mock(return_value=None)),
        _session_key_for_source=Mock(return_value="session"),
        _plugin_messaging_session_owned=Mock(return_value=True),
        _delivery_adapter_for=Mock(return_value=adapter),
        _thread_metadata_for_source=Mock(return_value={"thread_id": "thread"}),
        _plugin_local_session=Mock(return_value=object()),
    )
    monkeypatch.setattr(run, "_gateway_runner_ref", lambda: runner)
    ctx = PluginContext(PluginManifest(name="probe", key="probe", source="user"), PluginManager())
    return ctx, runner, source, adapter


async def _call(ctx, source, callback, *, key="session", timeout_s=90.0):
    def invoke():
        with scoped_session_key(key), bind_live_source(source):
            return ctx.deliver_to_conversation(callback, timeout_s=timeout_s)
    return await asyncio.to_thread(invoke)


@pytest.mark.asyncio
async def test_live_route_delivery_adapter_thread_and_context(delivery):
    ctx, runner, source, adapter = delivery
    seen = []

    async def callback(target):
        assert asyncio.get_running_loop() is runner._gateway_loop
        assert scoped_home() == ctx._manager.home_path
        seen.append(target)
        return {"ok": True, "event_id": "event"}

    def scoped_home():
        from hermes_constants import get_hermes_home
        return get_hermes_home()

    assert await _call(ctx, source, callback) == {"ok": True, "event_id": "event"}
    assert seen == [ConversationTarget("telegram", "42", adapter, {"thread_id": "thread"}, "session")]
    runner._delivery_adapter_for.assert_called_once_with(source)
    runner._plugin_messaging_session_owned.assert_called_once_with(
        "session", plugin_home=ctx._manager.home_path, plugin_id=ctx.plugin_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("refusal", ["no_session", "local", "other_profile", "on_loop", "unavailable"])
async def test_refusals_never_call_callback(delivery, refusal):
    ctx, runner, source, _adapter = delivery
    callback = Mock()
    key = "session"
    if refusal == "no_session":
        key = ""
    if refusal == "local":
        source.platform = Platform.LOCAL
    if refusal == "other_profile":
        runner._plugin_messaging_session_owned.return_value = False
    if refusal == "unavailable":
        runner._running = False
    if refusal == "on_loop":
        with scoped_session_key(key):
            result = ctx.deliver_to_conversation(callback)
    else:
        result = await _call(ctx, source, callback, key=key)
    expected = {"no_session": "no_conversation", "local": "unsupported_surface",
                "other_profile": "no_conversation", "on_loop": "on_event_loop",
                "unavailable": "gateway_unavailable"}
    assert result["error"] == expected[refusal]
    callback.assert_not_called()


@pytest.mark.asyncio
async def test_timeout_cancels_callback_before_side_effect(delivery):
    ctx, _runner, source, _adapter = delivery
    cancelled = asyncio.Event()
    effects = []

    async def callback(target):
        try:
            await asyncio.Event().wait()
            effects.append(target)
        finally:
            cancelled.set()

    assert (await _call(ctx, source, callback, timeout_s=0.1))["error"] == "timeout"
    await asyncio.wait_for(cancelled.wait(), 5)
    assert effects == []


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [ValueError, TimeoutError])
async def test_callback_exception_returns_error(delivery, caplog, error_type):
    ctx, _runner, source, _adapter = delivery

    async def callback(target):
        raise error_type("private payload")

    assert await _call(ctx, source, callback) == {"ok": False, "error": "delivery_failed"}
    assert [record.getMessage() for record in caplog.records] == ["plugin conversation delivery failed"]
    assert all(record.exc_info is None for record in caplog.records)
    assert "private payload" not in caplog.text


def test_host_boundary_refuses_live_callback():
    from hermes_cli.plugin_host_child import RemotePluginContext
    from hermes_cli.plugin_host_wire import PluginHostUnsupported

    remote = RemotePluginContext(Mock(), "probe", {"ctx_methods": ["deliver_to_conversation"]})
    with pytest.raises(PluginHostUnsupported, match="live adapter"):
        remote.deliver_to_conversation(Mock())


@pytest.mark.asyncio
async def test_timeout_during_resolution_never_starts_callback(delivery):
    import threading

    ctx, runner, source, _adapter = delivery
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    callback = Mock()

    def lookup(key):
        entered.set()
        assert release.wait(5)
        finished.set()

    runner.session_store.lookup_by_session_key = lookup
    try:
        result = await _call(ctx, source, callback, timeout_s=0.1)
        assert entered.is_set()
        assert result["error"] == "timeout"
    finally:
        release.set()
    assert await asyncio.to_thread(finished.wait, 5)
    callback.assert_not_called()


@pytest.mark.asyncio
async def test_stored_session_wins_over_live_route(delivery):
    ctx, runner, source, adapter = delivery
    stored = SessionSource(platform=Platform.TELEGRAM, chat_id="stored", thread_id="saved-thread")
    runner.session_store.lookup_by_session_key.return_value = SimpleNamespace(origin={"chat_id": "stored"})
    runner._restored_source = Mock(return_value=stored)

    async def callback(target):
        assert target.chat_id == "stored"
        assert target.adapter is adapter
        return {"ok": True}

    assert await _call(ctx, source, callback) == {"ok": True}
    assert runner._delivery_adapter_for.call_args.args[0].chat_id == "stored"
