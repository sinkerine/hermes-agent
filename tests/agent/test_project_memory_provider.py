"""Project memory behavior against real SQLite and loopback Hindsight services."""
import json
import logging
from contextvars import copy_context
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from threading import Barrier, Event
from types import SimpleNamespace

import pytest

from agent.memory_manager import MemoryManager
from agent.project_memory.config import API_KEY_ENV, ProjectMemoryServiceConfig
from agent.project_memory.hindsight import HindsightBackend
from agent.project_memory.provider import (
    PROJECT_MEMORY_RECALL_TOOL, ProjectMemoryProvider, ProjectMemoryTarget, build_project_memory_provider,
)
from agent.secret_scope import build_profile_secret_scope, reset_secret_scope, set_secret_scope
from hermes_cli import projects_db as db
from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from tests.fakes.hindsight_standin import HindsightStandin


@contextmanager
def profile_scope(home):
    home_token = set_hermes_home_override(home)
    secret_token = set_secret_scope(build_profile_secret_scope(home), profile_home=str(home))
    try:
        yield
    finally:
        reset_secret_scope(secret_token)
        reset_hermes_home_override(home_token)


def create_target(home, banks=("A", "B")):
    home.mkdir(exist_ok=True)
    with db.connect_closing(db_path=home / "projects.db") as conn:
        project = db.create_project(conn, name="Alpha", primary_path=str(home))
        for bank in banks:
            db.add_memory_bank(conn, project, bank, writable=bank != "B")
    return ProjectMemoryTarget(project, "alpha", "Alpha", home / "projects.db")


@pytest.fixture
def memory(tmp_path):
    with HindsightStandin() as server:
        target = create_target(tmp_path)
        server.seed("A", [{"text": "First fact", "type": "world"}, {"text": " Same   fact "}])
        server.seed("B", [{"text": "same fact"}, {"text": "Second fact"}])
        provider = ProjectMemoryProvider(target, ProjectMemoryServiceConfig(server.url),
                                         HindsightBackend(ProjectMemoryServiceConfig(server.url)))
        provider.initialize("session", platform="matrix", chat_id="room", thread_id="thread")
        try:
            yield SimpleNamespace(provider=provider, server=server, target=target)
        finally:
            provider.shutdown()


def test_ordered_merge_dedup_tool_filter_and_status(memory):
    p, server = memory.provider, memory.server
    block = p.prefetch("facts")
    assert block == "## Project memory — Alpha\n[A] First fact\n[A]  Same   fact \n[B] Second fact"
    assert p.recall_status().count == 3
    result = json.loads(p.handle_tool_call(PROJECT_MEMORY_RECALL_TOOL, {"query": "facts", "bank": "B"}))
    assert [hit["text"] for hit in result["results"]] == ["same fact", "Second fact"]
    assert result["failed_banks"] == []
    assert [r["path"] for r in server.requests()].count("/v1/default/banks/A/memories/recall") == 1
    assert "error" in json.loads(p.handle_tool_call(PROJECT_MEMORY_RECALL_TOOL, {"query": "facts", "bank": "X"}))


def test_snapshot_refreshes_banks_flags_and_keeps_prompt_and_schema_static(memory):
    p, target = memory.provider, memory.target
    p.on_turn_start(1, "facts")
    prompt, schemas = p.system_prompt_block(), p.get_tool_schemas()
    shared = SimpleNamespace(name="external")
    assert p.auto_retain_allows(shared) and not p.auto_retain_allows(p)
    with db.connect_closing(db_path=target.projects_db) as conn:
        db.remove_memory_bank(conn, target.project_id, "A")
        db.add_memory_bank(conn, target.project_id, "C")
        db.set_memory_settings(conn, target.project_id, retain_shared=False, retain_project=True)
    memory.server.seed("C", [{"text": "New fact"}])
    assert "[A]" in p.prefetch("facts")
    p.on_turn_start(2, "facts")
    block = p.prefetch("facts")
    assert "[A]" not in block and block.index("[B]") < block.index("[C]")
    assert not p.auto_retain_allows(shared) and p.auto_retain_allows(p)
    assert p.auto_retain_allows(SimpleNamespace(name="builtin"))
    assert p.system_prompt_block() == prompt and p.get_tool_schemas() == schemas
    with db.connect_closing(db_path=target.projects_db) as conn:
        db.remove_memory_bank(conn, target.project_id, "B")
        db.remove_memory_bank(conn, target.project_id, "C")
    p.on_turn_start(3, "facts")
    assert p.prefetch("facts") == "" and p.recall_status() is None
    assert json.loads(p.handle_tool_call(PROJECT_MEMORY_RECALL_TOOL, {"query": "facts"})) == {
        "results": [], "note": "no banks attached"}
    assert p.system_prompt_block() == prompt and p.get_tool_schemas() == schemas


