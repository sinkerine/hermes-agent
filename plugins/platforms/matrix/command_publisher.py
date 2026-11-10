"""MSC4391 room-state reconciliation, isolated from Matrix message handling."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from contextlib import suppress
import logging
from typing import Any, Protocol

from plugins.platforms.matrix.adapter_commands import (
    COMMAND_DESCRIPTION_EVENT_TYPE, build_command_manifest,
)

logger = logging.getLogger(__name__)


class StateApi(Protocol):
    async def get_room_state(self, room_id: str) -> list[dict[str, Any]]: ...

    async def put_room_state(self, room_id: str, event_type: str, state_key: str,
                             content: dict[str, Any]) -> None: ...


class MautrixStateApi:
    def __init__(self, client: Any) -> None:
        self.client = client

    async def get_room_state(self, room_id: str) -> list[dict[str, Any]]:
        from mautrix.api import Method, Path

        return await self.client.api.request(Method.GET, Path.v3.rooms[room_id].state)

    async def put_room_state(self, room_id: str, event_type: str, state_key: str,
                             content: dict[str, Any]) -> None:
        from mautrix.api import Method, Path

        await self.client.api.request(Method.PUT, Path.v3.rooms[room_id].state[event_type][state_key], content)


def _integer(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def power_level_allows(state: Sequence[Mapping[str, Any]], user_id: str,
                       event_type: str) -> tuple[bool, int, int]:
    events = {event.get("type"): event for event in state if event.get("state_key") == ""}
    create = events.get("m.room.create", {})
    creation = create.get("content", {})
    if str(creation.get("room_version")) == "12":
        if user_id == create.get("sender") or user_id in creation.get("additional_creators", []):
            return True, 0, 0
    power = events.get("m.room.power_levels")
    if power is None:
        return True, 0, 0
    content = power.get("content", {})
    required = _integer(content.get("events", {}).get(event_type), _integer(content.get("state_default"), 50))
    level = _integer(content.get("users", {}).get(user_id), _integer(content.get("users_default"), 0))
    return level >= required, level, required


def plan_room(state: Sequence[Mapping[str, Any]], bot_user_id: str,
              desired: Mapping[str, Mapping[str, Any]]) -> list[tuple[str, dict[str, Any]]]:
    current = {event["state_key"]: event for event in state
               if event.get("type") == COMMAND_DESCRIPTION_EVENT_TYPE and "state_key" in event}
    operations = []
    for key, content in desired.items():
        event = current.get(key, {})
        if event.get("sender") != bot_user_id or event.get("content") != content:
            operations.append((key, dict(content)))
    for key, event in current.items():
        if key not in desired and event.get("sender") == bot_user_id and event.get("content"):
            operations.append((key, {}))
    return operations


class MatrixCommandPublisher:
    def __init__(self, *, api: StateApi, bot_user_id: str, enabled: bool,
                 is_room_eligible: Callable[[str], Awaitable[bool]],
                 build_manifest: Callable[[], dict[str, dict[str, Any]]],
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 interval: float = 0.2) -> None:
        self.api = api
        self.bot_user_id = bot_user_id
        self.enabled = enabled
        self.is_room_eligible = is_room_eligible
        self.build_manifest = build_manifest
        self.sleep = sleep
        self.interval = interval
        self._rooms: dict[str, None] = {}
        self._queue: dict[str, None] = {}
        self._wake = asyncio.Event()
        self._full = False
        self._manifest: dict[str, dict[str, Any]] | None = None
        self._task: asyncio.Task | None = None
        self._warned_rooms: set[str] = set()
        self._has_put = False
        self._retry_delay = 1.0

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())
        self.request_full()

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    def request_full(self) -> None:
        self._full = True
        self._wake.set()

    def request_rooms(self, room_ids: Iterable[str]) -> None:
        for room_id in room_ids:
            self._rooms[room_id] = None
            self._queue[room_id] = None
        self._wake.set()

    def observe_sync(self, sync_data: Mapping[str, Any]) -> None:
        rooms = sync_data.get("rooms", {})
        for room_id in rooms.get("leave", {}):
            self._rooms.pop(room_id, None)
            self._queue.pop(room_id, None)
        for room_id, data in rooms.get("join", {}).items():
            events = [*data.get("state", {}).get("events", []), *data.get("timeline", {}).get("events", [])]
            if room_id not in self._rooms or any(event.get("type") == "m.room.power_levels" for event in events):
                self.request_rooms([room_id])

    async def _rebuild_manifest(self) -> None:
        self._full = False
        try:
            manifest = await asyncio.to_thread(self.build_manifest) if self.enabled else {}
        except Exception:
            logger.exception("Matrix: could not rebuild command descriptions")
        else:
            self._manifest = manifest
        self._queue.update(self._rooms)

    async def _run(self) -> None:
        while True:
            await self._wake.wait()
            self._wake.clear()
            self._retry_delay = 1.0
            while self._full or self._queue:
                if self._full:
                    await self._rebuild_manifest()
                    continue
                room_id = next(iter(self._queue))
                del self._queue[room_id]
                if self._manifest is None:
                    continue
                try:
                    await self._reconcile_room(room_id)
                except Exception:
                    logger.exception("Matrix: command-description reconciliation failed in %s", room_id)

    def _log_forbidden(self, room_id: str, level: int | None = None, required: int | None = None) -> None:
        if room_id not in self._warned_rooms:
            self._warned_rooms.add(room_id)
            logger.info("Matrix: cannot publish command descriptions in %s: needs %s has %s", room_id, required, level)

    async def _reconcile_room(self, room_id: str) -> None:
        from aiohttp import ClientError
        from mautrix.errors import MatrixRequestError

        desired = self._manifest if await self.is_room_eligible(room_id) else {}
        try:
            state = await self.api.get_room_state(room_id)
        except (MatrixRequestError, ClientError, TimeoutError):
            logger.warning("Matrix: cannot read command-description state in %s", room_id)
            return
        operations = plan_room(state, self.bot_user_id, desired)
        if not operations:
            return
        allowed, level, required = power_level_allows(state, self.bot_user_id, COMMAND_DESCRIPTION_EVENT_TYPE)
        if not allowed:
            self._log_forbidden(room_id, level, required)
            return
        published = cleared = 0
        for key, content in operations:
            if room_id not in self._rooms:
                break
            if not await self._put(room_id, key, content):
                break
            published += bool(content)
            cleared += not content
        logger.info("Matrix: command descriptions in %s: %d published, %d cleared", room_id, published, cleared)

    async def _put(self, room_id: str, key: str, content: dict[str, Any]) -> bool:
        from aiohttp import ClientError
        from mautrix.errors import MForbidden, MLimitExceeded, MatrixRequestError

        for attempt in range(6):
            if self._has_put:
                await self.sleep(self.interval)
            if room_id not in self._rooms:
                return False
            self._has_put = True
            try:
                await self.api.put_room_state(room_id, COMMAND_DESCRIPTION_EVENT_TYPE, key, content)
                return True
            except MLimitExceeded:
                if attempt == 5:
                    logger.warning("Matrix: command-description rate-limit retries exhausted in %s", room_id)
                    return False
                await self.sleep(self._retry_delay)
                self._retry_delay = min(30.0, self._retry_delay * 2)
            except MForbidden:
                self._log_forbidden(room_id)
                return False
            except (MatrixRequestError, ClientError, TimeoutError):
                logger.warning("Matrix: cannot publish command descriptions in %s", room_id)
                return False
        return False


class MatrixCommandDescriptionsMixin:
    _command_publisher: MatrixCommandPublisher | None = None

    def _start_command_descriptions(self) -> None:
        if self._command_publisher is not None or getattr(self, "_client", None) is None:
            return
        bot_user_id = self._user_id
        publisher = MatrixCommandPublisher(
            api=MautrixStateApi(self._client), bot_user_id=bot_user_id,
            enabled=self._extra_truthy(self.config, "command_descriptions", "MATRIX_COMMAND_DESCRIPTIONS", "true"),
            is_room_eligible=self._is_allowed_matrix_room_event,
            build_manifest=lambda: build_command_manifest(bot_user_id),
        )
        # Preserve the once-per-room warning across reconnects of this adapter.
        if not hasattr(self, "_command_description_warned_rooms"):
            self._command_description_warned_rooms: set[str] = set()
        publisher._warned_rooms = self._command_description_warned_rooms
        self._command_publisher = publisher
        publisher.request_rooms(self._joined_rooms)
        publisher.start()

    async def _stop_command_descriptions(self) -> None:
        if self._command_publisher is not None:
            await self._command_publisher.stop()
            self._command_publisher = None

    def _observe_command_description_sync(self, sync_data: dict[str, Any]) -> None:
        joined = getattr(self, "_joined_rooms", None)
        if joined is not None:
            joined.difference_update(sync_data.get("rooms", {}).get("leave", {}))
        if self._command_publisher is not None:
            self._command_publisher.observe_sync(sync_data)

    async def refresh_skill_group(self) -> None:
        if self._command_publisher is not None:
            self._command_publisher.request_full()
