"""Conversation pin resolution and isolated project cwd scopes."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from threading import Barrier
from types import SimpleNamespace

import pytest

from agent.runtime_cwd import (
    reset_session_cwd, resolve_agent_cwd, resolve_context_cwd, set_session_cwd,
)
from gateway import project_routing as routing
from gateway.config import Platform
from gateway.session import SessionSource
from gateway.session_identity import RoutingIdentity
from hermes_cli import projects_db as db
from tools.terminal_scope import (
    TerminalPolicyRefusal, TerminalPolicyUnavailable, enforce_no_refusal,
    get_terminal_scope, reset_terminal_scope, set_terminal_scope, terminal_env,
)


@pytest.fixture
def route(tmp_path):
    home = tmp_path / "runtime"
    home.mkdir()
    source = SessionSource(platform=Platform.MATRIX, chat_id="!room:example", profile="runtime")
    source._identity = RoutingIdentity(
        transport_profile="receiving", runtime_profile="runtime",
        authorization_home=tmp_path / "receiving", runtime_home=home,
    )
    runner = SimpleNamespace(
        config=SimpleNamespace(multiplex_profiles=True), _primary_profile_name="default",
        _resolve_profile_home_for_source=lambda event: event._identity.runtime_home,
    )
    folder = tmp_path / "alpha"
    folder.mkdir()
    with db.connect_closing(db_path=home / "projects.db") as conn:
        project_id = db.create_project(conn, name="Alpha", primary_path=str(folder))
        db.set_chat_binding(
            conn, platform="matrix", transport_profile="receiving", chat_id=source.chat_id,
            project_id=project_id,
        )
    token = set_terminal_scope({"TERMINAL_ENV": "local"})
    try:
        yield runner, source, project_id, folder
    finally:
        reset_terminal_scope(token)


def test_pin_serialization_is_typed_and_immutable():
    pin = routing.ProjectPin("p_alpha", "alpha", "Alpha", "/alpha", "runtime", 123)
    assert routing.ProjectPin.from_dict(pin.to_dict()) == pin
    assert routing.ProjectPin.from_dict({"project_id": None}) == routing.UNBOUND_PIN
    assert not routing.UNBOUND_PIN.bound
    with pytest.raises(FrozenInstanceError):
        pin.cwd = "/other"
    for data in (None, [], {}, {"project_id": ""}, {"project_id": 4},
                 {"project_id": "p", "cwd": []}, {"project_id": None, "profile": None},
                 {"project_id": None, "pinned_at": True}, {"project_id": None, "pinned_at": -1}):
        assert routing.ProjectPin.from_dict(data) is None


def test_binding_key_uses_transport_and_parent_room(route):
    runner, source, _, _ = route
    source.parent_chat_id = "!parent:example"
    source.chat_id = "thread-id"
    assert routing.binding_chat_id(source) == "!parent:example"
    assert routing.binding_key(runner, source) == ("matrix", "receiving", "!parent:example")
    assert routing.runtime_projects_db(runner, source) == source._identity.runtime_home / "projects.db"
    assert source._identity.transport is None  # Restored/internal identity needs no live adapter.
    del source._identity
    with pytest.raises(routing.ProjectRouteError) as error:
        routing.binding_key(runner, source)
    assert error.value.reason == "no_identity"
    runner.config.multiplex_profiles = False
    assert routing.binding_key(runner, source)[1] == "default"
    runner._primary_profile_name = "standalone"
    assert routing.binding_key(runner, source)[1] == "standalone"


def test_new_bound_conversation_reads_runtime_db_and_freezes_pin(route, monkeypatch, tmp_path):
    runner, source, project_id, folder = route
    ambient = tmp_path / "ambient"
    ambient.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(ambient))
    pin = routing.pin_from_binding(runner, source, now=123)
    assert pin == routing.ProjectPin(project_id, "alpha", "Alpha", str(folder), "runtime", 123)
    resolved, persist = routing.resolve_turn_pin(runner, source, None, history_empty=True)
    assert resolved.project_id == project_id and persist
    assert not (ambient / "projects.db").exists()
    other = tmp_path / "beta"
    other.mkdir()
    with db.connect_closing(db_path=routing.runtime_projects_db(runner, source)) as conn:
        beta = db.create_project(conn, name="Beta", primary_path=str(other))
        db.set_chat_binding(conn, platform="matrix", transport_profile="receiving",
                            chat_id=source.chat_id, project_id=beta)
        db.add_folder(conn, project_id, str(other), is_primary=True)
        db.update_project(conn, project_id, name="Renamed")
    for history_empty in (True, False):
        retained, persist = routing.resolve_turn_pin(runner, source, pin, history_empty=history_empty)
        assert retained is pin and not persist
    fresh, persist = routing.resolve_turn_pin(runner, source, None, history_empty=True)
    assert fresh.project_id == beta and persist


def test_explicit_unbound_and_old_history_ignore_later_binding_and_backend(route):
    runner, source, _, _ = route
    token = set_terminal_scope({"TERMINAL_ENV": "ssh"})
    try:
        for history_empty in (True, False):
            assert routing.resolve_turn_pin(runner, source, routing.UNBOUND_PIN, history_empty=history_empty) == (
                routing.UNBOUND_PIN, False)
        assert routing.resolve_turn_pin(runner, source, None, history_empty=False) == (routing.UNBOUND_PIN, True)
        source.chat_id = "!unbound:example"
        assert routing.resolve_turn_pin(runner, source, None, history_empty=True) == (routing.UNBOUND_PIN, True)
    finally:
        reset_terminal_scope(token)


@pytest.mark.parametrize("failure", ["missing", "archived", "no_path", "path_missing", "backend"])
@pytest.mark.parametrize("existing", [False, True])
def test_invalid_bound_or_pinned_project_fails_closed(route, failure, existing):
    runner, source, project_id, folder = route
    pin = routing.pin_from_binding(runner, source)
    with db.connect_closing(db_path=routing.runtime_projects_db(runner, source)) as conn:
        mutations = {
            "missing": lambda: db.delete_project(conn, project_id),
            "archived": lambda: db.archive_project(conn, project_id),
            "no_path": lambda: db.remove_folder(conn, project_id, str(folder)),
            "path_missing": folder.rmdir,
            "backend": lambda: None,
        }
        mutations[failure]()
    token = set_terminal_scope({"TERMINAL_ENV": "ssh" if failure == "backend" else "local"})
    try:
        with pytest.raises(routing.ProjectRouteError) as error:
            routing.resolve_turn_pin(runner, source, pin if existing else None, history_empty=True)
        assert error.value.reason == failure
        text = error.value.user_message()
        assert text and "gateway.project.error" not in text and "{" not in text
    finally:
        reset_terminal_scope(token)


@pytest.mark.parametrize("existing", [False, True])
def test_deleted_project_id_cannot_resolve_replacement_slug(route, tmp_path, existing):
    runner, source, project_id, folder = route
    pin = routing.pin_from_binding(runner, source)
    replacement_folder = tmp_path / "replacement"
    replacement_folder.mkdir()
    with db.connect_closing(db_path=routing.runtime_projects_db(runner, source)) as conn:
        assert db.delete_project(conn, project_id)
        replacement_id = db.create_project(
            conn, name="Replacement", slug=project_id, primary_path=str(replacement_folder),
        )
        # Interactive lookup still accepts slugs, including a deleted project's ID.
        assert db.get_project(conn, project_id).id == replacement_id
        assert db.get_chat_binding(
            conn, platform="matrix", transport_profile="receiving", chat_id=source.chat_id,
        ).project_id == project_id
    assert folder.is_dir()
    with pytest.raises(routing.ProjectRouteError) as error:
        routing.resolve_turn_pin(runner, source, pin if existing else None, history_empty=True)
    assert error.value.reason == "missing"


def test_pin_original_folder_is_revalidated_after_project_moves(route, tmp_path):
    runner, source, project_id, folder = route
    pin = routing.pin_from_binding(runner, source)
    new_folder = tmp_path / "new"
    new_folder.mkdir()
    with db.connect_closing(db_path=routing.runtime_projects_db(runner, source)) as conn:
        db.add_folder(conn, project_id, str(new_folder), is_primary=True)
    folder.rmdir()
    with pytest.raises(routing.ProjectRouteError) as error:
        routing.revalidate_pin(runner, source, pin)
    assert error.value.reason == "path_missing"
    assert error.value.params["path"] == str(folder)


def test_missing_binding_project_can_be_retried(route):
    runner, source, project_id, folder = route
    with db.connect_closing(db_path=routing.runtime_projects_db(runner, source)) as conn:
        db.archive_project(conn, project_id)
    with pytest.raises(routing.ProjectRouteError):
        routing.resolve_turn_pin(runner, source, None, history_empty=True)
    with db.connect_closing(db_path=routing.runtime_projects_db(runner, source)) as conn:
        db.restore_project(conn, project_id)
    pin, persist = routing.resolve_turn_pin(runner, source, None, history_empty=True)
    assert pin.cwd == str(folder) and persist


def test_validate_normalizes_primary_path(route):
    runner, source, project_id, folder = route
    with db.connect_closing(db_path=routing.runtime_projects_db(runner, source)) as conn:
        project = db.get_project(conn, project_id)
    project.primary_path = str(folder / ".." / folder.name)
    assert routing.validate_project(project, "alpha") == str(folder)
    assert "gateway.project.error" not in routing.ProjectRouteError("no_identity").user_message()


@pytest.mark.parametrize("backend", ["", "local"])
def test_local_backend_allowed(backend):
    token = set_terminal_scope({"TERMINAL_ENV": backend})
    try:
        routing.require_local_backend()
    finally:
        reset_terminal_scope(token)


def _cwd_values():
    return resolve_agent_cwd(), resolve_context_cwd(), terminal_env("TERMINAL_CWD")


def test_scope_restores_prior_cwds_and_mapping_on_error(tmp_path):
    before = tmp_path / "before"
    project = tmp_path / "project"
    before.mkdir()
    project.mkdir()
    base = {"TERMINAL_CWD": str(before), "TERMINAL_ENV": "local", "TERMINAL_TIMEOUT": "17"}
    terminal_token = set_terminal_scope(base)
    cwd_token = set_session_cwd(str(before))
    try:
        with pytest.raises(RuntimeError, match="turn failed"):
            with routing.project_turn_scope(routing.ProjectPin("p", cwd=str(project))):
                assert _cwd_values() == (project, project, str(project))
                assert terminal_env("TERMINAL_TIMEOUT") == "17"
                raise RuntimeError("turn failed")
        assert _cwd_values() == (before, before, str(before))
        assert get_terminal_scope() is base
        assert base["TERMINAL_CWD"] == str(before)
        for pin in (None, routing.UNBOUND_PIN):
            with routing.project_turn_scope(pin):
                assert get_terminal_scope() is base
                assert _cwd_values() == (before, before, str(before))
    finally:
        reset_session_cwd(cwd_token)
        reset_terminal_scope(terminal_token)


def test_two_threads_observe_only_their_pin_and_restore(tmp_path):
    barrier = Barrier(2)
    paths = [tmp_path / name for name in ("alpha", "beta", "prior")]
    for path in paths:
        path.mkdir()

    def turn(path):
        base = {"TERMINAL_CWD": str(paths[2]), "TERMINAL_ENV": "local"}
        terminal_token = set_terminal_scope(base)
        cwd_token = set_session_cwd(str(paths[2]))
        try:
            with routing.project_turn_scope(routing.ProjectPin("p_" + path.name, cwd=str(path))):
                barrier.wait(timeout=10)
                assert _cwd_values() == (path, path, str(path))
                barrier.wait(timeout=10)
            assert _cwd_values() == (paths[2], paths[2], str(paths[2]))
            assert get_terminal_scope() is base
        finally:
            reset_session_cwd(cwd_token)
            reset_terminal_scope(terminal_token)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(turn, path) for path in paths[:2]]
        for future in futures:
            future.result(timeout=20)


@pytest.mark.parametrize("existing", [False, True])
def test_unscoped_resolution_refuses_yaml_backend_over_ambient_local(route, monkeypatch, existing):
    runner, source, _, _ = route
    pin = routing.pin_from_binding(runner, source)
    home = source._identity.runtime_home
    (home / "config.yaml").write_text("terminal:\n  backend: ssh\n")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("TERMINAL_ENV", "local")
    token = set_terminal_scope(None)
    try:
        before = _cwd_values()
        with pytest.raises(routing.ProjectRouteError) as error:
            routing.resolve_turn_pin(runner, source, pin if existing else None, history_empty=True)
        assert error.value.reason == "backend"
        assert error.value.params == {"backend": "ssh"}
        assert get_terminal_scope() is None
        assert _cwd_values() == before
    finally:
        reset_terminal_scope(token)


def test_unscoped_turn_refuses_yaml_backend_and_restores_scopes(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text("terminal:\n  backend: ssh\n")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("TERMINAL_ENV", "local")
    monkeypatch.setenv("TERMINAL_CWD", str(home))
    token = set_terminal_scope(None)
    cwd_token = set_session_cwd(str(home))
    executed = False
    try:
        before = _cwd_values()
        with pytest.raises(routing.ProjectRouteError) as error:
            with routing.project_turn_scope(routing.ProjectPin("p", cwd=str(tmp_path))):
                executed = True
        assert not executed
        assert error.value.reason == "backend"
        assert error.value.params == {"backend": "ssh"}
        assert "ssh" in error.value.user_message()
        assert get_terminal_scope() is None
        assert _cwd_values() == before
    finally:
        reset_session_cwd(cwd_token)
        reset_terminal_scope(token)


@pytest.mark.parametrize("scoped", [False, True])
def test_nonlocal_scope_refuses_execution_and_preserves_policy(tmp_path, monkeypatch, scoped):
    monkeypatch.setenv("TERMINAL_ENV", "ssh")
    monkeypatch.setenv("TERMINAL_SSH_HOST", "standalone-host")
    base = {"TERMINAL_ENV": "ssh", "TERMINAL_SSH_HOST": "scoped-host"} if scoped else None
    token = set_terminal_scope(base)
    executed = False
    try:
        before = _cwd_values()
        host = terminal_env("TERMINAL_SSH_HOST")
        with pytest.raises(routing.ProjectRouteError) as error:
            with routing.project_turn_scope(routing.ProjectPin("p", cwd=str(tmp_path))):
                executed = True
        assert not executed
        assert error.value.reason == "backend"
        assert error.value.params == {"backend": "ssh"}
        assert get_terminal_scope() is base
        assert terminal_env("TERMINAL_ENV") == "ssh"
        assert terminal_env("TERMINAL_SSH_HOST") == host
        assert _cwd_values() == before
    finally:
        reset_terminal_scope(token)


def test_standalone_config_overrides_environment_policy(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("TERMINAL_ENV", "ssh")
    (home / "config.yaml").write_text("terminal:\n  backend: local\n")
    token = set_terminal_scope(None)
    try:
        routing.require_local_backend()
        assert get_terminal_scope() is None
        assert terminal_env("TERMINAL_ENV") == "ssh"
        with routing.project_turn_scope(routing.ProjectPin("p", cwd=str(tmp_path))):
            assert terminal_env("TERMINAL_ENV") == "local"
            routing.require_local_backend()
        assert get_terminal_scope() is None
        assert terminal_env("TERMINAL_ENV") == "ssh"
    finally:
        reset_terminal_scope(token)


@pytest.mark.parametrize("prepared", [False, True])
def test_scope_preserves_terminal_refusal(tmp_path, monkeypatch, prepared):
    refusal = TerminalPolicyRefusal("unreadable policy")
    if prepared:
        monkeypatch.setattr(routing, "prepare_profile_terminal_scope", lambda *args, **kwargs: refusal)
    token = set_terminal_scope(None if prepared else refusal)
    try:
        with routing.project_turn_scope(routing.ProjectPin("p", cwd=str(tmp_path))):
            assert get_terminal_scope() is refusal
            with pytest.raises(TerminalPolicyUnavailable, match="unreadable policy"):
                enforce_no_refusal()
            with pytest.raises(TerminalPolicyUnavailable, match="unreadable policy"):
                routing.require_local_backend()
            assert get_terminal_scope() is refusal
        assert get_terminal_scope() is (None if prepared else refusal)
    finally:
        reset_terminal_scope(token)


def test_empty_scoped_policy_never_borrows_ambient_backend(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("terminal:\n  backend: ssh\n")
    monkeypatch.setenv("TERMINAL_ENV", "ssh")
    base = {}
    token = set_terminal_scope(base)
    try:
        with routing.project_turn_scope(routing.ProjectPin("p", cwd=str(tmp_path))):
            assert terminal_env("TERMINAL_ENV") == ""
            routing.require_local_backend()
        assert get_terminal_scope() is base
    finally:
        reset_terminal_scope(token)