def test_retain_only_writable_bank_and_metadata(memory):
    p = memory.provider
    p.sync_turn("user", "answer")
    assert memory.server.requests() == []
    with db.connect_closing(db_path=memory.target.projects_db) as conn:
        db.set_memory_settings(conn, memory.target.project_id, retain_project=True)
    p.on_turn_start(7, "user")
    p.sync_turn("user", "answer", session_id="s7")
    requests = memory.server.requests()
    assert [r["path"] for r in requests] == ["/v1/default/banks/A/memories"]
    assert requests[0]["body"]["items"] == [{
        "content": "User: user\nAssistant: answer", "context": "Hermes project alpha conversation",
        "document_id": "s7-7", "tags": ["hermes", "project:alpha", "session:s7"],
        "metadata": {"platform": "matrix", "chat_id": "room", "thread_id": "thread"}}]


@pytest.mark.parametrize("timeout", [False, True])
def test_bank_failure_cooldown_warns_once_and_recovers(memory, monkeypatch, caplog, timeout):
    p, server = memory.provider, memory.server
    clock = [100.0]
    monkeypatch.setattr("agent.project_memory.provider.monotonic", lambda: clock[0])
    if timeout:
        p.service = replace(p.service, timeout=0.2)
    server.queue_fault("B", status=503, delay=2 if timeout else 0)
    with caplog.at_level(logging.INFO, logger="agent.project_memory.provider"):
        for _ in range(2):
            assert "First fact" in p.prefetch("facts")
        assert len(server.requests("/v1/default/banks/B/")) == 1
        assert sum(r.levelno == logging.WARNING for r in caplog.records) == 1
        clock[0] += 301
        assert "Second fact" in p.prefetch("facts")
        assert "recovered" in caplog.text


def test_retain_failure_cooldown_is_separate_and_fails_open(memory, caplog):
    p, server = memory.provider, memory.server
    with db.connect_closing(db_path=memory.target.projects_db) as conn:
        db.set_memory_settings(conn, memory.target.project_id, retain_project=True)
    p.on_turn_start(1, "facts")
    server.queue_fault("A", status=500)
    p.sync_turn("u", "a")
    p.sync_turn("u", "a")
    assert len(server.requests("/v1/default/banks/A/memories")) == 1
    assert "First fact" in p.prefetch("facts")
    assert sum(r.levelno == logging.WARNING for r in caplog.records) == 1


def test_queued_retain_keeps_originating_turn_snapshot(memory):
    p, target = memory.provider, memory.target
    manager = MemoryManager()
    manager.add_provider(p, slot="project")
    manager.set_auto_retain_policy(p.auto_retain_allows)
    manager.initialize_all("session", platform="matrix")
    started, release = Event(), Event()

    def block_worker():
        started.set()
        assert release.wait(10)

    try:
        manager._submit_background(block_worker)
        assert started.wait(10)
        with db.connect_closing(db_path=target.projects_db) as conn:
            db.set_memory_settings(conn, target.project_id, retain_project=True)
        manager.on_turn_start(1, "first")
        manager.sync_all("first", "answer", session_id="session")
        with db.connect_closing(db_path=target.projects_db) as conn:
            db.remove_memory_bank(conn, target.project_id, "A")
            db.add_memory_bank(conn, target.project_id, "C")
        memory.server.seed("C", [])
        manager.on_turn_start(2, "second")
        manager.sync_all("second", "answer", session_id="session")
        release.set()
        assert manager.flush_pending(timeout=10)
        writes = [r for r in memory.server.requests() if r["path"].endswith("/memories")]
        assert [(r["path"], r["body"]["items"][0]["document_id"]) for r in writes] == [
            ("/v1/default/banks/A/memories", "session-1"), ("/v1/default/banks/C/memories", "session-2")]
    finally:
        release.set()
        manager.shutdown_all()


@pytest.mark.parametrize("prime_turn", [False, True])
def test_copied_turn_refreshes_parent_recall_policy_and_preserves_queued_write(memory, prime_turn):
    from tests.agent.test_memory_manager_retain_policy import RecordingProvider
    p, target = memory.provider, memory.target
    with db.connect_closing(db_path=target.projects_db) as conn:
        db.set_memory_settings(conn, target.project_id, retain_project=True)
    shared = RecordingProvider("shared", checkpoint_version=2)
    manager = MemoryManager()
    manager.add_provider(shared)
    manager.add_provider(p, slot="project")
    manager.set_auto_retain_policy(p.auto_retain_allows)
    manager.initialize_all("session", platform="matrix")
    started, release = Event(), Event()

    def block_worker():
        started.set()
        assert release.wait(10)

    try:
        if prime_turn:
            manager.on_turn_start(1, "first")
        assert "[A] First fact" in manager.prefetch_all("facts")
        manager._submit_background(block_worker)
        assert started.wait(10)
        manager.sync_all("first", "answer", session_id="session")
        with db.connect_closing(db_path=target.projects_db) as conn:
            db.remove_memory_bank(conn, target.project_id, "A")
            db.remove_memory_bank(conn, target.project_id, "B")
            db.add_memory_bank(conn, target.project_id, "C")
            db.set_memory_settings(conn, target.project_id, retain_shared=False)
        memory.server.seed("C", [{"text": "Current fact"}])
        copy_context().run(manager.on_turn_start, 2, "second")
        shared.calls.clear()
        recall = manager.prefetch_all("facts")
        assert "[C] Current fact" in recall and "[A]" not in recall
        result = json.loads(manager.handle_tool_call(PROJECT_MEMORY_RECALL_TOOL, {"query": "facts"}))
        assert [hit["bank"] for hit in result["results"]] == ["C"]
        assert not p.auto_retain_allows(shared)
        manager.on_session_end([])
        assert manager.on_pre_compress([]) == ""
        assert not manager.supports_pre_compress_checkpoint()
        manager.on_delegation("task", "result")
        manager.sync_all("second", "answer", session_id="session")
        release.set()
        assert manager.flush_pending(timeout=10)
        assert shared.calls == ["prefetch", "sync_turn"]
        writes = [r for r in memory.server.requests() if r["path"].endswith("/memories")]
        assert [(r["path"], r["body"]["items"][0]["document_id"]) for r in writes] == [
            ("/v1/default/banks/A/memories", f"session-{int(prime_turn)}"),
            ("/v1/default/banks/C/memories", "session-2")]
    finally:
        release.set()
        manager.shutdown_all()


