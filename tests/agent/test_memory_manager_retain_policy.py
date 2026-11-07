"""Contracts for project-provider slots and automatic retention policy boundaries."""

import logging

import pytest

from agent.memory_manager import MemoryManager
from agent.memory_provider import MemoryProvider


class RecordingProvider(MemoryProvider):
    def __init__(self, name, *, checkpoint_version=1):
        self._name = name
        self.pre_compress_checkpoint_api_version = checkpoint_version
        self.calls = []

    @property
    def name(self):
        return self._name

    def is_available(self):
        return True

    def initialize(self, session_id, **kwargs):
        self.calls.append("initialize")

    def get_tool_schemas(self):
        return [{"name": f"{self.name}_recall", "description": "Recall memories",
                 "parameters": {"type": "object", "properties": {}}}]

    def prefetch(self, query, *, session_id=""):
        self.calls.append("prefetch")
        return f"{self.name} memories"

    def queue_prefetch(self, query, *, session_id=""):
        self.calls.append("queue_prefetch")

    def sync_turn(self, user_content, assistant_content, *, session_id=""):
        self.calls.append("sync_turn")

    def on_session_end(self, messages):
        self.calls.append("on_session_end")

    def on_pre_compress(self, messages, *, require_checkpoint=False):
        self.calls.append("on_pre_compress")
        return f"{self.name} checkpoint"

    def on_delegation(self, task, result, **kwargs):
        self.calls.append("on_delegation")

    def on_turn_start(self, turn_number, message, **kwargs):
        self.calls.append("on_turn_start")

    def on_session_switch(self, new_session_id, **kwargs):
        self.calls.append("on_session_switch")

    def handle_tool_call(self, tool_name, args, **kwargs):
        self.calls.append("tool")
        return "recalled"

    def on_memory_write(self, action, target, content, metadata=None):
        self.calls.append("on_memory_write")


@pytest.fixture
def manager():
    manager = MemoryManager()
    yield manager
    manager.shutdown_all()


def test_external_and_project_slots_are_independent(manager, caplog):
    external = RecordingProvider("shared")
    project = RecordingProvider("project")
    manager.add_provider(external)
    manager.add_provider(project, slot="project")
    manager.add_provider(RecordingProvider("other_shared"))
    manager.add_provider(RecordingProvider("other_project"), slot="project")
    assert manager.providers == [external, project]
    assert "Only one external memory provider" in caplog.text
    assert "project provider 'project' is already registered" in caplog.text
    assert manager.handle_tool_call("shared_recall", {}) == "recalled"
    assert manager.handle_tool_call("project_recall", {}) == "recalled"


@pytest.mark.parametrize("denied_slot", ["external", "project"])
def test_policy_gates_only_automatic_writes(manager, denied_slot):
    builtin = RecordingProvider("builtin")
    shared = RecordingProvider("shared")
    project = RecordingProvider("project")
    manager.add_provider(builtin)
    manager.add_provider(shared)
    manager.add_provider(project, slot="project")
    denied = shared if denied_slot == "external" else project
    allowed = project if denied_slot == "external" else shared
    manager.set_auto_retain_policy(lambda provider: provider is allowed)

    manager.sync_all("user instruction", "assistant reply")
    manager.on_session_end([])
    checkpoint = manager.on_pre_compress([])
    manager.on_delegation("task", "result")
    manager.on_turn_start(1, "user instruction")
    assert manager.prefetch_all("user instruction") == (
        "builtin memories\n\nshared memories\n\nproject memories")
    manager.queue_prefetch_all("next instruction")
    manager.on_session_switch("next-session")
    assert manager.handle_tool_call(f"{denied.name}_recall", {}) == "recalled"
    manager.on_memory_write("add", "memory", "explicit fact")
    assert manager.flush_pending(timeout=5)

    automatic = {"sync_turn", "on_session_end", "on_pre_compress", "on_delegation"}
    assert automatic.isdisjoint(denied.calls)
    assert automatic.issubset(allowed.calls)
    assert automatic.issubset(builtin.calls)
    assert set(denied.calls) == {
        "prefetch", "queue_prefetch", "on_turn_start", "on_session_switch", "tool", "on_memory_write"}
    assert f"{denied.name} checkpoint" not in checkpoint
    assert f"{allowed.name} checkpoint" in checkpoint


def test_checkpoint_support_ignores_suppressed_provider(manager):
    shared = RecordingProvider("shared", checkpoint_version=2)
    project = RecordingProvider("project")
    manager.add_provider(shared)
    manager.add_provider(project, slot="project")
    assert manager.supports_pre_compress_checkpoint()
    manager.set_auto_retain_policy(lambda provider: provider is project)
    assert not manager.supports_pre_compress_checkpoint()
    with pytest.raises(RuntimeError, match="No active memory provider completed"):
        manager.on_pre_compress([], require_checkpoint=True)
    assert "on_pre_compress" not in shared.calls
    project.pre_compress_checkpoint_api_version = 2
    assert manager.supports_pre_compress_checkpoint()
    assert manager.on_pre_compress([], require_checkpoint=True) == "project checkpoint"


def test_raising_policy_denies_non_builtin_and_warns_once(manager, caplog):
    builtin = RecordingProvider("builtin")
    shared = RecordingProvider("shared", checkpoint_version=2)
    manager.add_provider(builtin)
    manager.add_provider(shared)

    def broken_policy(provider):
        raise RuntimeError("cannot read settings")

    manager.set_auto_retain_policy(broken_policy)
    with caplog.at_level(logging.WARNING):
        manager.sync_all("instruction", "reply")
        manager.on_session_end([])
        manager.on_pre_compress([])
        manager.on_delegation("task", "result")
        assert not manager.supports_pre_compress_checkpoint()
        assert manager.flush_pending(timeout=5)
    assert shared.calls == []
    assert set(builtin.calls) == {"sync_turn", "on_session_end", "on_pre_compress", "on_delegation"}
    warnings = [r for r in caplog.records if "auto-retain policy failed" in r.message]
    assert len(warnings) == 1


def test_clearing_policy_restores_automatic_writes(manager):
    shared = RecordingProvider("shared")
    manager.add_provider(shared)
    manager.set_auto_retain_policy(lambda provider: False)
    manager.sync_all("first instruction", "first reply")
    assert manager.flush_pending(timeout=5)
    assert shared.calls == []
    manager.set_auto_retain_policy(None)
    manager.sync_all("second instruction", "second reply")
    manager.on_session_end([])
    manager.on_pre_compress([])
    manager.on_delegation("task", "result")
    assert manager.flush_pending(timeout=5)
    assert set(shared.calls) == {"sync_turn", "on_session_end", "on_pre_compress", "on_delegation"}


def test_session_boundary_suppresses_extraction_but_still_switches(manager):
    shared = RecordingProvider("shared")
    manager.add_provider(shared)
    manager.set_auto_retain_policy(lambda provider: False)
    manager.commit_session_boundary_async([], new_session_id="new-session")
    assert manager.flush_pending(timeout=5)
    assert shared.calls == ["on_session_switch"]
