"""Room-state diff, permissions and background publication contracts."""

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import quote

import pytest

from plugins.platforms.matrix.adapter_commands import COMMAND_DESCRIPTION_EVENT_TYPE as TYPE
from plugins.platforms.matrix.command_publisher import (
    MatrixCommandDescriptionsMixin, MatrixCommandPublisher, MautrixStateApi,
    plan_room, power_level_allows,
)

BOT = "@hermes:localhost"
CONTENT = {"command": "status", "parameters": []}


def event(key="key", content=None, sender=BOT, event_type=TYPE):
    return {"type": event_type, "state_key": key, "sender": sender,
            "content": CONTENT if content is None else content}


@pytest.mark.parametrize("power,expected", [
    (None, (True, 0, 0)),
    ({}, (False, 0, 50)),
    ({"events": {TYPE: 0}}, (True, 0, 0)),
    ({"state_default": 10, "users": {BOT: 10}}, (True, 10, 10)),
    ({"state_default": 10, "users_default": 9}, (False, 9, 10)),
    ({"events": {TYPE: "15"}, "users": {BOT: "20"}}, (True, 20, 15)),
    ({"events": {TYPE: "bad"}, "state_default": "5", "users": {BOT: None}, "users_default": "6"}, (True, 6, 5)),
    ({"state_default": "bad", "users_default": "bad"}, (False, 0, 50)),
])
def test_power_level_defaults_and_overrides(power, expected):
    state = [] if power is None else [event("", power, event_type="m.room.power_levels")]
    assert power_level_allows(state, BOT, TYPE) == expected


@pytest.mark.parametrize("sender,additional,allowed", [(BOT, [], True), ("@other:localhost", [BOT], True),
                                                       ("@other:localhost", [], False)])
def test_room_version_12_creators(sender, additional, allowed):
    state = [event("", {}, event_type="m.room.power_levels"),
             event("", {"room_version": "12", "additional_creators": additional}, sender, "m.room.create")]
    assert power_level_allows(state, BOT, TYPE)[0] is allowed


@pytest.mark.parametrize("state,desired,expected", [
    ([event()], {"key": CONTENT}, []),
    ([event(content={"command": "old"})], {"key": CONTENT}, [("key", CONTENT)]),
    ([event(sender="@other:localhost")], {"key": CONTENT}, [("key", CONTENT)]),
    ([event("stale")], {"key": CONTENT}, [("key", CONTENT), ("stale", {})]),
    ([event(content={})], {}, []),
    ([event(), event("foreign", sender="@other:localhost")], {}, [("key", {})]),
])
def test_room_plan(state, desired, expected):
    assert plan_room(state, BOT, desired) == expected


class StateApi:
    def __init__(self, *, states=None, errors=(), get_errors=None):
        self.states = states or {}
        self.errors = list(errors)
        self.get_errors = get_errors or {}
        self.gets = asyncio.Queue()
        self.puts = asyncio.Queue()
        self.attempts = []
        self.times = []
        self.delays = []
        self.clock = 0.0

    async def get_room_state(self, room_id):
        self.gets.put_nowait(room_id)
        if room_id in self.get_errors:
            raise self.get_errors[room_id]
        return self.states.get(room_id, [])

    async def put_room_state(self, room_id, event_type, state_key, content):
        self.times.append(self.clock)
        self.attempts.append((room_id, state_key, content))
        if self.errors:
            error = self.errors.pop(0)
            if error is not None:
                raise error
        self.states.setdefault(room_id, [])
        self.states[room_id] = [entry for entry in self.states[room_id] if entry.get("state_key") != state_key]
        self.states[room_id].append(event(state_key, content))
        self.puts.put_nowait((room_id, state_key, content))

    async def sleep(self, delay):
        self.delays.append(delay)
        self.clock += delay


async def received(queue):
    return await asyncio.wait_for(queue.get(), 3)


def publisher(api, *, enabled=True, eligible=None, manifest=None):
    return MatrixCommandPublisher(
        api=api, bot_user_id=BOT, enabled=enabled,
        is_room_eligible=eligible or AsyncMock(return_value=True),
        build_manifest=manifest or (lambda: {"key": CONTENT}), sleep=api.sleep,
    )


