"""Durable conversation project pins reach messaging agents without changing local workspaces."""
import asyncio
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from threading import Event, get_ident
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.runtime_cwd import resolve_agent_cwd, resolve_context_cwd
from gateway.config import GatewayConfig, Platform
from gateway.project_routing import PIN_METADATA_KEY, ProjectPin, UNBOUND_PIN
from gateway.project_routing_turn import apply_turn_project_pin, project_pin_for_key
from gateway.run import GatewayRunner
from gateway.run_turn_runner import TurnRunner
from gateway.session import SessionSource, SessionStore
from gateway.session_identity import RoutingIdentity
from gateway.turn_context import TurnContext
from hermes_cli import projects_db
from hermes_constants import get_hermes_home, reset_hermes_home_override, set_hermes_home_override
from tools.terminal_scope import reset_terminal_scope, set_terminal_scope, terminal_env


@pytest.fixture
def routing(tmp_path, monkeypatch):
    launch, runtime = tmp_path / "launch", tmp_path / "runtime"
    launch.mkdir()
    runtime.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(launch))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    config = GatewayConfig(multiplex_profiles=True)
    store = SessionStore(launch / "sessions", config)
    store._profile_home_cache["runtime"] = runtime
    source = SessionSource(platform=Platform.MATRIX, chat_id="!room:test", thread_id="$thread", profile="runtime")
    source._identity = RoutingIdentity(
        transport_profile="receiving", runtime_profile="runtime", authorization_home=launch, runtime_home=runtime,
    )
    scopes = []

    @contextmanager
    def profile_scope(event):
        scopes.append(get_ident())
        home_token = set_hermes_home_override(event._identity.runtime_home)
        terminal_token = set_terminal_scope({"TERMINAL_ENV": "local", "TERMINAL_CWD": str(runtime)})
        try:
            yield
        finally:
            reset_terminal_scope(terminal_token)
            reset_hermes_home_override(home_token)

    runner = SimpleNamespace(
        config=config, session_store=store, _primary_profile_name="default",
        _resolve_profile_home_for_source=lambda event: event._identity.runtime_home,
        _profile_scope_for_source=profile_scope,
    )
    folder = tmp_path / "alpha"
    folder.mkdir()
    with projects_db.connect_closing(db_path=runtime / "projects.db") as conn:
        project = projects_db.create_project(conn, name="Alpha", primary_path=str(folder))
        projects_db.set_chat_binding(conn, platform="matrix", transport_profile="receiving",
                                     chat_id=source.chat_id, project_id=project)
    entry = store.get_or_create_session(source)
    yield SimpleNamespace(runner=runner, store=store, source=source, entry=entry, project=project,
                          folder=folder, launch=launch, runtime=runtime, scopes=scopes)
    store.close_all_db_handles()


@pytest.mark.asyncio
async def test_first_bound_pin_stamps_own_database_off_loop_and_survives_reload(routing):
    r = routing
    assert await apply_turn_project_pin(r.runner, r.source, r.entry, []) is None
    pin = project_pin_for_key(r.runner, r.entry.session_key)
    assert pin.project_id == r.project and pin.cwd == str(r.folder) and pin.profile == "runtime"
    assert r.scopes and get_ident() not in r.scopes
    assert r.store._db_for_key(r.entry.session_key).get_session(r.entry.session_id)["cwd"] == str(r.folder)
    assert r.store._routing_db.get_session(r.entry.session_id) is None
    assert get_hermes_home() == r.launch
    reloaded = SessionStore(r.launch / "sessions", r.runner.config)
    reloaded._profile_home_cache["runtime"] = r.runtime
    try:
        assert project_pin_for_key(SimpleNamespace(session_store=reloaded), r.entry.session_key) == pin
    finally:
        reloaded.close_all_db_handles()


