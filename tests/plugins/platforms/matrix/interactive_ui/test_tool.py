"""Real plugin registration/dispatch and the scoped gateway delivery callback."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import pytest_asyncio

from agent.delegation_context import delegated_child_context
from gateway.config import Platform
from gateway.plugin_messages import bind_live_source
from gateway.session import SessionSource
from gateway.session_context import scoped_session_key
from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest
from plugins.platforms.matrix.adapter import register
from tools.registry import registry
from .test_producer import knowledge_map
from . import test_producer

producer = pytest.fixture(test_producer.producer.__wrapped__)


@pytest_asyncio.fixture
async def registered(producer, monkeypatch):
    from gateway import run
    from plugins.platforms.matrix import adapter

    monkeypatch.setattr(adapter, "matrix_deps_present", lambda: True)
    source = SessionSource(platform=Platform("matrix"), chat_id="!room:example.org", thread_id="$root")
    runner = SimpleNamespace(
        _gateway_loop=asyncio.get_running_loop(), _running=True,
        session_store=SimpleNamespace(lookup_by_session_key=Mock(return_value=None)),
        _session_key_for_source=Mock(return_value="session"),
        _plugin_messaging_session_owned=Mock(return_value=True),
        _delivery_adapter_for=Mock(return_value=producer.adapter),
        _thread_metadata_for_source=Mock(return_value={"thread_id": "$root"}),
        _plugin_local_session=Mock(return_value=object()),
    )
    monkeypatch.setattr(run, "_gateway_runner_ref", lambda: runner)
    from hermes_cli import plugins
    manager = PluginManager()
    monkeypatch.setattr(plugins, "get_plugin_manager", lambda: manager)
    ctx = PluginContext(PluginManifest(name="matrix", key="matrix", source="bundled"), manager)
    register(ctx)
    yield ctx, runner, source, producer
    manager.unload()


async def dispatch(registered, args, *, bound=True, child=False):
    _ctx, _runner, source, _producer = registered

    def invoke():
        with scoped_session_key("session" if bound else ""), bind_live_source(source):
            if child:
                with delegated_child_context("child"):
                    return registry.dispatch("present_ui", args)
            return registry.dispatch("present_ui", args)

    return json.loads(await asyncio.to_thread(invoke))


@pytest.mark.asyncio
async def test_registry_valid_map_sends_and_stores(registered):
    result = await dispatch(registered, {"ui": knowledge_map()})
    assert result["ok"] and result["revision"] == 0
    producer = registered[3]
    producer.adapter._client.send_message_event.assert_awaited_once()
    assert producer.store.get_by_event(result["event_id"]).ui_id == result["ui_id"]
    registered[1]._plugin_messaging_session_owned.assert_called_once()


@pytest.mark.asyncio
async def test_registry_path_error_no_send(registered):
    ui = knowledge_map()
    ui["nodes"][1].pop("text")
    result = await dispatch(registered, {"ui": ui})
    assert result["error"].startswith("interactive_ui: nodes[1].text: missing_field:")
    registered[3].adapter._client.send_message_event.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("refusal", ["unbound", "child", "process_child", "telegram_all", "local_all", "wrong_adapter", "disconnected", "other_profile"])
async def test_actual_dispatch_refuses_unsupported_context_even_all_override(registered, refusal, monkeypatch):
    from model_tools import get_tool_definitions

    _ctx, runner, source, producer = registered
    # all retains its existing explicit override: the visible schema must still refuse at runtime.
    assert "present_ui" in {tool["function"]["name"] for tool in get_tool_definitions(enabled_toolsets=["all"], skip_tool_search_assembly=True)}
    if refusal == "telegram_all":
        source.platform = Platform.TELEGRAM
    if refusal == "local_all":
        source.platform = Platform.LOCAL
    if refusal == "wrong_adapter":
        runner._delivery_adapter_for.return_value = object()
    if refusal == "disconnected":
        producer.adapter._interactive_ui_controller = None
    if refusal == "other_profile":
        runner._plugin_messaging_session_owned.return_value = False
    if refusal == "process_child":
        monkeypatch.setenv("HERMES_DELEGATED_CHILD_CONTEXT", "1")
    result = await dispatch(registered, {"ui": knowledge_map()}, bound=refusal != "unbound", child=refusal == "child")
    assert "Matrix conversations only" in result["error"]
    if refusal in ("child", "process_child"):
        assert "delegated_child" in result["error"]
        runner._delivery_adapter_for.assert_not_called()
    producer.adapter._client.send_message_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_normal_disabled_and_named_platform_gating_and_session_cache(registered):
    from hermes_cli.tools_config import _get_platform_tools
    from model_tools import get_tool_definitions

    config = {"platform_toolsets": {"matrix": []}}
    selected = _get_platform_tools(config, "matrix")
    assert "interactive_ui" not in selected
    config["platform_toolsets"]["matrix"].append("interactive_ui")
    assert "interactive_ui" not in selected
    enabled = _get_platform_tools(config, "matrix")
    assert "interactive_ui" in enabled
    assert "present_ui" in {tool["function"]["name"] for tool in get_tool_definitions(enabled_toolsets=list(enabled), skip_tool_search_assembly=True)}
    for platform in ("telegram", "cli", "local", "desktop", "tui"):
        selected = _get_platform_tools({"platform_toolsets": {platform: ["interactive_ui"]}}, platform)
        assert "interactive_ui" not in selected
        assert "present_ui" not in {tool["function"]["name"] for tool in get_tool_definitions(enabled_toolsets=list(selected), skip_tool_search_assembly=True)}