@pytest.mark.asyncio
async def test_pacing_global_across_rooms_and_noop_second_pass():
    api = StateApi()
    worker = publisher(api)
    worker.request_rooms(["!a", "!b"])
    worker.start()
    try:
        assert await received(api.puts) == ("!a", "key", CONTENT)
        assert await received(api.puts) == ("!b", "key", CONTENT)
        assert api.times[1] - api.times[0] >= 0.2
        while not api.gets.empty():
            api.gets.get_nowait()
        worker.request_full()
        assert await received(api.gets) == "!a"
        assert await received(api.gets) == "!b"
        assert api.puts.empty() and len(api.attempts) == 2
    finally:
        await worker.stop()


@pytest.mark.asyncio
async def test_backoff_persists_between_events_and_retries_same_key():
    from mautrix.errors import MLimitExceeded

    api = StateApi(errors=[MLimitExceeded(429), MLimitExceeded(429), None, MLimitExceeded(429), None])
    worker = publisher(api, manifest=lambda: {"key": CONTENT, "second": {"command": "help", "parameters": []}})
    worker.request_rooms(["!a"])
    worker.start()
    try:
        await received(api.puts)
        await received(api.puts)
        assert [delay for delay in api.delays if delay != 0.2] == [1, 2, 4]
        assert [attempt[1] for attempt in api.attempts] == ["key", "key", "key", "second", "second"]
        assert all(b - a >= 0.2 for a, b in zip(api.times, api.times[1:]))
    finally:
        await worker.stop()


@pytest.mark.asyncio
async def test_six_rate_limits_abandon_room_but_keep_worker_alive(caplog):
    from mautrix.errors import MLimitExceeded

    api = StateApi(errors=[MLimitExceeded(429) for _ in range(6)])
    worker = publisher(api)
    worker.request_rooms(["!a", "!b"])
    worker.start()
    try:
        assert (await received(api.puts))[0] == "!b"
        assert len([attempt for attempt in api.attempts if attempt[0] == "!a"]) == 6
        assert [delay for delay in api.delays if delay != 0.2] == [1, 2, 4, 8, 16]
        assert "retries exhausted" in caplog.text
    finally:
        await worker.stop()


@pytest.mark.asyncio
async def test_forbidden_logs_once_and_retries_on_power_change(caplog):
    from mautrix.errors import MForbidden

    caplog.set_level(logging.INFO)
    api = StateApi(errors=[MForbidden(403), MForbidden(403)])
    worker = publisher(api)
    worker.request_rooms(["!a"])
    worker.start()
    try:
        await received(api.gets)
        worker.request_rooms(["!a"])
        await received(api.gets)
        assert len([record for record in caplog.records if "cannot publish" in record.message]) == 1
        worker.observe_sync({"rooms": {"join": {"!a": {"state": {"events": [{"type": "m.room.power_levels"}]}}}}})
        assert (await received(api.puts))[0] == "!a"
    finally:
        await worker.stop()


@pytest.mark.asyncio
async def test_get_failure_skips_room_and_permission_failure_sends_nothing(caplog):
    caplog.set_level(logging.INFO)
    api = StateApi(states={"!power": [event("", {}, event_type="m.room.power_levels")]},
                   get_errors={"!failure": TimeoutError()})
    worker = publisher(api)
    worker.request_rooms(["!failure", "!power", "!ok"])
    worker.start()
    try:
        assert (await received(api.puts))[0] == "!ok"
        assert all(attempt[0] == "!ok" for attempt in api.attempts)
        assert "cannot read" in caplog.text and "needs 50 has 0" in caplog.text
    finally:
        await worker.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled,eligible", [(False, True), (True, False)])
async def test_disabled_or_ineligible_blanks_only_own_descriptions(enabled, eligible):
    api = StateApi(states={"!a": [event(), event("foreign", sender="@other:localhost")]})
    worker = publisher(api, enabled=enabled, eligible=AsyncMock(return_value=eligible))
    worker.request_rooms(["!a"])
    worker.start()
    try:
        assert await received(api.puts) == ("!a", "key", {})
        assert len(api.attempts) == 1
    finally:
        await worker.stop()


@pytest.mark.asyncio
async def test_observe_sync_joins_power_changes_and_leaves():
    api = StateApi()
    worker = publisher(api)
    worker.observe_sync({"rooms": {"join": {"!a": {}, "!left": {}}}})
    worker.observe_sync({"rooms": {"leave": {"!left": {}}}})
    worker.start()
    try:
        assert (await received(api.puts))[0] == "!a"
        assert len(api.attempts) == 1
        api.gets.get_nowait()
        worker.observe_sync({"rooms": {"join": {"!a": {"timeline": {"events": [{"type": "m.room.power_levels"}]}},
                                                   "!new": {}}}})
        assert await received(api.gets) == "!a"
        assert (await received(api.puts))[0] == "!new"
    finally:
        await worker.stop()