@pytest.mark.asyncio
@pytest.mark.parametrize("old_history", [False, True])
async def test_unbound_and_preexisting_history_persist_explicit_unbound(routing, old_history):
    r = routing
    if not old_history:
        with projects_db.connect_closing(db_path=r.runtime / "projects.db") as conn:
            projects_db.delete_chat_binding(conn, platform="matrix", transport_profile="receiving", chat_id=r.source.chat_id)
    history = [{"role": "user", "content": "before binding"}] if old_history else []
    assert await apply_turn_project_pin(r.runner, r.source, r.entry, history) is None
    assert project_pin_for_key(r.runner, r.entry.session_key) == UNBOUND_PIN
    assert not r.store._db_for_key(r.entry.session_key).get_session(r.entry.session_id)["cwd"]
    assert await apply_turn_project_pin(r.runner, r.source, r.entry, []) is None
    assert project_pin_for_key(r.runner, r.entry.session_key) == UNBOUND_PIN


@pytest.mark.asyncio
async def test_rebind_keeps_conversation_pin_and_force_new_resolves_current_binding(routing, tmp_path):
    r = routing
    await apply_turn_project_pin(r.runner, r.source, r.entry, [])
    original = project_pin_for_key(r.runner, r.entry.session_key)
    beta = tmp_path / "beta"
    beta.mkdir()
    with projects_db.connect_closing(db_path=r.runtime / "projects.db") as conn:
        other = projects_db.create_project(conn, name="Beta", primary_path=str(beta))
        projects_db.set_chat_binding(conn, platform="matrix", transport_profile="receiving",
                                     chat_id=r.source.chat_id, project_id=other)
    for history in ([], [{"role": "user", "content": "continuation"}]):
        await apply_turn_project_pin(r.runner, r.source, r.entry, history)
        assert project_pin_for_key(r.runner, r.entry.session_key) == original
    fresh = r.store.get_or_create_session(r.source, force_new=True)
    assert fresh.session_id != r.entry.session_id
    assert project_pin_for_key(r.runner, fresh.session_key) is None
    await apply_turn_project_pin(r.runner, r.source, fresh, [])
    assert project_pin_for_key(r.runner, fresh.session_key).project_id == other
    assert r.store._db_for_key(fresh.session_key).get_session(fresh.session_id)["cwd"] == str(beta)


@pytest.mark.asyncio
async def test_invalid_binding_refuses_without_persisting_and_can_retry(routing):
    r = routing
    with projects_db.connect_closing(db_path=r.runtime / "projects.db") as conn:
        projects_db.archive_project(conn, r.project)
    refusal = await apply_turn_project_pin(r.runner, r.source, r.entry, [])
    assert "archived" in refusal and "gateway.project.error" not in refusal
    assert project_pin_for_key(r.runner, r.entry.session_key) is None
    assert not r.store._db_for_key(r.entry.session_key).get_session(r.entry.session_id)["cwd"]
    with projects_db.connect_closing(db_path=r.runtime / "projects.db") as conn:
        projects_db.restore_project(conn, r.project)
    assert await apply_turn_project_pin(r.runner, r.source, r.entry, []) is None
    assert project_pin_for_key(r.runner, r.entry.session_key).project_id == r.project


@pytest.mark.asyncio
async def test_local_prepare_skips_routing(routing):
    r = routing
    assert await apply_turn_project_pin(r.runner, replace(r.source, platform=Platform.LOCAL), r.entry, []) is None
    assert project_pin_for_key(r.runner, r.entry.session_key) is None
    assert r.scopes == []


def test_signature_includes_workspace():
    signature = GatewayRunner._agent_config_signature
    assert signature("model", {}, [], "", session_cwd="/alpha") != signature("model", {}, [], "", session_cwd="/beta")
    assert signature("model", {}, [], "") == signature("model", {}, [], "", session_cwd="")