def test_failed_snapshot_read_preserves_previous_banks(memory, monkeypatch, caplog):
    p = memory.provider
    p.on_turn_start(1, "facts")
    def fail(**kwargs):
        raise OSError("database unavailable")
    monkeypatch.setattr("agent.project_memory.provider.connect_closing", fail)
    with caplog.at_level(logging.DEBUG):
        p.on_turn_start(2, "facts")
    assert "First fact" in p.prefetch("facts")
    assert "snapshot refresh failed" in caplog.text


def test_profiles_recall_and_background_retain_use_own_home_key_and_server(tmp_path):
    from hermes_cli.config import atomic_config_write
    barrier = Barrier(2)
    with HindsightStandin() as first, HindsightStandin() as second:
        def run(name, server):
            home = tmp_path / name
            target = create_target(home, (name,))
            (home / ".env").write_text(f"{API_KEY_ENV}={name}-key\n")
            atomic_config_write(home / "config.yaml", {"project_memory": {"hindsight_url": server.url}})
            with db.connect_closing(db_path=target.projects_db) as conn:
                db.set_memory_settings(conn, target.project_id, retain_project=True)
            server.seed(name, [{"text": f"{name} fact"}])
            with profile_scope(home):
                provider = build_project_memory_provider(target)
                manager = MemoryManager()
                manager.add_provider(provider, slot="project")
                manager.set_auto_retain_policy(provider.auto_retain_allows)
                manager.initialize_all(name, platform="matrix", hermes_home=str(home))
                try:
                    barrier.wait(timeout=10)
                    manager.on_turn_start(1, "facts")
                    assert f"[{name}] {name} fact" in manager.prefetch_all("facts")
                    manager.sync_all("question", "answer", session_id=name)
                    assert manager.flush_pending(timeout=10)
                finally:
                    manager.shutdown_all()
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(run, name, server) for name, server in (("first", first), ("second", second))]
            for future in futures:
                future.result(timeout=20)
        for name, server in (("first", first), ("second", second)):
            requests = server.requests()
            assert len(requests) == 2
            assert all(r["path"].startswith(f"/v1/default/banks/{name}/") for r in requests)
            assert all(r["headers"]["Authorization"] == f"Bearer {name}-key" for r in requests)
            assert requests[1]["body"]["items"][0]["document_id"] == f"{name}-1"


def test_project_policy_controls_shared_hooks_and_preserves_combined_recall(memory):
    from tests.agent.test_memory_manager_retain_policy import RecordingProvider
    p, target = memory.provider, memory.target
    shared = RecordingProvider("shared", checkpoint_version=2)
    manager = MemoryManager()
    manager.add_provider(shared)
    manager.add_provider(p, slot="project")
    manager.set_auto_retain_policy(p.auto_retain_allows)
    manager.initialize_all("session", platform="matrix")
    try:
        manager.on_turn_start(1, "facts")
        recall = manager.prefetch_all("facts")
        assert recall.index("shared memories") < recall.index("## Project memory")
        manager.sync_all("first", "answer", session_id="session")
        assert manager.flush_pending(timeout=10)
        assert "sync_turn" in shared.calls
        assert not [r for r in memory.server.requests() if r["path"].endswith("/memories")]
        with db.connect_closing(db_path=target.projects_db) as conn:
            db.set_memory_settings(conn, target.project_id, retain_shared=False, retain_project=True)
        shared.calls.clear()
        manager.on_turn_start(2, "facts")
        manager.sync_all("second", "answer", session_id="session")
        manager.on_session_end([])
        assert manager.on_pre_compress([]) == ""
        assert not manager.supports_pre_compress_checkpoint()
        manager.on_delegation("task", "result")
        manager.on_memory_write("add", "memory", "explicit fact")
        assert manager.flush_pending(timeout=10)
        assert set(shared.calls) == {"on_turn_start", "on_memory_write"}
        assert [r["path"] for r in memory.server.requests() if r["path"].endswith("/memories")] == [
            "/v1/default/banks/A/memories"]
    finally:
        manager.shutdown_all()