@pytest.mark.asyncio
async def test_reload_coalesces_while_put_is_blocked_and_stop_cancels():
    api = StateApi()
    started, cancelled = asyncio.Event(), asyncio.Event()
    builds = []

    def manifest():
        builds.append(True)
        return {"key": CONTENT}

    async def blocking_put(*args):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    api.put_room_state = blocking_put
    worker = publisher(api, manifest=manifest)
    adapter = MatrixCommandDescriptionsMixin()
    adapter._command_publisher = worker
    worker.request_rooms(["!a"])
    worker.start()
    await asyncio.wait_for(started.wait(), 3)
    for _ in range(5):
        await asyncio.wait_for(adapter.refresh_skill_group(), 3)
    assert len(builds) == 1
    await worker.stop()
    assert cancelled.is_set()
    await worker.stop()


@pytest.mark.asyncio
async def test_full_while_running_rebuilds_once_after_current_room():
    api = StateApi()
    started, release = asyncio.Event(), asyncio.Event()
    builds = []
    original_put = api.put_room_state

    async def blocking_put(*args):
        started.set()
        await release.wait()
        await original_put(*args)

    def manifest():
        builds.append(True)
        return {"key": {**CONTENT, "description": len(builds)}}

    api.put_room_state = blocking_put
    worker = publisher(api, manifest=manifest)
    worker.request_rooms(["!a"])
    worker.start()
    try:
        await asyncio.wait_for(started.wait(), 3)
        for _ in range(5):
            worker.request_full()
        release.set()
        await received(api.puts)
        assert (await received(api.puts))[2]["description"] == 2
        assert len(builds) == 2
    finally:
        await worker.stop()


@pytest.mark.asyncio
async def test_failed_manifest_retains_previous_and_next_trigger_recovers(caplog):
    api = StateApi()
    should_fail = False

    def manifest():
        if should_fail:
            raise RuntimeError("scan failure")
        return {"key": CONTENT}

    worker = publisher(api, manifest=manifest)
    worker.request_rooms(["!a"])
    worker.start()
    try:
        await received(api.puts)
        api.gets.get_nowait()
        should_fail = True
        worker.request_full()
        await received(api.gets)
        worker.request_rooms(["!new"])
        assert (await received(api.puts))[0] == "!new"
        assert any(record.exc_info for record in caplog.records)
    finally:
        await worker.stop()


@pytest.mark.asyncio
async def test_mautrix_paths_encode_standard_base64_state_key():
    client = SimpleNamespace(api=SimpleNamespace(request=AsyncMock(return_value=[])))
    api = MautrixStateApi(client)
    await api.get_room_state("!room:localhost")
    await api.put_room_state("!room:localhost", TYPE, "/+abc=", CONTENT)
    get_call, put_call = client.api.request.call_args_list
    assert str(get_call.args[1]).endswith("/rooms/%21room%3Alocalhost/state")
    assert str(put_call.args[1]).endswith(f"/state/{TYPE}/{quote('/+abc=', safe='')}")
    assert put_call.args[2] == CONTENT


@pytest.mark.asyncio
async def test_adapter_hooks_tolerate_uninitialized_instances():
    from plugins.platforms.matrix.adapter import MatrixAdapter

    adapter = object.__new__(MatrixAdapter)
    adapter._start_command_descriptions()
    adapter._observe_command_description_sync({"rooms": {"join": {"!a": {}}}})
    await adapter.refresh_skill_group()
    await adapter._stop_command_descriptions()


@pytest.mark.asyncio
async def test_adapter_reload_requests_publication():
    from unittest.mock import Mock
    from plugins.platforms.matrix.adapter import MatrixAdapter

    adapter = object.__new__(MatrixAdapter)
    adapter._command_publisher = SimpleNamespace(request_full=Mock())
    await adapter.refresh_skill_group()
    adapter._command_publisher.request_full.assert_called_once_with()