@pytest.mark.asyncio
@pytest.mark.parametrize("local", [False, True])
async def test_real_run_sync_observes_workspace_and_restores_scope(routing, monkeypatch, tmp_path, local):
    from gateway.session_policy import build_policy
    r = routing
    await apply_turn_project_pin(r.runner, r.source, r.entry, [])
    desktop = tmp_path / "desktop"
    desktop.mkdir()
    policy = build_policy({"cwd": str(desktop), "model": "m", "provider": "custom"}, {}) if local else None
    monkeypatch.setattr("gateway.session_policy.policy_for_source", lambda runner, source: policy)
    monkeypatch.setattr("gateway.session_authorities.active_authority", lambda runner: None)
    source = replace(r.source, platform=Platform.LOCAL) if local else r.source
    turn = TurnRunner(r.runner, TurnContext(source=source, session_key=r.entry.session_key, session_id=r.entry.session_id))
    expected = desktop if local else r.folder

    def execute():
        assert resolve_agent_cwd() == expected
        assert resolve_context_cwd() == expected
        assert terminal_env("TERMINAL_CWD") == str(expected)
        assert get_hermes_home() == r.runtime
        return {"final_response": "ok"}

    turn._run_sync_scoped = execute

    def worker():
        with r.runner._profile_scope_for_source(r.source):
            before = resolve_agent_cwd(), terminal_env("TERMINAL_CWD")
            assert turn.run_sync()["final_response"] == "ok"
            assert (resolve_agent_cwd(), terminal_env("TERMINAL_CWD")) == before
    await asyncio.to_thread(worker)


@pytest.mark.asyncio
async def test_fresh_agent_receives_pin_cwd_and_signature(routing):
    from tests.gateway.test_local_viewer_events_submit import _RecordingAgent, _gateway_runner
    r = routing
    await apply_turn_project_pin(r.runner, r.source, r.entry, [])
    runner = _gateway_runner()
    runner.session_store = r.store
    runner._agent_config_signature.side_effect = GatewayRunner._agent_config_signature
    ctx = TurnContext(source=r.source, session_key=r.entry.session_key, session_id=r.entry.session_id,
                      user_config={}, AIAgent=_RecordingAgent)
    turn = TurnRunner(runner, ctx)
    agent, reused = turn._resolve_turn_agent({"model": "m", "runtime": {}}, "matrix", "", 2, None, {})
    assert not reused and agent.session_id == r.entry.session_id
    assert _RecordingAgent.built[-1]["cwd"] == str(r.folder)
    assert runner._agent_config_signature.call_args.kwargs["session_cwd"] == str(r.folder)


