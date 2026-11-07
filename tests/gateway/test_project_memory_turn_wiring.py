"""Project memory targets and tool surfaces follow immutable messaging project pins."""
from dataclasses import replace
from unittest.mock import patch

import pytest

from agent.project_memory.provider import PROJECT_MEMORY_RECALL_TOOL, ProjectMemoryProvider, ProjectMemoryTarget
from gateway.config import Platform
from gateway.project_routing import UNBOUND_PIN
from gateway.project_routing_turn import apply_turn_project_pin, project_memory_target_for_pin, project_pin_for_key
from gateway.run import GatewayRunner
from gateway.run_turn_runner import TurnRunner
from gateway.turn_context import TurnContext
from tests.gateway import test_project_turn_wiring

routing = test_project_turn_wiring.routing


def test_signature_includes_project_and_service_config():
    signature = GatewayRunner._agent_config_signature
    assert signature("m", {}, [], "", project_memory_id="a") != signature("m", {}, [], "", project_memory_id="b")
    extract = GatewayRunner._extract_cache_busting_config
    a = extract({"project_memory": {"hindsight_url": "http://one"}})
    b = extract({"project_memory": {"hindsight_url": "http://two"}})
    assert signature("m", {}, [], "", cache_keys=a) != signature("m", {}, [], "", cache_keys=b)


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["bound", "unbound", "local"])
async def test_resolve_turn_agent_passes_project_target_or_none(routing, state):
    from tests.gateway.test_local_viewer_events_submit import _RecordingAgent, _gateway_runner
    r = routing
    await apply_turn_project_pin(r.runner, r.source, r.entry, [])
    if state == "unbound":
        r.store.set_session_project_pin(r.entry.session_key, UNBOUND_PIN.to_dict(), expected_session_id=r.entry.session_id,
                                        clear_terminal_cwd=False)
    source = replace(r.source, platform=Platform.LOCAL) if state == "local" else r.source
    runner = _gateway_runner()
    runner.session_store = r.store
    runner._resolve_profile_home_for_source = r.runner._resolve_profile_home_for_source
    runner._agent_config_signature.side_effect = GatewayRunner._agent_config_signature
    turn = TurnRunner(runner, TurnContext(source=source, session_key=r.entry.session_key,
                                         session_id=r.entry.session_id, user_config={}, AIAgent=_RecordingAgent))
    _, reused = turn._resolve_turn_agent({"model": "m", "runtime": {}}, "matrix", "", 2, None, {})
    target = _RecordingAgent.built[-1]["project_memory"]
    assert not reused
    if state == "bound":
        assert target.project_id == r.project and target.projects_db == r.runtime / "projects.db"
        assert target == project_memory_target_for_pin(r.runner, r.source, project_pin_for_key(r.runner, r.entry.session_key))
    else:
        assert target is None
    assert runner._agent_config_signature.call_args.kwargs["project_memory_id"] == (r.project if state == "bound" else "")


@pytest.mark.parametrize("service,memory_disabled,skip_memory", [
    (True, False, False), (False, False, False), (True, True, False), (True, False, True),
])
def test_real_agent_attaches_project_without_shared_provider(tmp_path, service, memory_disabled, skip_memory):
    from run_agent import AIAgent
    cfg = {"memory": {}, "agent": {}, "project_memory": {"hindsight_url": "http://127.0.0.1:9" if service else ""}}
    target = ProjectMemoryTarget("project", "alpha", "Alpha", tmp_path / "projects.db")
    with (
        patch("hermes_cli.config.load_config", return_value=cfg),
        patch("hermes_cli.config.load_config_readonly", return_value=cfg),
        patch("agent.model_metadata.get_model_context_length", return_value=204800),
        patch("model_tools.get_tool_definitions", return_value=[]),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI"),
    ):
        agent = AIAgent(api_key="test-key", base_url="https://openrouter.ai/api/v1", quiet_mode=True,
                        skip_context_files=True, skip_memory=skip_memory, session_id="test",
                        platform="matrix", enabled_toolsets=["memory"],
                        disabled_toolsets=["memory"] if memory_disabled else [], project_memory=target)
    try:
        has_provider = service and not skip_memory
        assert bool(agent._memory_manager) == has_provider
        if has_provider:
            assert any(isinstance(p, ProjectMemoryProvider) for p in agent._memory_manager.providers)
        tool_names = {tool["function"]["name"] for tool in agent.tools}
        assert (PROJECT_MEMORY_RECALL_TOOL in tool_names) == (has_provider and not memory_disabled)
    finally:
        agent.close()


@pytest.mark.asyncio
async def test_background_agent_receives_same_project_target(routing, monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock
    r = routing
    await apply_turn_project_pin(r.runner, r.source, r.entry, [])
    runner = object.__new__(GatewayRunner)
    runner.config, runner.session_store = r.runner.config, r.store
    runner._profile_scope_for_source = r.runner._profile_scope_for_source
    runner._resolve_profile_home_for_source = r.runner._resolve_profile_home_for_source
    runner._session_key_for_source = lambda source: r.entry.session_key
    adapter = SimpleNamespace(send=AsyncMock(), extract_media=lambda text: ([], text),
                              extract_images=lambda text: ([], text))
    runner._delivery_adapter_for = lambda source: adapter
    runner._thread_metadata_for_source = lambda *args: {}
    runner._resolve_session_agent_runtime = lambda **kw: ("m", {"api_key": "key"})
    runner._resolve_turn_toolsets = lambda *args: ([], [])
    runner._provider_routing = {}
    runner._resolve_session_reasoning_config = lambda **kw: None
    runner._resolve_session_service_tier = lambda **kw: None
    runner._resolve_turn_agent_config = lambda *args: {"model": "m", "runtime": {}}
    runner._refresh_fallback_model = lambda: None
    runner._session_db = None
    runner._run_in_executor_with_context = asyncio.to_thread
    runner._cleanup_agent_resources = MagicMock()
    monkeypatch.setattr("gateway.run._load_gateway_config", dict)
    factory = MagicMock(return_value=SimpleNamespace(run_conversation=lambda **kw: {"final_response": "done"}))
    monkeypatch.setattr("run_agent.AIAgent", factory)
    await runner._run_background_task("inspect", r.source, "bg-task")
    factory.assert_called_once()
    target = factory.call_args.kwargs["project_memory"]
    assert target.project_id == r.project and target.projects_db == r.runtime / "projects.db"
    adapter.send.assert_awaited_once()