@pytest.mark.asyncio
async def test_adapter_start_honors_disable_and_bridge_then_reconnects(monkeypatch):
    from gateway.config import PlatformConfig
    from plugins.platforms.matrix.adapter import MatrixAdapter, _apply_yaml_config
    from plugins.platforms.matrix import command_publisher

    extra = _apply_yaml_config({}, {"command_descriptions": False})
    assert extra["command_descriptions"] == "false" or extra["command_descriptions"] is False
    adapter = MatrixAdapter(PlatformConfig(extra=extra))
    adapter._client = SimpleNamespace()
    adapter._user_id = BOT
    adapter._joined_rooms = {"!a"}
    api = StateApi(states={"!a": [event()]})
    monkeypatch.setattr(command_publisher, "MautrixStateApi", lambda client: api)
    adapter._start_command_descriptions()
    first = adapter._command_publisher
    try:
        assert await received(api.puts) == ("!a", "key", {})
        assert first.enabled is False
        first._warned_rooms.add("!a")
    finally:
        await adapter._stop_command_descriptions()
    adapter._start_command_descriptions()
    try:
        assert adapter._command_publisher is not first
        assert "!a" in adapter._command_publisher._warned_rooms
    finally:
        await adapter._stop_command_descriptions()


@pytest.mark.asyncio
async def test_worker_and_thread_retain_owner_profile_after_caller_scope_changes(tmp_path):
    from hermes_constants import get_hermes_home, reset_hermes_home_override, set_hermes_home_override

    owner = tmp_path / "owner"
    other = tmp_path / "other"
    observed = []
    api = StateApi()

    def manifest():
        observed.append(get_hermes_home())
        return {"key": CONTENT}

    async def eligible(room):
        observed.append(get_hermes_home())
        return True

    worker = publisher(api, manifest=manifest, eligible=eligible)
    token = set_hermes_home_override(str(owner))
    try:
        worker.request_rooms(["!a"])
        worker.start()
    finally:
        reset_hermes_home_override(token)
    try:
        await received(api.puts)
        api.gets.get_nowait()
        token = set_hermes_home_override(str(other))
        try:
            worker.request_full()
            await received(api.gets)
        finally:
            reset_hermes_home_override(token)
        assert len(observed) == 4 and all(home == owner for home in observed)
    finally:
        await worker.stop()


@pytest.mark.asyncio
async def test_other_put_error_abandons_room_and_next_trigger_retries(caplog):
    from mautrix.errors import MUnknown

    api = StateApi(errors=[MUnknown(500)])
    worker = publisher(api, manifest=lambda: {"key": CONTENT, "second": CONTENT})
    worker.request_rooms(["!a", "!b"])
    worker.start()
    try:
        assert (await received(api.puts))[0] == "!b"
        await received(api.puts)
        assert len([attempt for attempt in api.attempts if attempt[0] == "!a"]) == 1
        assert "cannot publish" in caplog.text
        worker.request_rooms(["!a"])
        assert (await received(api.puts))[0] == "!a"
    finally:
        await worker.stop()


@pytest.mark.asyncio
async def test_backoff_caps_at_thirty_seconds_within_pass():
    from mautrix.errors import MLimitExceeded

    errors = [MLimitExceeded(429)] * 4 + [None] + [MLimitExceeded(429)] * 3
    api = StateApi(errors=errors)
    worker = publisher(api, manifest=lambda: {"key": CONTENT, "second": CONTENT})
    worker.request_rooms(["!a"])
    worker.start()
    try:
        await received(api.puts)
        await received(api.puts)
        assert [delay for delay in api.delays if delay != 0.2] == [1, 2, 4, 8, 16, 30, 30]
    finally:
        await worker.stop()


@pytest.mark.asyncio
async def test_adapter_publishes_by_default(monkeypatch):
    from gateway.config import PlatformConfig
    from plugins.platforms.matrix.adapter import MatrixAdapter
    from plugins.platforms.matrix import command_publisher

    monkeypatch.delenv("MATRIX_COMMAND_DESCRIPTIONS", raising=False)
    adapter = MatrixAdapter(PlatformConfig())
    adapter._client = SimpleNamespace()
    adapter._user_id = BOT
    adapter._joined_rooms = {"!a"}
    api = StateApi()
    monkeypatch.setattr(command_publisher, "MautrixStateApi", lambda client: api)
    monkeypatch.setattr(command_publisher, "build_command_manifest", lambda bot: {"key": CONTENT})
    adapter._start_command_descriptions()
    try:
        assert await received(api.puts) == ("!a", "key", CONTENT)
    finally:
        await adapter._stop_command_descriptions()