@pytest.mark.asyncio
@pytest.mark.parametrize("storage_error", [False, True])
async def test_prepare_refusal_restores_environment_and_never_reaches_model(routing, monkeypatch, storage_error):
    from gateway.session_results import execution_result
    r = routing
    await apply_turn_project_pin(r.runner, r.source, r.entry, [])
    original_pin = project_pin_for_key(r.runner, r.entry.session_key)
    with projects_db.connect_closing(db_path=r.runtime / "projects.db") as conn:
        projects_db.archive_project(conn, r.project)
    runner = object.__new__(GatewayRunner)
    runner.config, runner.session_store = r.runner.config, r.store
    runner._profile_scope_for_source = r.runner._profile_scope_for_source
    runner._resolve_profile_home_for_source = r.runner._resolve_profile_home_for_source
    runner._hmwa_open_session = AsyncMock(return_value=(False, False))
    runner._set_session_env = lambda context: ["tokens"]
    runner._clear_session_env = MagicMock()
    runner._pinned_session_context_prompt = lambda *a, **kw: ""
    runner._hmwa_acquire_turn_lease = AsyncMock()
    runner._mark_durable_active_turn = AsyncMock()
    runner._hmwa_first_contact_notes = AsyncMock()
    runner._run_agent = AsyncMock()
    # Real hygiene would construct an agent and compress this eligible transcript.
    history = [{"role": role, "content": "large history " * 100} for role in ("user", "assistant") * 3]
    runner._async_session_store = SimpleNamespace(_store=r.store, load_transcript=AsyncMock(return_value=history))
    runner._session_db = None
    runner._session_has_compression_in_flight = AsyncMock(return_value=False)
    runner._resolve_session_agent_runtime = MagicMock(return_value=("m", {"api_key": "key"}))
    runner._event_thread_metadata = lambda *args: {}
    compressor = MagicMock(return_value=(history, None))
    agent_factory = MagicMock(return_value=SimpleNamespace(_compress_context=compressor))
    runner._hmwa_hygiene_build_agent = AsyncMock(side_effect=lambda *a: (agent_factory(), None))
    monkeypatch.setattr("gateway.run._load_gateway_config", lambda: {"compression": {"hygiene_hard_message_limit": 4}})
    monkeypatch.setattr("agent.model_metadata.get_model_context_length_async", AsyncMock(return_value=100))
    monkeypatch.setattr("gateway.run_turn_prepare.build_session_context", lambda *args: None)
    event = SimpleNamespace(internal=False, text="hi")
    if storage_error:
        monkeypatch.setattr("gateway.project_routing_turn.resolve_turn_pin", MagicMock(side_effect=OSError("unreadable")))
        with pytest.raises(OSError, match="unreadable"):
            await runner._hmwa_prepare_turn(event, r.source, r.entry, r.entry.session_key, r.entry.session_key, 1)
        runner._clear_session_env.assert_called_once_with(["tokens"])
        runner._run_agent.assert_not_awaited()
        assert project_pin_for_key(runner, r.entry.session_key) == original_pin
        return
    captured = {}
    token = execution_result.set(captured)
    try:
        reply, tokens = await runner._hmwa_prepare_turn(event, r.source, r.entry, r.entry.session_key,
                                                       r.entry.session_key, 1)
    finally:
        execution_result.reset(token)
    assert "archived" in reply and tokens == ["tokens"]
    assert captured["result"]["failed"] and captured["result"]["failure_reason"] == "project_route"
    runner._clear_session_env.assert_called_once_with(tokens)
    runner._hmwa_first_contact_notes.assert_not_awaited()
    runner._hmwa_hygiene_build_agent.assert_not_awaited()
    runner._resolve_session_agent_runtime.assert_not_called()
    agent_factory.assert_not_called()
    compressor.assert_not_called()
    runner._run_agent.assert_not_awaited()
    assert project_pin_for_key(runner, r.entry.session_key) == original_pin


def test_cwd_stamping_failure_is_metadata_only(routing, monkeypatch, caplog):
    r = routing
    db = r.store._db_for_key(r.entry.session_key)
    monkeypatch.setattr(db, "update_session_cwd", MagicMock(side_effect=RuntimeError("metadata write")))
    r.store.update_session_row_cwd(r.entry.session_key, r.entry.session_id, str(r.folder))
    assert "Could not stamp session cwd" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["bound", "archived", "reset_during_validation", "reset_before_execution"])
