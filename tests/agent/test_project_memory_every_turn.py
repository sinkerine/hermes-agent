"""Project conversations recall on literal followups; other sessions keep the trivial gate."""

from types import SimpleNamespace

import pytest

from agent.memory_manager import MemoryManager
from agent.memory_provider import MemoryProvider, is_trivial_prompt
from agent.turn_context import _memory_turn_start_and_prefetch
from hermes_cli.config import atomic_config_write
from hermes_constants import get_hermes_home


class TurnRecordingProvider(MemoryProvider):
    def __init__(self, name: str, events: list[tuple]):
        self._name = name
        self.events = events

    @property
    def name(self) -> str:
        return self._name

    def is_available(self) -> bool:
        return True

    def initialize(self, session_id: str, **kwargs) -> None:
        pass

    def get_tool_schemas(self) -> list[dict]:
        return []

    def on_turn_start(self, turn_number: int, message: str, **kwargs) -> None:
        self.events.append((self.name, "start", turn_number, message))

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        self.events.append((self.name, "recall", session_id, query))
        return f"{self.name} memories"


@pytest.fixture
def memory_turn():
    events = []
    manager = MemoryManager()
    shared = TurnRecordingProvider("shared", events)
    manager.add_provider(shared)
    manager.initialize_all("session", platform="matrix")
    agent = SimpleNamespace(_memory_manager=manager, _user_turn_count=1, session_id="session")
    try:
        yield agent, manager, events
    finally:
        manager.shutdown_all()


def expected_events(names, query, turn):
    return ([(name, "start", turn, query) for name in names]
            + [(name, "recall", "session", query) for name in names])


@pytest.mark.parametrize("query", ["next", "continue", "proceed", "do it", "what do we know?"])
def test_project_slot_recalls_shared_and_project_on_each_followup(memory_turn, query):
    agent, manager, events = memory_turn
    manager.add_provider(TurnRecordingProvider("project", events), slot="project")
    names = ["shared", "project"]
    assert _memory_turn_start_and_prefetch(agent, "initial question") == "shared memories\n\nproject memories"
    assert events == expected_events(names, "initial question", 1)
    events.clear()
    agent._user_turn_count = 2
    assert is_trivial_prompt(query) is (query != "what do we know?")
    assert _memory_turn_start_and_prefetch(agent, query) == "shared memories\n\nproject memories"
    assert events == expected_events(names, query, 2)


@pytest.mark.parametrize("platform,service_url", [
    ("matrix", ""), ("matrix", "http://127.0.0.1:1"),
    ("cli", "http://127.0.0.1:1"), ("tui", "http://127.0.0.1:1"),
    ("desktop", "http://127.0.0.1:1"),
])
@pytest.mark.parametrize("query", ["next", "continue", "proceed", "do it", "what do we know?"])
def test_without_project_slot_preserves_trivial_gate(request, platform, service_url, query):
    atomic_config_write(get_hermes_home() / "config.yaml", {"project_memory": {"hindsight_url": service_url}})
    agent, manager, events = request.getfixturevalue("memory_turn")
    assert manager.prefetch_every_turn is False
    agent.platform = platform
    result = _memory_turn_start_and_prefetch(agent, query)
    if is_trivial_prompt(query):
        assert result == ""
        assert events == [("shared", "start", 1, query)]
    else:
        assert result == "shared memories"
        assert events == expected_events(["shared"], query, 1)


def test_every_turn_capability_tracks_project_slot_add_and_drop(memory_turn):
    agent, manager, events = memory_turn
    assert manager.prefetch_every_turn is False
    project = TurnRecordingProvider("project", events)
    manager.add_provider(project, slot="project")
    assert manager.prefetch_every_turn is True
    assert _memory_turn_start_and_prefetch(agent, "next") == "shared memories\n\nproject memories"
    assert events == expected_events(["shared", "project"], "next", 1)
    manager._drop_provider(project)
    assert manager.prefetch_every_turn is False
    events.clear()
    assert _memory_turn_start_and_prefetch(agent, "continue") == ""
    assert events == [("shared", "start", 1, "continue")]