async def test_background_uses_pin_in_worker_or_refuses_before_agent(routing, monkeypatch, state):
    import run_agent
    r = routing
    await apply_turn_project_pin(r.runner, r.source, r.entry, [])
    if state == "archived":
        with projects_db.connect_closing(db_path=r.runtime / "projects.db") as conn:
            projects_db.archive_project(conn, r.project)
    runner = object.__new__(GatewayRunner)
    runner.config, runner.session_store = r.runner.config, r.store
    runner._profile_scope_for_source = r.runner._profile_scope_for_source
    runner._resolve_profile_home_for_source = r.runner._resolve_profile_home_for_source
    runner._session_key_for_source = lambda source: r.entry.session_key
    adapter = SimpleNamespace(send=AsyncMock(), extract_media=lambda text: ([], text),
                              extract_images=lambda text: ([], text))
    runner._delivery_adapter_for = lambda source: adapter
    runner._thread_metadata_for_source = lambda *args: {"thread_id": r.source.thread_id}
    runner._resolve_session_agent_runtime = MagicMock(return_value=("m", {"api_key": "key"}))
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

    class Agent:
        def __init__(self, **kwargs):
            assert kwargs["cwd"] == str(r.folder)
            assert resolve_agent_cwd() == r.folder and get_hermes_home() == r.runtime

        def run_conversation(self, **kwargs):
            assert terminal_env("TERMINAL_CWD") == str(r.folder)
            return {"final_response": "done", "messages": []}

    factory = MagicMock(side_effect=Agent)
    monkeypatch.setattr(run_agent, "AIAgent", factory)
    if state == "reset_during_validation":
        from gateway.project_routing import revalidate_pin

        def reset_after_validation(*args):
            result = revalidate_pin(*args)
            r.store.reset_session(r.entry.session_key)
            return result

        monkeypatch.setattr("gateway.project_routing.revalidate_pin", reset_after_validation)
    elif state == "reset_before_execution":
        async def reset_before_execution(fn):
            r.store.reset_session(r.entry.session_key)
            return await asyncio.to_thread(fn)

        runner._run_in_executor_with_context = reset_before_execution
    await runner._run_background_task("inspect", r.source, "bg-task")
    if state.startswith("reset"):
        factory.assert_not_called()
        adapter.send.assert_not_awaited()
        assert project_pin_for_key(runner, r.entry.session_key) is None
        return
    adapter.send.assert_awaited_once()
    if state == "archived":
        assert "archived" in adapter.send.call_args.args[1]
        factory.assert_not_called()
        runner._resolve_session_agent_runtime.assert_not_called()
    else:
        factory.assert_called_once()
        runner._cleanup_agent_resources.assert_called_once()
        assert adapter.send.call_args.kwargs["metadata"] == {"thread_id": r.source.thread_id}


@pytest.mark.asyncio
async def test_compression_child_recovered_after_crash_keeps_project_pin(routing):
    r = routing
    await apply_turn_project_pin(r.runner, r.source, r.entry, [])
    original = project_pin_for_key(r.runner, r.entry.session_key)
    db = r.store._db_for_key(r.entry.session_key)
    assert db.try_acquire_compression_lock(r.entry.session_id, "compressor")
    db.publish_compression_child(
        parent_session_id=r.entry.session_id, child_session_id="compressed-child", source="matrix",
        messages=[{"role": "user", "content": "summary"}], compression_lock_holder="compressor",
    )
    restarted = SessionStore(r.launch / "sessions", r.runner.config)
    restarted._profile_home_cache["runtime"] = r.runtime
    restored_runner = SimpleNamespace(**{**vars(r.runner), "session_store": restarted})
    try:
        assert project_pin_for_key(restored_runner, r.entry.session_key) == original
        child = restarted.get_or_create_session(r.source)
        assert child.session_id == "compressed-child"
        assert await apply_turn_project_pin(restored_runner, r.source, child, []) is None
        assert project_pin_for_key(restored_runner, child.session_key) == original
        assert db.get_session(child.session_id)["cwd"] == str(r.folder)
    finally:
        restarted.close_all_db_handles()


@pytest.mark.asyncio
async def test_run_sync_restores_project_overlay_when_execution_raises(routing, monkeypatch):
    r = routing
    await apply_turn_project_pin(r.runner, r.source, r.entry, [])
    monkeypatch.setattr("gateway.session_authorities.active_authority", lambda runner: None)
    turn = TurnRunner(r.runner, TurnContext(source=r.source, session_key=r.entry.session_key,
                                          session_id=r.entry.session_id))

    def fail():
        assert resolve_agent_cwd() == r.folder
        raise RuntimeError("execution failed")

    turn._run_sync_scoped = fail
    with r.runner._profile_scope_for_source(r.source):
        before = resolve_agent_cwd(), terminal_env("TERMINAL_CWD")
        with pytest.raises(RuntimeError, match="execution failed"):
            await asyncio.to_thread(turn.run_sync)
        assert (resolve_agent_cwd(), terminal_env("TERMINAL_CWD")) == before


@pytest.mark.asyncio
async def test_nonboundary_switch_preserves_only_project_pin_and_boundary_clears_it(routing):
    r = routing
    await apply_turn_project_pin(r.runner, r.source, r.entry, [])
    original = project_pin_for_key(r.runner, r.entry.session_key)
    r.store.set_session_metadata(r.entry.session_key, "unrelated", "old conversation")
    db = r.store._db_for_key(r.entry.session_key)
    db.create_session("switch-child", "matrix", parent_session_id=r.entry.session_id)
    child = r.store.switch_session(r.entry.session_key, "switch-child")
    assert project_pin_for_key(r.runner, child.session_key) == original
    assert child.metadata[PIN_METADATA_KEY] is not r.entry.metadata[PIN_METADATA_KEY]
    assert "unrelated" not in child.metadata
    restarted = SessionStore(r.launch / "sessions", r.runner.config)
    restarted._profile_home_cache["runtime"] = r.runtime
    try:
        assert project_pin_for_key(SimpleNamespace(session_store=restarted), child.session_key) == original
    finally:
        restarted.close_all_db_handles()
    db.create_session("explicit-resume", "matrix")
    boundary = r.store.switch_session(child.session_key, "explicit-resume", preserve_prompt_pin=False)
    assert boundary.metadata == {}
    assert project_pin_for_key(r.runner, boundary.session_key) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("old_history,cancelled", [(True, False), (False, False), (True, True)])
async def test_resolver_cannot_publish_into_reset_successor(routing, monkeypatch, tmp_path, old_history, cancelled):
    from gateway import project_routing_turn
    r = routing
    started, release, finished = Event(), Event(), Event()
    original = project_routing_turn.resolve_turn_pin

    def paused_resolve(*args, **kwargs):
        result = original(*args, **kwargs)
        started.set()
        assert release.wait(10)
        return result

    monkeypatch.setattr(project_routing_turn, "resolve_turn_pin", paused_resolve)
    history = [{"role": "user", "content": "before binding"}] if old_history else []

    async def resolve():
        try:
            return await apply_turn_project_pin(r.runner, r.source, r.entry, history)
        finally:
            if not cancelled:
                finished.set()

    # Observe the worker itself: cancellation only unwinds the asyncio awaiter.
    original_set = r.store.set_session_project_pin

    def publish(*args, **kwargs):
        try:
            return original_set(*args, **kwargs)
        finally:
            finished.set()

    monkeypatch.setattr(r.store, "set_session_project_pin", publish)
    task = asyncio.create_task(resolve())
    try:
        assert await asyncio.to_thread(started.wait, 10)
        if cancelled:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        successor = r.store.reset_session(r.entry.session_key)
        beta = tmp_path / "current-project"
        beta.mkdir()
        with projects_db.connect_closing(db_path=r.runtime / "projects.db") as conn:
            current_project = projects_db.create_project(conn, name="Current", primary_path=str(beta))
            projects_db.set_chat_binding(conn, platform="matrix", transport_profile="receiving",
                                         chat_id=r.source.chat_id, project_id=current_project)
        release.set()
        if not cancelled:
            # Stale resolution must stop the old turn as well as refuse publication.
            with pytest.raises(RuntimeError, match="route"):
                await task
        assert await asyncio.to_thread(finished.wait, 10)
        assert successor.metadata == {}
        assert not r.store._db_for_key(successor.session_key).get_session(successor.session_id)["cwd"]
        monkeypatch.setattr(project_routing_turn, "resolve_turn_pin", original)
        await apply_turn_project_pin(r.runner, r.source, successor, [])
        assert project_pin_for_key(r.runner, successor.session_key).project_id == current_project
    finally:
        release.set()
        if not task.done():
            await task


@pytest.mark.asyncio
async def test_old_executor_never_uses_successor_project_pin(routing, monkeypatch):
    r = routing
    await apply_turn_project_pin(r.runner, r.source, r.entry, [])
    ctx = TurnContext(source=r.source, session_key=r.entry.session_key, session_id=r.entry.session_id)
    turn = TurnRunner(r.runner, ctx)
    turn._run_sync_scoped = MagicMock(return_value={"final_response": "wrong conversation"})
    monkeypatch.setattr("gateway.session_authorities.active_authority", lambda runner: None)
    successor = r.store.reset_session(r.entry.session_key)
    await apply_turn_project_pin(r.runner, r.source, successor, [])
    result = turn.run_sync()
    assert result["interrupted"] and not result["completed"]
    turn._run_sync_scoped.assert_not_called()


@pytest.mark.asyncio
async def test_old_agent_construction_never_reads_successor_metadata(routing):
    from tests.gateway.test_local_viewer_events_submit import _RecordingAgent, _gateway_runner
    r = routing
    await apply_turn_project_pin(r.runner, r.source, r.entry, [])
    runner = _gateway_runner()
    runner.session_store = r.store
    runner._agent_config_signature.side_effect = GatewayRunner._agent_config_signature
    turn = TurnRunner(runner, TurnContext(source=r.source, session_key=r.entry.session_key,
                                         session_id=r.entry.session_id, user_config={}, AIAgent=_RecordingAgent))
    successor = r.store.reset_session(r.entry.session_key)
    await apply_turn_project_pin(r.runner, r.source, successor, [])
    with pytest.raises(RuntimeError, match="route"):
        turn._resolve_turn_agent({"model": "m", "runtime": {}}, "matrix", "", 2, None, {})


@pytest.mark.asyncio
async def test_prepare_stamps_child_created_by_hygiene_after_project_validation(routing, monkeypatch):
    from gateway.session import AsyncSessionStore
    r = routing
    await apply_turn_project_pin(r.runner, r.source, r.entry, [])
    runner = object.__new__(GatewayRunner)
    runner.config, runner.session_store = r.runner.config, r.store
    runner._profile_scope_for_source = r.runner._profile_scope_for_source
    runner._hmwa_open_session = AsyncMock(return_value=(False, False))
    runner._set_session_env = lambda context: []
    runner._pinned_session_context_prompt = lambda *args, **kwargs: ""
    runner._hmwa_acquire_turn_lease = AsyncMock()
    runner._mark_durable_active_turn = AsyncMock()
    runner._hmwa_first_contact_notes = AsyncMock()
    runner._voice_channel_sidecar_note = lambda *args: None
    runner._prepare_profile_scoped_inbound_message_text = AsyncMock(return_value=None)
    runner._async_session_store = AsyncSessionStore(r.store)
    runner._rebind_turn_lease = MagicMock()
    runner._sync_telegram_topic_binding = MagicMock()
    monkeypatch.setattr("gateway.run_turn_prepare.build_session_context", lambda *args: None)
    db = r.store._db_for_key(r.entry.session_key)
    history = [{"role": "user", "content": "long history " * 100}]
    r.store.append_to_transcript(r.entry.session_id, history[0])
    original_sid = r.entry.session_id

    async def compress(event, source, entry, session_key, loaded_history, quick_key, generation):
        assert project_pin_for_key(runner, session_key).project_id == r.project
        assert db.get_session(original_sid)["cwd"] == str(r.folder)
        assert db.try_acquire_compression_lock(original_sid, "compressor")
        compressed = [{"role": "assistant", "content": "summary"}]
        db.publish_compression_child(
            parent_session_id=original_sid, child_session_id="hygiene-child", source="matrix",
            messages=compressed, compression_lock_holder="compressor",
        )
        attempt = SimpleNamespace(agent=SimpleNamespace(session_id="hygiene-child"), history=loaded_history)
        await runner._hmwa_hygiene_adopt_transcript(
            attempt, compressed, loaded_history,
            SimpleNamespace(msg_count=len(loaded_history), approx_tokens=1000, warn_token_threshold=10000),
            session_entry=entry, source=source,
            _quick_key=quick_key, run_generation=generation,
        )
        return attempt.history

    runner._hmwa_run_session_hygiene = compress
    prepared, _ = await runner._hmwa_prepare_turn(
        SimpleNamespace(internal=False, text="hi"), r.source, r.entry,
        r.entry.session_key, r.entry.session_key, 1,
    )
    assert prepared is None
    assert r.entry.session_id == "hygiene-child"
    assert project_pin_for_key(runner, r.entry.session_key).project_id == r.project
    assert db.get_session("hygiene-child")["cwd"] == str(r.folder)


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["reset", "force_new"])
async def test_project_boundary_drops_route_terminal_cwd_but_continuation_keeps_cd(routing, boundary):
    from tools.terminal_tool import _resolve_command_cwd, clear_session_cwd, record_session_cwd
    r = routing
    key = r.entry.session_key

    def cwd():
        return _resolve_command_cwd(workdir=None, default_cwd=str(r.folder), session_key=key)

    try:
        with r.runner._profile_scope_for_source(r.source):
            record_session_cwd(key, str(r.runtime))
        await apply_turn_project_pin(r.runner, r.source, r.entry, [])
        with r.runner._profile_scope_for_source(r.source):
            assert cwd() == str(r.folder)
            record_session_cwd(key, str(r.folder / "sources"))
        await apply_turn_project_pin(r.runner, r.source, r.entry, [{"role": "user", "content": "continue"}])
        with r.runner._profile_scope_for_source(r.source):
            assert cwd() == str(r.folder / "sources")
        if boundary == "reset":
            successor = r.store.reset_session(key)
        else:
            successor = r.store.get_or_create_session(r.source, force_new=True)
        await apply_turn_project_pin(r.runner, r.source, successor, [])
        with r.runner._profile_scope_for_source(r.source):
            assert cwd() == str(r.folder)
            record_session_cwd(key, str(r.folder / "after-cd"))
        db = r.store._db_for_key(key)
        db.create_session("terminal-compression-child", "matrix", parent_session_id=successor.session_id)
        child = r.store.switch_session(key, "terminal-compression-child")
        await apply_turn_project_pin(r.runner, r.source, child, [])
        with r.runner._profile_scope_for_source(r.source):
            assert cwd() == str(r.folder / "after-cd")
    finally:
        with r.runner._profile_scope_for_source(r.source):
            clear_session_cwd(key)


@pytest.mark.asyncio
async def test_published_old_pin_cannot_clear_successor_terminal_cwd(routing, monkeypatch):
    from tools.terminal_tool import clear_session_cwd, get_session_cwd, record_session_cwd
    r = routing
    key = r.entry.session_key
    published, release = Event(), Event()
    original_update = r.store._update_entry

    def pause_after_publication(*args, **kwargs):
        result = original_update(*args, **kwargs)
        assert result
        published.set()
        assert release.wait(10)
        return result

    monkeypatch.setattr(r.store, "_update_entry", pause_after_publication)
    task = asyncio.create_task(apply_turn_project_pin(r.runner, r.source, r.entry, []))
    try:
        assert await asyncio.to_thread(published.wait, 10)
        assert r.entry.metadata[PIN_METADATA_KEY]["project_id"] == r.project
        successor = r.store.reset_session(key)
        assert successor.session_id != r.entry.session_id
        successor_cwd = str(r.folder / "successor-cd")
        with r.runner._profile_scope_for_source(r.source):
            record_session_cwd(key, successor_cwd)
        release.set()
        await task
        with r.runner._profile_scope_for_source(r.source):
            assert get_session_cwd(key) == successor_cwd
        assert successor.metadata == {}
    finally:
        release.set()
        try:
            await task
        finally:
            with r.runner._profile_scope_for_source(r.source):
                clear_session_cwd(key)
